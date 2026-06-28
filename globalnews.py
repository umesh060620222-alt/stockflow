"""Global macro news collector — events that move US indices (S&P 500, Nasdaq, Dow).

Queries several Google News RSS feeds across key macro themes, classifies
each headline with weighted lexicons, then optionally summarises with Claude.
"""
from __future__ import annotations
import re, json, os, datetime as dt
from email.utils import parsedate_to_datetime

# ── Macro query groups ────────────────────────────────────────────────────────
QUERIES = [
    ("Geopolitical", [
        "war conflict attack missile strike",
        "Iran Israel war strike",
        "Russia Ukraine war",
        "North Korea nuclear missile",
        "China Taiwan tensions",
        "Middle East conflict",
    ]),
    ("Fed / Rates", [
        "Federal Reserve interest rates FOMC",
        "Fed rate hike cut decision",
        "US inflation CPI data",
        "Treasury yield bonds",
    ]),
    ("Economy", [
        "US recession GDP jobs report",
        "US unemployment layoffs",
        "US debt ceiling default",
        "banking crisis credit crunch",
    ]),
    ("Oil / Energy", [
        "oil price OPEC crude",
        "energy supply disruption",
    ]),
    ("Trade / Currency", [
        "US China trade tariffs",
        "dollar index DXY currency",
        "emerging market crisis",
    ]),
]

# ── Lexicons ──────────────────────────────────────────────────────────────────
BEARISH = {
    "war": 3, "attack": 2, "missile": 3, "strike": 2, "invasion": 3,
    "conflict": 2, "nuclear": 3, "escalation": 2, "escalates": 2,
    "sanctions": 2, "blockade": 3, "coup": 3, "assassin": 3,
    "recession": 3, "contraction": 2, "stagflation": 3, "banking crisis": 3,
    "inflation surges": 3, "inflation rises": 2, "cpi hotter": 3,
    "rate hike": 2, "hawkish": 2, "default": 3, "debt crisis": 3,
    "shutdown": 2, "downgrade": 2, "collapse": 3,
    "tariff": 2, "tariffs": 2, "trade war": 3, "supply disruption": 2,
    "oil spike": 2, "crude surge": 2,
    "sell-off": 2, "crash": 3, "plunge": 2, "risk-off": 2,
    "pandemic": 2, "outbreak": 2, "lockdown": 2,
}
BULLISH = {
    "ceasefire": 3, "peace deal": 3, "peace talks": 2, "truce": 3,
    "de-escalation": 3, "withdrawal": 2, "diplomacy": 1,
    "rate cut": 3, "dovish": 2, "pivot": 2, "pause": 1,
    "stimulus": 2, "easing": 2, "soft landing": 3,
    "strong jobs": 2, "jobs beat": 2, "gdp beats": 2, "beat estimates": 2,
    "trade deal": 3, "tariff relief": 3, "tariff cut": 3,
    "rally": 2, "risk-on": 2, "record high": 2, "rebound": 1, "recovery": 1,
}


def _compile(d):
    return [(re.compile(r"\b" + re.escape(k) + r"\b", re.I), k, v) for k, v in d.items()]


_BEAR = _compile(BEARISH)
_BULL = _compile(BULLISH)


def _score(text, compiled):
    hits, total = [], 0
    for rx, w, wt in compiled:
        if rx.search(text):
            hits.append(w); total += wt
    return hits, total


def _classify(title: str) -> dict:
    t = title or ""
    bh, bs = _score(t, _BEAR)
    bl, bu = _score(t, _BULL)
    net = bu - bs
    if net >= 2:
        sig = "bullish"
    elif net <= -2:
        sig = "bearish"
    elif bu and bs:
        sig = "mixed"
    elif bu:
        sig = "bullish"
    elif bs:
        sig = "bearish"
    else:
        sig = "neutral"
    return {"signal": sig, "bull_score": bu, "bear_score": bs,
            "matched": (bl + bh)[:5]}


def _fetch(query: str, n=6):
    import requests, xml.etree.ElementTree as ET
    url = ("https://news.google.com/rss/search?q=" +
           requests.utils.quote(query) + "&hl=en-US&gl=US&ceid=US:en")
    try:
        r = requests.get(url, timeout=12, headers={"User-Agent": "Mozilla/5.0"})
        items = []
        now_utc = dt.datetime.utcnow().replace(tzinfo=dt.timezone.utc)
        for it in ET.fromstring(r.content).findall(".//item")[:n]:
            title = (it.findtext("title") or "").strip()
            link  = (it.findtext("link") or "").strip()
            pub   = None
            try:
                pub = parsedate_to_datetime(it.findtext("pubDate") or "")
            except Exception:
                pass
            age_min = round((now_utc - pub).total_seconds() / 60) if pub else None
            if title and (age_min is None or age_min < 1440):  # within 24h
                items.append({"title": title, "link": link, "age_min": age_min})
        return items
    except Exception:
        return []


def _claude_summary(all_headlines: list[str]) -> dict | None:
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key or not all_headlines:
        return None
    import requests as req
    model = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
    blob = "\n".join(f"- {h}" for h in all_headlines[:25])
    prompt = (
        "You are a macro analyst. Given these recent global news headlines, assess the "
        "short-term impact on US equity indices (S&P 500, Nasdaq, Dow).\n\n"
        + blob +
        '\n\nReply ONLY with JSON: '
        '{"overall":"bullish|bearish|mixed|neutral",'
        '"conviction":0-100,'
        '"summary":"2-3 sentence plain English impact summary",'
        '"top_risks":["...","..."],'
        '"top_tailwinds":["...","..."]}'
    )
    try:
        r = req.post("https://api.anthropic.com/v1/messages",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": model, "max_tokens": 300, "temperature": 0,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=25)
        text = r.json()["content"][0]["text"]
        m = re.search(r"\{.*\}", text, re.S)
        return json.loads(m.group(0)) if m else None
    except Exception:
        return None


def fetch_global_news() -> dict:
    """Fetch, classify and summarise global macro news. Returns dict ready for JSON."""
    import datetime as _dt
    now_ist = _dt.datetime.utcnow() + _dt.timedelta(hours=5, minutes=30)
    groups = []
    all_titles = []
    seen = set()

    for category, queries in QUERIES:
        items = []
        for q in queries:
            for it in _fetch(q):
                key = it["title"][:60]
                if key in seen:
                    continue
                seen.add(key)
                c = _classify(it["title"])
                age = it["age_min"]
                items.append({
                    "title": it["title"],
                    "link": it["link"],
                    "signal": c["signal"],
                    "bull_score": c["bull_score"],
                    "bear_score": c["bear_score"],
                    "matched": c["matched"],
                    "age_min": age,
                    "age_label": (f"{age}m ago" if age is not None and age < 60
                                  else f"{age//60}h ago" if age is not None
                                  else ""),
                })
                if c["signal"] in ("bearish", "bullish", "mixed"):
                    all_titles.append(it["title"])
        if items:
            # sort: most impactful (highest score) first
            items.sort(key=lambda x: max(x["bull_score"], x["bear_score"]), reverse=True)
            groups.append({"category": category, "items": items[:10]})

    claude = _claude_summary(all_titles)
    return {
        "updated": now_ist.strftime("%Y-%m-%d %H:%M IST"),
        "groups": groups,
        "claude": claude,
    }
