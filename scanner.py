"""Unusual volume scanner — finds stocks where buyers/sellers are actively filling orders.

Score = (current_volume / expected_volume_by_now) * abs(price_change_pct)
High score + price up = unusual buying. High score + price down = unusual selling.
"""
from __future__ import annotations
import datetime, json, os

WATCHLIST = [
    "RELIANCE","TCS","HDFCBANK","ICICIBANK","INFY","HINDUNILVR","ITC","SBIN",
    "BAJFINANCE","AXISBANK","KOTAKBANK","LT","ASIANPAINT","MARUTI","SUNPHARMA",
    "DRREDDY","WIPRO","TECHM","ULTRACEMCO","TITAN","NESTLEIND","POWERGRID","NTPC",
    "ONGC","TATAMOTORS","TATASTEEL","JSWSTEEL","HINDALCO","COALINDIA","DIVISLAB",
    "CIPLA","BAJAJFINSV","ADANIPORTS","GRASIM","HEROMOTOCO","EICHERMOT","BPCL",
    "VEDL","BRITANNIA","PIDILITIND",
]

_AVG_VOL_CACHE: dict = {}   # {symbol: avg_20d_volume}
_AVG_VOL_DATE: str = ""


def _avg_volumes() -> dict:
    """20-day average daily volumes from yfinance (cached once per day)."""
    global _AVG_VOL_CACHE, _AVG_VOL_DATE
    today = str(datetime.date.today())
    if _AVG_VOL_DATE == today and _AVG_VOL_CACHE:
        return _AVG_VOL_CACHE
    try:
        import yfinance as yf
        tickers = [s + ".NS" for s in WATCHLIST]
        hist = yf.download(tickers, period="25d", interval="1d",
                           group_by="ticker", progress=False, threads=True,
                           auto_adjust=False)
        out = {}
        for sym in WATCHLIST:
            try:
                v = hist[sym + ".NS"]["Volume"].tail(20).mean()
                out[sym] = float(v)
            except Exception:
                pass
        _AVG_VOL_CACHE = out
        _AVG_VOL_DATE = today
    except Exception:
        pass
    return _AVG_VOL_CACHE


def scan() -> dict:
    """Return top unusual-volume stocks right now."""
    ist_now = datetime.datetime.utcnow() + datetime.timedelta(hours=5, minutes=30)
    market_open = ist_now.replace(hour=9, minute=15, second=0, microsecond=0)
    elapsed_min = max(1.0, (ist_now - market_open).total_seconds() / 60)
    time_fraction = min(1.0, elapsed_min / 375)   # 375 min = full trading day

    # ── live quotes from Kite ─────────────────────────────────────────────────
    import zerodha as Z
    try:
        kc = Z.kite()
        instruments = [f"NSE:{s}" for s in WATCHLIST]
        quotes = kc.quote(instruments)
    except Exception as e:
        return {"error": str(e), "stocks": []}

    avg_vols = _avg_volumes()

    stocks = []
    for sym in WATCHLIST:
        q = quotes.get(f"NSE:{sym}")
        if not q:
            continue
        ltp      = float(q.get("last_price", 0))
        volume   = int(q.get("volume", 0))
        buy_qty  = int(q.get("buy_quantity", 0))
        sell_qty = int(q.get("sell_quantity", 0))
        ohlc     = q.get("ohlc", {})
        prev_close = float(ohlc.get("close", ltp) or ltp)

        chg_pct = round((ltp - prev_close) / prev_close * 100, 2) if prev_close else 0.0
        ratio   = round(buy_qty / sell_qty, 2) if sell_qty else 0.0

        avg_vol  = avg_vols.get(sym, 0)
        vol_rate = 0.0
        if avg_vol > 0 and time_fraction > 0:
            vol_rate = round(volume / (avg_vol * time_fraction), 2)

        # score: unusual volume × price conviction
        score = round(vol_rate * (abs(chg_pct) if abs(chg_pct) > 0.1 else 0.1), 3)

        direction = "up" if chg_pct > 0.2 else "down" if chg_pct < -0.2 else "flat"

        stocks.append({
            "symbol":    sym,
            "ltp":       round(ltp, 2),
            "chg_pct":   chg_pct,
            "volume":    volume,
            "vol_rate":  vol_rate,
            "ratio":     ratio,
            "score":     score,
            "direction": direction,
        })

    stocks.sort(key=lambda x: x["score"], reverse=True)
    return {
        "updated":       ist_now.strftime("%H:%M IST"),
        "time_fraction": round(time_fraction, 3),
        "stocks":        stocks[:20],
    }
