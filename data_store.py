"""Daily scanner journal — saves 9:08 snapshot, enriches with actual open/30-min prices."""
from __future__ import annotations
import json, os, datetime

# Set DATA_DIR env var to a Railway persistent volume path if deployed there
DATA_DIR = os.getenv("DATA_DIR", os.path.join(os.path.dirname(__file__), "data"))
os.makedirs(DATA_DIR, exist_ok=True)


def _path(date_str: str) -> str:
    return os.path.join(DATA_DIR, f"scanner_{date_str}.json")


def save_snapshot(date_str: str, stocks: list, top_pick: str | None, top_loser: str | None) -> dict:
    path = _path(date_str)
    existing = {}
    if os.path.exists(path):
        with open(path) as f:
            existing = json.load(f)
    existing.update({
        "date":      date_str,
        "stocks":    stocks,
        "top_pick":  top_pick,
        "top_loser": top_loser,
        "results":   existing.get("results", {}),
    })
    with open(path, "w") as f:
        json.dump(existing, f, indent=2)
    return existing


def enrich(date_str: str, kite) -> dict:
    path = _path(date_str)
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


def get_history() -> list:
    files = sorted(
        [f for f in os.listdir(DATA_DIR)
         if f.startswith("scanner_") and f.endswith(".json")],
        reverse=True,
    )
    out = []
    for fname in files[:30]:
        try:
            with open(os.path.join(DATA_DIR, fname)) as f:
                out.append(json.load(f))
        except Exception:
            pass
    return out
