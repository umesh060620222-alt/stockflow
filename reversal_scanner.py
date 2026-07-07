"""Resistance reversal scanner.

Flow:
  1. scan_near_resistance() — fetch daily OHLCV via yfinance, find stocks within
     NEAR_PCT% of their 52-week high, with volume > 20d avg AND RSI(14) > 65.
  2. check_reversal(symbol, kc) — fetch latest 5-min candles from Kite, run
     TA-Lib (preferred) or pandas-ta candlestick pattern detection.
  3. Returns confirmed bearish-reversal candidates ready to flash as SELL.

Entry filters (ALL must pass):
  - Within NEAR_PCT% of the 52-week high
  - Today's volume > 20-day average volume  (participation present)
  - RSI(14) > 65  (overbought zone, momentum stretched)

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
import zerodha as Z

NEAR_PCT   = 0.50   # within this % of 52-week high → candidate
SCAN_TOP   = 40     # how many stocks from WATCHLIST_DAILY to scan daily
RSI_MIN    = 65     # RSI(14) must be above this (overbought / momentum stretched)
VOL_RATIO  = 1.0    # today's volume must be >= this × 20d avg volume

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
    """52-week high is the only resistance we track."""
    levels: dict[str, float] = {}
    if len(df) >= 200:   # need ~200 trading days for a clean 52w high
        levels["52w_high"] = float(df["High"].tail(252).max())
    elif len(df) >= 60:  # fallback for newer listings: use whatever history we have
        levels["52w_high"] = float(df["High"].max())
    return levels


def _rsi(close: "np.ndarray", period: int = 14) -> float:
    """Simple RSI without TA-Lib dependency."""
    if HAS_TALIB:
        r = ta.RSI(close.astype(float), timeperiod=period)
        v = r[~np.isnan(r)]
        return float(v[-1]) if len(v) else float("nan")
    # Wilder's smoothed RS
    deltas = np.diff(close[-period * 3:].astype(float))
    gains  = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_g  = gains[-period:].mean()
    avg_l  = losses[-period:].mean()
    if avg_l == 0:
        return 100.0
    return float(100 - 100 / (1 + avg_g / avg_l))


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

def _kite_daily(kc, symbols: list[str], days: int = 400) -> dict[str, pd.DataFrame]:
    """Fetch daily OHLCV for each symbol via Kite historical API.
    Returns {symbol: DataFrame(Open,High,Low,Close,Volume)}."""
    imap   = Z.instrument_map(kc)
    to_dt  = datetime.datetime.now()
    from_dt = to_dt - datetime.timedelta(days=days + 5)  # pad for holidays
    out: dict[str, pd.DataFrame] = {}
    for sym in symbols:
        tok = imap.get(sym)
        if not tok:
            log.debug("no token for %s", sym)
            continue
        try:
            rows = kc.historical_data(tok, from_dt, to_dt, "day")
            if not rows:
                continue
            df = pd.DataFrame(rows)
            df = df.rename(columns={"open": "Open", "high": "High",
                                    "low": "Low", "close": "Close", "volume": "Volume"})
            df["date"] = pd.to_datetime(df["date"])
            df = df.set_index("date").sort_index()
            out[sym] = df
        except Exception as e:
            log.debug("kite daily %s: %s", sym, e)
    return out


def scan_near_resistance(kc, symbols: list[str] | None = None) -> list[dict]:
    """Return stocks near their 52w high with volume > 20d avg AND RSI(14) > 65.
    Uses Kite historical API for accurate, real-time daily data."""
    if symbols is None:
        symbols = WATCHLIST_DAILY[:SCAN_TOP]

    daily = _kite_daily(kc, symbols, days=400)

    out: list[dict] = []
    for sym, df in daily.items():
        try:
            if len(df) < 20:
                continue

            ltp     = float(df["Close"].iloc[-1])
            vol_now = float(df["Volume"].iloc[-1])
            vol20   = float(df["Volume"].tail(21).iloc[:-1].mean())  # 20d avg excl. today

            if vol20 > 0 and vol_now < VOL_RATIO * vol20:
                continue

            rsi_val = _rsi(df["Close"].values)
            if np.isnan(rsi_val) or rsi_val < RSI_MIN:
                continue

            levels = _resistance_levels(df)
            res_name, res_level = _nearest_resistance(ltp, levels)
            if not res_name:
                continue

            out.append({
                "symbol":          sym,
                "ltp":             round(ltp, 2),
                "resistance":      round(res_level, 2),
                "resistance_type": "52w high",
                "pct_from_res":    round((res_level - ltp) / res_level * 100, 2),
                "rsi":             round(rsi_val, 1),
                "vol_ratio":       round(vol_now / vol20, 2) if vol20 > 0 else None,
                "patterns":        [],
                "extra_signals":   [],
                "confirmed":       False,
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


def check_reversal(symbol: str, kc, resistance: float = 0,
                   imap: dict | None = None) -> dict:
    """Fetch 5-min candles from Kite and check for bearish reversal signals."""
    try:
        if imap is None:
            imap = Z.instrument_map(kc)
        tok = imap.get(symbol)
        if not tok:
            return {"patterns": [], "extra_signals": [], "confirmed": False, "error": "instrument not found"}

        now  = datetime.datetime.now()
        from_ = now - datetime.timedelta(days=3)
        candles = kc.historical_data(tok, from_, now, "5minute")
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

def run(kc) -> list[dict]:
    """Scan for near-resistance stocks via Kite daily data, then verify with
    Kite 5-min candlestick patterns.  Always requires an authenticated kc."""
    imap       = Z.instrument_map(kc)   # fetch once, reuse for both daily + 5-min
    candidates = scan_near_resistance(kc, symbols=None)

    for c in candidates:
        rev = check_reversal(c["symbol"], kc, resistance=c["resistance"], imap=imap)
        c["patterns"]      = rev.get("patterns", [])
        c["extra_signals"] = rev.get("extra_signals", [])
        c["confirmed"]     = rev.get("confirmed", False)
        c["error"]         = rev.get("error")

    candidates.sort(key=lambda x: (not x["confirmed"], x["pct_from_res"]))
    return candidates
