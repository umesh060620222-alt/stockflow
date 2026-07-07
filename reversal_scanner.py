"""Resistance reversal scanner.

Flow:
  1. scan_near_resistance() — fetch daily OHLCV via yfinance, find stocks within
     NEAR_PCT% of a key resistance level (prev-day high, 20d high, 52w high).
  2. check_reversal(symbol, kc) — fetch latest 5-min candles from Kite, run
     TA-Lib (preferred) or pandas-ta candlestick pattern detection.
  3. Returns confirmed bearish-reversal candidates ready to flash as SELL.

Additional signals beyond candlestick patterns:
  - RSI divergence    : price at new high, RSI making lower high (momentum fading)
  - Volume dry-up     : last 3 up-candles have shrinking volume (distribution)
  - Stochastic OB     : Stoch %K > 80 AND bearish crossover (%K crosses below %D)
  - MACD bearish cross: MACD line crosses below signal line near resistance
  - BB upper touch    : close > upper Bollinger Band (mean-reversion setup)
  - Failed breakout   : price briefly pierced resistance then closed back below it
"""
from __future__ import annotations
import datetime, logging
import numpy as np
import pandas as pd

log = logging.getLogger("reversal")

try:
    import talib as ta
    HAS_TALIB = True
except ImportError:
    HAS_TALIB = False

try:
    import pandas_ta as pta
    HAS_PANDAS_TA = True
except ImportError:
    HAS_PANDAS_TA = False

from scanner import WATCHLIST_DAILY

NEAR_PCT   = 0.50   # within this % of resistance → candidate
SCAN_TOP   = 40     # how many stocks from WATCHLIST_DAILY to scan daily

# TA-Lib CDL function → label  (all bearish = negative return value)
BEARISH_PATTERNS = {
    "CDLSHOOTINGSTAR":    "Shooting Star",
    "CDLHANGINGMAN":      "Hanging Man",
    "CDLENGULFING":       "Bearish Engulfing",
    "CDLHARAMI":          "Bearish Harami",
    "CDLEVENINGSTAR":     "Evening Star",
    "CDLDARKCLOUDCOVER":  "Dark Cloud Cover",
    "CDLGRAVESTONEDOJI":  "Gravestone Doji",
    "CDL3BLACKCROWS":     "Three Black Crows",
    "CDLIDENTICAL3CROWS": "Identical Three Crows",
    "CDLABANDONEDBABY":   "Abandoned Baby (bear)",
}

# pandas-ta pattern names (subset available without TA-Lib)
PANDAS_TA_PATTERNS = [
    "shooting_star", "hanging_man", "engulfing",
    "harami", "evening_star", "dark_cloud_cover", "gravestone_doji",
]


# ── resistance levels ─────────────────────────────────────────────────────────

def _resistance_levels(df: pd.DataFrame) -> dict:
    """Key resistance levels from daily OHLCV (High column)."""
    levels: dict[str, float] = {}
    if len(df) >= 2:
        levels["prev_day_high"] = float(df["High"].iloc[-2])
    if len(df) >= 20:
        levels["20d_high"] = float(df["High"].tail(20).max())
    if len(df) >= 60:
        levels["3m_high"] = float(df["High"].tail(63).max())
    if len(df) >= 126:
        levels["6m_high"] = float(df["High"].tail(126).max())
    if len(df) >= 252:
        levels["52w_high"] = float(df["High"].tail(252).max())
    return levels


def _nearest_resistance(ltp: float, levels: dict) -> tuple[str, float] | tuple[None, None]:
    """Return (name, level) of closest resistance within NEAR_PCT%, or (None,None)."""
    best_name, best_level, best_dist = None, None, float("inf")
    for name, level in levels.items():
        if level <= 0:
            continue
        dist = (level - ltp) / level * 100   # positive = price below resistance
        if 0 <= dist <= NEAR_PCT and dist < best_dist:
            best_name, best_level, best_dist = name, level, dist
    return best_name, best_level


# ── daily scan ────────────────────────────────────────────────────────────────

def scan_near_resistance(symbols: list[str] | None = None) -> list[dict]:
    """Return stocks whose last close is within NEAR_PCT% of a key resistance."""
    import yfinance as yf
    if symbols is None:
        symbols = WATCHLIST_DAILY[:SCAN_TOP]
    yf_syms = [s + ".NS" for s in symbols]
    try:
        raw = yf.download(yf_syms, period="252d", interval="1d",
                          group_by="ticker", progress=False,
                          threads=True, auto_adjust=True)
    except Exception as e:
        log.warning("yfinance download failed: %s", e)
        return []

    out: list[dict] = []
    for sym, yf_sym in zip(symbols, yf_syms):
        try:
            df = raw[yf_sym] if len(yf_syms) > 1 else raw
            df = df.dropna()
            if len(df) < 5:
                continue
            ltp = float(df["Close"].iloc[-1])
            levels = _resistance_levels(df)
            res_name, res_level = _nearest_resistance(ltp, levels)
            if res_name:
                out.append({
                    "symbol":         sym,
                    "ltp":            round(ltp, 2),
                    "resistance":     round(res_level, 2),
                    "resistance_type": res_name.replace("_", " "),
                    "pct_from_res":   round((res_level - ltp) / res_level * 100, 2),
                    "patterns":       [],   # filled by check_reversal
                    "extra_signals":  [],
                    "confirmed":      False,
                })
        except Exception as e:
            log.debug("scan error %s: %s", sym, e)

    out.sort(key=lambda x: x["pct_from_res"])
    return out


# ── pattern detection ─────────────────────────────────────────────────────────

def _patterns_talib(o, h, l, c) -> list[str]:
    hits = []
    o_a, h_a, l_a, c_a = (np.asarray(x, dtype=float) for x in (o, h, l, c))
    for fn, label in BEARISH_PATTERNS.items():
        try:
            result = getattr(ta, fn)(o_a, h_a, l_a, c_a)
            if result[-1] < 0:
                hits.append(label)
        except Exception:
            pass
    return hits


def _patterns_pandas_ta(df: pd.DataFrame) -> list[str]:
    hits = []
    if not HAS_PANDAS_TA:
        return hits
    try:
        for pat in PANDAS_TA_PATTERNS:
            result = pta.cdl_pattern(df["Open"], df["High"], df["Low"], df["Close"], name=pat)
            if result is not None and len(result):
                val = result.iloc[-1].values
                if any(v < 0 for v in val):
                    hits.append(pat.replace("_", " ").title())
    except Exception as e:
        log.debug("pandas_ta pattern error: %s", e)
    return hits


def _extra_signals(df: pd.DataFrame) -> list[str]:
    """Non-candlestick bearish signals."""
    sigs = []
    c = df["Close"].values
    h = df["High"].values
    v = df["Volume"].values if "Volume" in df.columns else None

    # RSI divergence — price at new high but RSI making lower high
    if HAS_TALIB and len(c) >= 20:
        try:
            rsi = ta.RSI(c.astype(float), timeperiod=14)
            if (h[-1] >= max(h[-5:-1])             # price at new 5-bar high
                    and rsi[-1] < max(rsi[-5:-1])):  # RSI lower
                sigs.append("RSI divergence")
        except Exception:
            pass

    # Volume dry-up on last 3 up-candles
    if v is not None and len(df) >= 4:
        try:
            recent = df.tail(4)
            up_bars = recent[recent["Close"] > recent["Open"]]
            if len(up_bars) >= 3:
                vols = up_bars["Volume"].values
                if vols[-1] < vols[-2] < vols[-3]:
                    sigs.append("Volume dry-up (distribution)")
        except Exception:
            pass

    # Stochastic overbought + bearish crossover
    if HAS_TALIB and len(c) >= 20:
        try:
            slowk, slowd = ta.STOCH(h.astype(float), df["Low"].values.astype(float),
                                    c.astype(float))
            if slowk[-2] > 80 and slowk[-1] < slowd[-1] and slowk[-2] >= slowd[-2]:
                sigs.append("Stochastic OB + bearish cross")
        except Exception:
            pass

    # MACD bearish crossover
    if HAS_TALIB and len(c) >= 35:
        try:
            macd, sig_line, _ = ta.MACD(c.astype(float))
            if macd[-1] < sig_line[-1] and macd[-2] >= sig_line[-2]:
                sigs.append("MACD bearish crossover")
        except Exception:
            pass

    # Bollinger Band upper touch
    if HAS_TALIB and len(c) >= 20:
        try:
            upper, _, _ = ta.BBANDS(c.astype(float), timeperiod=20)
            if c[-1] >= upper[-1]:
                sigs.append("BB upper band touch")
        except Exception:
            pass

    # Failed breakout — price closed back below resistance after piercing it
    if len(h) >= 3:
        try:
            res = float(df.attrs.get("resistance", 0))
            if res > 0 and h[-1] > res and c[-1] < res:
                sigs.append("Failed breakout (close below resistance)")
        except Exception:
            pass

    return sigs


def check_reversal(symbol: str, kc, resistance: float = 0) -> dict:
    """Fetch 5-min candles from Kite and check for bearish reversal signals."""
    try:
        instruments = kc.instruments("NSE")
        inst = next(
            (i for i in instruments
             if i["tradingsymbol"] == symbol and i.get("instrument_type") == "EQ"),
            None,
        )
        if not inst:
            return {"patterns": [], "extra_signals": [], "confirmed": False, "error": "instrument not found"}

        now  = datetime.datetime.now()
        from_ = now - datetime.timedelta(days=3)
        candles = kc.historical_data(inst["instrument_token"], from_, now, "5minute")
        if len(candles) < 6:
            return {"patterns": [], "extra_signals": [], "confirmed": False, "error": "not enough candles"}

        df = pd.DataFrame(candles)
        df = df.rename(columns={"open": "Open", "high": "High",
                                 "low": "Low", "close": "Close", "volume": "Volume"})
        df.attrs["resistance"] = resistance

        o = df["Open"].tolist()
        h = df["High"].tolist()
        l = df["Low"].tolist()
        c = df["Close"].tolist()

        if HAS_TALIB:
            patterns = _patterns_talib(o, h, l, c)
        else:
            patterns = _patterns_pandas_ta(df)

        extra = _extra_signals(df)
        confirmed = bool(patterns) or len(extra) >= 2

        return {
            "patterns":      patterns,
            "extra_signals": extra,
            "confirmed":     confirmed,
            "candles":       len(candles),
        }
    except Exception as e:
        log.warning("check_reversal %s: %s", symbol, e)
        return {"patterns": [], "extra_signals": [], "confirmed": False, "error": str(e)[:120]}


# ── full run (called from backend endpoint) ───────────────────────────────────

def run(kc=None) -> list[dict]:
    """Scan for near-resistance stocks, then verify with Kite 5-min patterns."""
    candidates = scan_near_resistance()
    if kc is None:
        return candidates          # return proximity list without pattern check

    for c in candidates:
        rev = check_reversal(c["symbol"], kc, resistance=c["resistance"])
        c["patterns"]      = rev.get("patterns", [])
        c["extra_signals"] = rev.get("extra_signals", [])
        c["confirmed"]     = rev.get("confirmed", False)
        c["error"]         = rev.get("error")

    # confirmed ones first, then sort by closeness to resistance
    candidates.sort(key=lambda x: (not x["confirmed"], x["pct_from_res"]))
    return candidates
