"""
BrokerHub Drop Console — backend (Ticketmaster edition)
-------------------------------------------------------
Two jobs:
  GET  /shows?q=<artist or ticketmaster link>&country=US   -> all events for that artist
  POST /thoughts  {artist, cities[], venues[]}             -> a short broker-facing take

Uses the official Ticketmaster Discovery API (no scraping) and the Anthropic API.
Deploy on Railway. Set these environment variables:
  TICKETMASTER_API_KEY   (your Consumer Key from developer.ticketmaster.com -> My Apps)
  ANTHROPIC_API_KEY      (your existing key)
  ANTHROPIC_MODEL        (optional, e.g. claude-sonnet-4-5 — whatever your key uses)
  ENABLE_WEB_SEARCH      (optional, "false" to disable web search in /thoughts)
"""

import os
import datetime
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

TM_KEY = os.environ.get("TICKETMASTER_API_KEY", "")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-5")
ENABLE_WEB_SEARCH = os.environ.get("ENABLE_WEB_SEARCH", "true").lower() != "false"

TM_BASE = "https://app.ticketmaster.com/discovery/v2"
EST = ZoneInfo("America/New_York")

app = FastAPI(title="BrokerHub Drop Console backend")

# Allow the Drop Console (browser or local file) to call this backend.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------- helpers ----------
def keyword_from_query(q: str) -> str:
    """Accept a plain artist name OR a Ticketmaster link and return a search keyword."""
    q = q.strip()
    if not q.startswith("http"):
        return q
    slug = urlparse(q).path.strip("/").split("/")[0]
    words = slug.replace("-", " ").split()
    stop = {"presents", "tour", "tickets", "event", "presale", "the", "artist"}
    out = []
    for w in words:
        if w.lower() in stop:
            break
        out.append(w)
    return " ".join(out[:4]) or slug.replace("-", " ")


def onsale_est(iso: str) -> str:
    """'2026-06-23T14:00:00Z' -> '10am est' (or '10:30am est')."""
    if not iso:
        return ""
    try:
        dt = datetime.datetime.fromisoformat(iso.replace("Z", "+00:00"))
        local = dt.astimezone(EST)
        hour = local.strftime("%I").lstrip("0") or "12"
        ampm = local.strftime("%p").lower()
        minute = local.minute
        return f"{hour}:{minute:02d}{ampm} est" if minute else f"{hour}{ampm} est"
    except Exception:
        return ""


def transform(event: dict) -> dict:
    venues = (event.get("_embedded", {}).get("venues") or [{}])
    v = venues[0] if venues else {}
    city = (v.get("city") or {}).get("name", "")
    state = (v.get("state") or {}).get("stateCode") or (v.get("country") or {}).get("countryCode", "")
    sales = event.get("sales", {}) or {}
    public_start = (sales.get("public") or {}).get("startDateTime", "")
    presales = [
        {"name": p.get("name", ""), "start": p.get("startDateTime", "")}
        for p in (sales.get("presales") or [])
    ]
    return {
        "city": city,
        "state": state,
        "venue": v.get("name", ""),
        "url": event.get("url", ""),
        "date": (event.get("dates", {}).get("start", {}) or {}).get("localDate", ""),
        "onsaleEst": onsale_est(public_start),
        "presales": presales,
    }


# ---------- routes ----------
@app.get("/")
def root():
    return {"ok": True, "service": "brokerhub-drop-console-backend"}


@app.get("/shows")
async def shows(q: str, country: str | None = None):
    if not TM_KEY:
        raise HTTPException(500, "TICKETMASTER_API_KEY is not set on the server.")

    keyword = keyword_from_query(q)
    async with httpx.AsyncClient(timeout=25) as client:
        # 1) resolve the artist (attraction)
        ar = await client.get(
            f"{TM_BASE}/attractions.json",
            params={"keyword": keyword, "apikey": TM_KEY, "size": 5},
        )
        if ar.status_code != 200:
            raise HTTPException(502, f"Ticketmaster attractions lookup failed ({ar.status_code}).")
        attractions = ar.json().get("_embedded", {}).get("attractions", [])
        if not attractions:
            raise HTTPException(404, f"No artist found for '{keyword}'. Try the exact artist name.")
        artist = attractions[0]
        aid, name = artist["id"], artist.get("name", keyword)

        # 2) pull all events for that artist (paginated)
        events, page, total_pages = [], 0, 1
        while page < total_pages and page < 6:
            params = {
                "attractionId": aid,
                "apikey": TM_KEY,
                "size": 200,
                "page": page,
                "sort": "date,asc",
            }
            if country:
                params["countryCode"] = country
            er = await client.get(f"{TM_BASE}/events.json", params=params)
            if er.status_code != 200:
                break
            data = er.json()
            events.extend(data.get("_embedded", {}).get("events", []))
            total_pages = data.get("page", {}).get("totalPages", 1)
            page += 1

    # de-dupe by event URL, keep order
    seen, out = set(), []
    for e in events:
        row = transform(e)
        if row["url"] and row["url"] in seen:
            continue
        seen.add(row["url"])
        out.append(row)

    return {"artist": name, "count": len(out), "shows": out}


class ThoughtReq(BaseModel):
    artist: str
    cities: list[str] = []
    venues: list[str] = []


@app.post("/thoughts")
async def thoughts(req: ThoughtReq):
    if not ANTHROPIC_KEY:
        raise HTTPException(500, "ANTHROPIC_API_KEY is not set on the server.")

    cities = ", ".join(req.cities[:30]) or "various markets"
    prompt = (
        f"You're helping an admin of a ticket-broker community write a short internal note about an "
        f"upcoming tour to post for members.\n\n"
        f"Artist: {req.artist}\n"
        f"Markets on this tour: {cities}\n\n"
        f"Write 2-4 sentences, broker-facing and direct. Cover: which markets to prioritize "
        f"(biggest / highest-demand), a quick seating-strategy note, and an honest read on expected "
        f"demand and the resale outlook based on the artist's current popularity and venue sizes. "
        f"Use web search to ground your read in recent, real info about this tour and the artist's "
        f"current standing. Do NOT invent specific sales figures, sellout times, or stats — if you "
        f"don't have hard numbers, speak qualitatively (e.g. 'strong demand expected', "
        f"'softer secondary market'). Output only the note text: no preamble, no markdown headers, "
        f"no bullet points."
    )

    body = {
        "model": ANTHROPIC_MODEL,
        "max_tokens": 700,
        "messages": [{"role": "user", "content": prompt}],
    }
    if ENABLE_WEB_SEARCH:
        body["tools"] = [{"type": "web_search_20250305", "name": "web_search"}]

    async with httpx.AsyncClient(timeout=90) as client:
        r = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json=body,
        )
    if r.status_code != 200:
        raise HTTPException(502, f"Anthropic API error ({r.status_code}): {r.text[:200]}")

    data = r.json()
    text = "".join(
        b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"
    ).strip()
    return {"thoughts": text}
