"""Signals + entry/exit rules for the ~10-minute momentum capture.

Indicators are computed per session (VWAP and returns reset each day). The entry
is a state-change: volume surge + price on the right side of VWAP + momentum +
relative strength. Exit is target / stop / time / VWAP stall.
"""
from __future__ import annotations
import pandas as pd
import config


def add_indicators(df: pd.DataFrame, bench_ret: pd.Series | None) -> pd.DataFrame:
    """Per-session VWAP, rolling avg volume, momentum, return-since-open, and
    relative strength vs the benchmark (aligned by timestamp)."""
    df = df.copy()
    out = []
    for _, day in df.groupby("date"):
        d = day.copy()
        tp = (d["high"] + d["low"] + d["close"]) / 3.0
        cum_v = d["volume"].cumsum().replace(0, pd.NA)
        d["vwap"] = (tp * d["volume"]).cumsum() / cum_v
        d["vwap"] = d["vwap"].ffill().fillna(d["close"])
        d["vol_avg"] = d["volume"].rolling(config.VOL_AVG_BARS, min_periods=3).mean()
        d["mom"] = d["close"].pct_change(config.MOM_LOOKBACK)
        d["ret_open"] = d["close"] / d["close"].iloc[0] - 1.0
        d["bar_idx"] = range(len(d))
        out.append(d)
    res = pd.concat(out)
    if bench_ret is not None:
        res["bench_ret"] = bench_ret.reindex(res.index).ffill()
    else:
        res["bench_ret"] = 0.0
    res["rs"] = res["ret_open"] - res["bench_ret"]
    return res


def entry_signal(row) -> str | None:
    """Return 'long', 'short', or None for this bar (evaluated on bar close)."""
    if row["bar_idx"] < config.SKIP_OPEN_BARS:
        return None
    if pd.isna(row["vol_avg"]) or row["vol_avg"] <= 0:
        return None
    vol_surge = row["volume"] > config.VOL_MULT * row["vol_avg"]
    if not vol_surge:
        return None

    if config.MODE == "meanrev":
        # fade over-extension: expect price to revert toward VWAP
        dev = (row["close"] - row["vwap"]) / row["vwap"]
        if dev <= -config.DEVIATION_PCT:   # stretched below VWAP -> buy the revert up
            return "long"
        if dev >= config.DEVIATION_PCT:     # stretched above VWAP -> sell the revert down
            return "short"
        return None

    # momentum: ride the move
    # long: above VWAP, positive momentum, outperforming Nifty
    if row["close"] > row["vwap"] and row["mom"] > 0 and row["rs"] > config.RS_MIN:
        return "long"
    # short: below VWAP, negative momentum, underperforming Nifty
    if row["close"] < row["vwap"] and row["mom"] < 0 and row["rs"] < -config.RS_MIN:
        return "short"
    return None


def check_exit(side, entry_price, entry_ts, bar, minutes_held) -> tuple[bool, float, str]:
    """Decide whether to exit on this bar. Returns (exit?, fill_price, reason).
    Uses bar high/low for target/stop touches (stop wins ties — conservative)."""
    if side == "long":
        target = entry_price * (1 + config.TARGET_PCT)
        stop = entry_price * (1 - config.STOP_PCT)
        if bar["low"] <= stop:
            return True, stop, "stop"
        if bar["high"] >= target:
            return True, target, "target"
        if config.USE_VWAP_STALL_EXIT and bar["close"] < bar["vwap"]:
            return True, bar["close"], "vwap_stall"
    else:  # short
        target = entry_price * (1 - config.TARGET_PCT)
        stop = entry_price * (1 + config.STOP_PCT)
        if bar["high"] >= stop:
            return True, stop, "stop"
        if bar["low"] <= target:
            return True, target, "target"
        if config.USE_VWAP_STALL_EXIT and bar["close"] > bar["vwap"]:
            return True, bar["close"], "vwap_stall"
    if minutes_held >= config.TIME_STOP_MIN:
        return True, bar["close"], "time"
    return False, 0.0, ""


def gross_pct(side, entry_price, exit_price) -> float:
    r = (exit_price - entry_price) / entry_price
    return r if side == "long" else -r


def net_pct(gross) -> float:
    """Subtract round-trip costs + slippage both sides."""
    return gross - config.COST_PCT - 2 * config.SLIPPAGE_PCT
