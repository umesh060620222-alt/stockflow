"""Daily scanner journal — pre-market snapshots and manual session ledger."""
from __future__ import annotations
import json, os, datetime

DATA_DIR      = os.getenv("DATA_DIR", os.path.join(os.path.dirname(__file__), "data"))
PREMARKET_DIR = os.path.join(DATA_DIR, "premarket")
SESSIONS_DIR  = os.path.join(DATA_DIR, "sessions")

for _d in (DATA_DIR, PREMARKET_DIR, SESSIONS_DIR):
    os.makedirs(_d, exist_ok=True)


# ── Pre-market (auto at 9:08) ──────────────────────────────────────────────

def save_premarket(date_str: str, body: dict) -> dict:
    """Save 9:08 auto-snapshot to data/premarket/scanner_YYYY-MM-DD.json."""
    path = os.path.join(PREMARKET_DIR, f"scanner_{date_str}.json")
    body["date"] = date_str
    with open(path, "w") as f:
        json.dump(body, f)
    return {"saved": True, "type": "premarket"}


# ── Sessions (one file per manual connect/disconnect) ──────────────────────

def save_session(date_str: str, session_id: str, body: dict) -> dict:
    """Save a manual session to data/sessions/YYYY-MM-DD/session_HH-MM-SS.json."""
    day_dir = os.path.join(SESSIONS_DIR, date_str)
    os.makedirs(day_dir, exist_ok=True)
    path = os.path.join(day_dir, f"session_{session_id}.json")
    body["date"] = date_str
    body["session_id"] = session_id
    with open(path, "w") as f:
        json.dump(body, f)
    return {"saved": True, "type": "session", "session_id": session_id}


def get_sessions(date_str: str) -> list:
    """List session summaries for a day (no raw ticks)."""
    day_dir = os.path.join(SESSIONS_DIR, date_str)
    if not os.path.exists(day_dir):
        return []
    out = []
    for fname in sorted(f for f in os.listdir(day_dir) if f.endswith(".json")):
        try:
            with open(os.path.join(day_dir, fname)) as f:
                d = json.load(f)
            out.append({
                "session_id":  d.get("session_id"),
                "date":        d.get("date"),
                "top_pick":    d.get("top_pick"),
                "top_loser":   d.get("top_loser"),
                "stock_count": len(d.get("ticks", [])),
                "file":        fname,
            })
        except Exception:
            pass
    return out


# ── Shared helpers ─────────────────────────────────────────────────────────

def get_raw(date_str: str) -> dict:
    """Return raw pre-market snapshot (checks premarket/ then legacy data/)."""
    for folder in (PREMARKET_DIR, DATA_DIR):
        path = os.path.join(folder, f"scanner_{date_str}.json")
        if os.path.exists(path):
            with open(path) as f:
                return json.load(f)
    return {"error": f"No pre-market data for {date_str}"}


def get_session_raw(date_str: str, session_id: str) -> dict:
    """Return raw tick data for a specific session."""
    path = os.path.join(SESSIONS_DIR, date_str, f"session_{session_id}.json")
    if not os.path.exists(path):
        return {"error": f"No session {session_id} for {date_str}"}
    with open(path) as f:
        return json.load(f)


def get_history() -> list:
    """Last 30 days summary — premarket + session count per day."""
    seen, entries = set(), []
    for folder in (PREMARKET_DIR, DATA_DIR):
        for fname in os.listdir(folder):
            if not (fname.startswith("scanner_") and fname.endswith(".json")):
                continue
            date_str = fname[len("scanner_"):-len(".json")]
            if date_str in seen:
                continue
            seen.add(date_str)
            entries.append((date_str, os.path.join(folder, fname)))

    out = []
    for date_str, path in sorted(entries, reverse=True)[:30]:
        try:
            with open(path) as f:
                d = json.load(f)
            sessions = get_sessions(date_str)
            out.append({
                "date":            date_str,
                "top_pick":        d.get("top_pick"),
                "top_loser":       d.get("top_loser"),
                "has_premarket":   "premarket_ticks" in d,
                "premarket_count": len(d.get("premarket_ticks", [])),
                "sessions":        sessions,
                "session_count":   len(sessions),
            })
        except Exception:
            pass
    return out


# ── Legacy (keep for compat) ───────────────────────────────────────────────

def save_snapshot(date_str: str, body: dict) -> dict:
    return save_premarket(date_str, body)


def enrich(date_str: str, kite) -> dict:
    path = os.path.join(PREMARKET_DIR, f"scanner_{date_str}.json")
    if not os.path.exists(path):
        path = os.path.join(DATA_DIR, f"scanner_{date_str}.json")
    if not os.path.exists(path):
        return {"error": f"No snapshot for {date_str}"}
    with open(path) as f:
        data = json.load(f)

    syms = {s["sym"] for s in data.get("stocks", [])}
    rows = kite.instruments("NSE")
    token_map = {r["tradingsymbol"]: r["instrument_token"]
                 for r in rows
                 if r.get("instrument_type") == "EQ" and r["tradingsymbol"] in syms}

    date = datetime.datetime.strptime(date_str, "%Y-%m-%d")
    from_dt = date.replace(hour=9, minute=14)
    to_dt   = date.replace(hour=9, minute=46)

    results = data.get("results", {})
    for sym, token in token_map.items():
        try:
            candles = kite.historical_data(token, from_dt, to_dt, "minute")
            if len(candles) < 2:
                continue
            open_price = float(candles[0]["open"])
            price_30m  = float(candles[-1]["close"])
            results[sym] = {
                "open":      round(open_price, 2),
                "price_30m": round(price_30m, 2),
                "pnl_pct":   round((price_30m - open_price) / open_price * 100, 2),
            }
        except Exception:
            pass

    data["results"] = results
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
    return data
