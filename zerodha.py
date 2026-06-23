"""Zerodha Kite Connect data source — drop-in replacement for the yfinance fetch.

Returns the exact same shape as data.fetch(): {symbol: DataFrame[o,h,l,c,v] in IST}.
Keyed by the same '.NS' symbols as config.UNIVERSE so nothing downstream changes.

Auth model: api_key/secret identify your Kite Connect app; the access token is
generated once per day via login.py and cached in access_token.json.
"""
from __future__ import annotations
import os, json, datetime as dt
import pandas as pd
import config

HERE = os.path.dirname(__file__)
TOKEN_FILE = os.path.join(HERE, "access_token.json")
INSTR_CACHE = os.path.join(HERE, "instruments_nse.csv")

# yfinance interval -> Kite interval
_KINT = {"1m": "minute", "3m": "3minute", "5m": "5minute", "15m": "15minute"}


def _creds():
    """API key/secret from kite_secrets.py (preferred) or env vars."""
    try:
        import kite_secrets as k
        return k.API_KEY, k.API_SECRET
    except Exception:
        return os.getenv("KITE_API_KEY", ""), os.getenv("KITE_API_SECRET", "")


def load_token():
    """Today's access token, or None if missing/stale (tokens expire daily)."""
    if os.path.exists(TOKEN_FILE):
        d = json.load(open(TOKEN_FILE))
        if d.get("date") == str(dt.date.today()):
            return d.get("access_token")
    return None


def save_token(tok: str):
    json.dump({"access_token": tok, "date": str(dt.date.today())}, open(TOKEN_FILE, "w"))


def kite(with_token=True):
    from kiteconnect import KiteConnect
    api_key, _ = _creds()
    if not api_key:
        raise RuntimeError("No API key — set kite_secrets.py or KITE_API_KEY env.")
    kc = KiteConnect(api_key=api_key)
    if with_token:
        tok = load_token()
        if not tok:
            raise RuntimeError("No valid access token for today — run: python login.py")
        kc.set_access_token(tok)
    return kc


def _ksym(sym: str) -> str:
    """'.NS' yfinance symbol -> Kite tradingsymbol; benchmark -> NIFTY 50 index."""
    if sym == config.BENCHMARK:
        return "NIFTY 50"
    return sym.replace(".NS", "")


def instrument_map(kc) -> dict:
    """{tradingsymbol: instrument_token} for NSE, cached to a daily CSV."""
    if os.path.exists(INSTR_CACHE):
        df = pd.read_csv(INSTR_CACHE)
    else:
        df = pd.DataFrame(kc.instruments("NSE"))
        df.to_csv(INSTR_CACHE, index=False)
    return {r["tradingsymbol"]: int(r["instrument_token"]) for _, r in df.iterrows()}


def fetch(symbols=None, interval=None, period=None) -> dict:
    symbols = symbols or (config.UNIVERSE + [config.BENCHMARK])
    interval = interval or config.INTERVAL
    kint = _KINT.get(interval, "5minute")
    days = {"1d": 1, "5d": 5, "7d": 7}.get(period or config.PERIOD, 1)

    kc = kite()
    imap = instrument_map(kc)
    to_d = dt.datetime.now()
    from_d = to_d - dt.timedelta(days=days + 3)   # pad for weekends/holidays

    out = {}
    for sym in symbols:
        ks = _ksym(sym)
        tok = imap.get(ks)
        if not tok:
            print(f"  no instrument token for {ks} ({sym})")
            continue
        try:
            rows = kc.historical_data(tok, from_d, to_d, kint)
        except Exception as e:
            print(f"  {sym}: {e}")
            continue
        if not rows:
            continue
        df = pd.DataFrame(rows).set_index("date")
        df.index = pd.to_datetime(df.index)
        df.index = (df.index.tz_localize(config.TZ) if df.index.tz is None
                    else df.index.tz_convert(config.TZ))
        df.index.name = "ts"          # avoid clashing with the 'date' column below
        df = df[["open", "high", "low", "close", "volume"]]
        df["date"] = df.index.date
        out[sym] = df
    return out
