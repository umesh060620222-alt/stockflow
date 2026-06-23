"""Collect intraday bars from yfinance (free, no broker auth) and cache to disk.

This is the swap point: replace fetch() with a broker WebSocket recorder later
for true real-time. For the 'did the signal predict the move' test, today's bars
pulled after close are exactly equivalent to having recorded them live.
"""
from __future__ import annotations
import os
import pandas as pd
import config

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")


def fetch(symbols=None, interval=None, period=None) -> dict:
    """Return {symbol: DataFrame[open,high,low,close,volume]} indexed in IST.
    Dispatches to the configured source. Caches each symbol so reruns are offline."""
    if config.SOURCE == "zerodha":
        import zerodha
        out = zerodha.fetch(symbols, interval, period)
        _cache(out, interval or config.INTERVAL)
        return out
    return _fetch_yfinance(symbols, interval, period)


def _cache(out: dict, interval: str):
    os.makedirs(DATA_DIR, exist_ok=True)
    for sym, df in out.items():
        df.to_csv(os.path.join(DATA_DIR, f"{sym.replace('^','_')}_{interval}.csv"))


def _fetch_yfinance(symbols=None, interval=None, period=None) -> dict:
    import yfinance as yf
    symbols = symbols or (config.UNIVERSE + [config.BENCHMARK])
    interval = interval or config.INTERVAL
    period = period or config.PERIOD
    os.makedirs(DATA_DIR, exist_ok=True)

    raw = yf.download(symbols, period=period, interval=interval,
                      group_by="ticker", auto_adjust=False, progress=False, threads=True)
    out = {}
    for sym in symbols:
        try:
            df = raw[sym].copy() if len(symbols) > 1 else raw.copy()
        except Exception:
            continue
        df = df.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]]
        df = df.dropna(how="any")
        if df.empty:
            continue
        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC")
        df.index = df.index.tz_convert(config.TZ)
        df["date"] = df.index.date
        out[sym] = df
        df.to_csv(os.path.join(DATA_DIR, f"{sym.replace('^','_')}_{interval}.csv"))
    return out


def load_cached(symbols=None, interval=None) -> dict:
    """Reload previously fetched CSVs (offline reruns while tuning)."""
    symbols = symbols or (config.UNIVERSE + [config.BENCHMARK])
    interval = interval or config.INTERVAL
    out = {}
    for sym in symbols:
        path = os.path.join(DATA_DIR, f"{sym.replace('^','_')}_{interval}.csv")
        if not os.path.exists(path):
            continue
        df = pd.read_csv(path, index_col=0, parse_dates=True)
        if df.index.tz is None:
            df.index = df.index.tz_localize(config.TZ)
        df["date"] = df.index.date
        out[sym] = df
    return out
