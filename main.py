"""
BrokerHub Drop Console — backend (SeatGeek edition)
---------------------------------------------------
Two jobs:
  GET  /shows?q=<artist or link>&country=US   -> all upcoming events for that artist
  POST /thoughts  {artist, cities[], venues[]} -> a short broker-facing take

Uses the free SeatGeek public API (no approval needed) and the Anthropic API.
Deploy on Railway. Set these environment variables:
  SEATGEEK_CLIENT_ID   (free: https://seatgeek.com/build -> create an app -> Client ID)
  ANTHROPIC_API_KEY    (your existing key)
  ANTHROPIC_MODEL      (optional, e.g. claude-sonnet-4-5 — whatever your key uses)
  ENABLE_WEB_SEARCH    (optional, "false" to disable web search in /thoughts)
"""

import os
import datetime
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

SEATGEEK_ID = os.environ.get("SEATGEEK_CLIENT_ID", "")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-5")
ENABLE_WEB_SEARCH = os.environ.get("ENABLE_WEB_SEARCH", "true").lower() != "false"

SG_BASE = "https://api.seatgeek.com/2"

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
    """Accept a plain artist name OR any ticketing link and return a search keyword."""
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


def transform(event: dict) -> dict:
    v = event.get("venue", {}) or {}
    city = v.get("city", "")
    state = v.get("state") or v.get("country", "")
    date = (event.get("datetime_local") or event.get("datetime_utc") or "")[:10]
    return {
        "city": city,
        "state": state,
        "venue": v.get("name", ""),
        "url": event.get("url", ""),
        "date": date,
        "onsaleEst": "",   # SeatGeek doesn't expose primary on-sale times — set manually
        "presales": [],
    }


# ---------- routes ----------
@app.get("/")
def root():
    return {"ok": True, "service": "brokerhub-drop-console-backend"}


@app.get("/shows")
async def shows(q: str, country: str | None = None):
    if not SEATGEEK_ID:
        raise HTTPException(500, "SEATGEEK_CLIENT_ID is not set on the server.")

    keyword = keyword_from_query(q)
    now_iso = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

    async with httpx.AsyncClient(timeout=25) as client:
        # 1) resolve the artist (performer)
        pr = await client.get(
            f"{SG_BASE}/performers",
            params={"q": keyword, "client_id": SEATGEEK_ID, "per_page": 5},
        )
        if pr.status_code != 200:
            raise HTTPException(502, f"SeatGeek performer lookup failed ({pr.status_code}).")
        performers = pr.json().get("performers", [])
        if not performers:
            raise HTTPException(404, f"No artist found for '{keyword}'. Try the exact artist name.")
        performer = performers[0]
        slug, name = performer["slug"], performer.get("name", keyword)

        # 2) pull all upcoming events for that performer (paginated)
        events, page = [], 1
        while page <= 6:
            params = {
                "performers.slug": slug,
                "client_id": SEATGEEK_ID,
                "per_page": 100,
                "page": page,
                "sort": "datetime_utc.asc",
                "datetime_utc.gt": now_iso,
            }
            if country:
                params["venue.country"] = country
            er = await client.get(f"{SG_BASE}/events", params=params)
            if er.status_code != 200:
                break
            data = er.json()
            evs = data.get("events", [])
            events.extend(evs)
            total = data.get("meta", {}).get("total", 0)
            if not evs or page * 100 >= total:
                break
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
