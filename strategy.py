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
    if bench_ret is not None:
        df["bench_ret"] = bench_ret.reindex(df.index).ffill()
    else:
        df["bench_ret"] = 0.0
        
    out = []
    for _, day in df.groupby("date"):
        d = day.copy()
        tp = (d["high"] + d["low"] + d["close"]) / 3.0
        cum_v = d["volume"].cumsum().replace(0, pd.NA)
        d["vwap"] = (tp * d["volume"]).cumsum() / cum_v
        d["vwap"] = d["vwap"].ffill().fillna(d["close"])
        d["vol_avg"] = d["volume"].rolling(config.VOL_AVG_BARS, min_periods=3).mean()
        d["vol_avg_prev"] = d["volume"].shift(1).rolling(config.VOL_AVG_BARS, min_periods=3).mean()
        d["mom"] = d["close"].pct_change(config.MOM_LOOKBACK)
        d["ret_open"] = d["close"] / d["close"].iloc[0] - 1.0
        d["bar_idx"] = range(len(d))
        
        # Calculate ATR(14)
        prev_close = d["close"].shift(1)
        tr = pd.concat([
            d["high"] - d["low"],
            (d["high"] - prev_close).abs(),
            (d["low"] - prev_close).abs()
        ], axis=1).max(axis=1)
        d["atr"] = tr.rolling(14, min_periods=7).mean().fillna(d["high"] - d["low"])
        
        # streak mode: consecutive bars where price rose
        up = d["close"] > d["close"].shift(1)
        blocks = (up != up.shift()).cumsum()
        run = up.groupby(blocks).cumcount() + 1
        d["streak"] = run.where(up, 0).astype(int)
        
        # ATR Pullback State Machine
        atr_signals = [None] * len(d)
        peak1 = None
        trough1 = None
        peak_atr = None
        stage = 1
        
        highs = d["high"].tolist()
        lows = d["low"].tolist()
        volumes = d["volume"].tolist()
        atrs = d["atr"].tolist()
        times = [t.strftime("%H:%M") for t in d.index]
        vol_avg_prevs = d["vol_avg_prev"].tolist()
        bench_rets = d["bench_ret"].tolist()
        
        for idx in range(len(d)):
            high = highs[idx]
            low = lows[idx]
            vol = volumes[idx]
            atr = atrs[idx]
            time_str = times[idx]
            vol_avg_prev = vol_avg_prevs[idx]
            bench_ret_val = bench_rets[idx]
            
            if stage == 1:
                if peak1 is None or high > peak1:
                    peak1 = high
                    peak_atr = atr
                else:
                    trough1 = low
                    stage = 2
                continue
                
            if stage == 2:
                if high > peak1:
                    peak1 = high
                    peak_atr = atr
                    trough1 = low
                    stage = 1
                    continue
                
                trough1 = min(trough1, low)
                drop_required = config.ATR_DROP_MULT * (peak_atr if peak_atr else atr)
                if trough1 <= peak1 - drop_required:
                    stage = 3
                continue
                
            if stage == 3:
                if low < trough1:
                    trough1 = low
                
                bounce_required = config.ATR_BOUNCE_MULT * atr
                bounce_level = trough1 + bounce_required
                if high >= bounce_level:
                    # 1. Time Filter (before 11:00 AM or at/after 2:00 PM IST)
                    is_valid_time = time_str < "11:00" or time_str >= "14:00"
                    
                    # 2. Volume Filter (> 1.5x previous 12-candle average)
                    is_valid_vol = (vol > config.VOL_MULT * vol_avg_prev) if (vol_avg_prev and vol_avg_prev > 0) else True
                    
                    # 3. Nifty Filter
                    is_nifty_green = True
                    if config.USE_NIFTY_FILTER:
                        is_nifty_green = bench_ret_val > 0
                        
                    if is_valid_time and is_valid_vol and is_nifty_green:
                        atr_signals[idx] = "long"
                        # Reset state machine to look for the next setup
                        peak1 = None
                        trough1 = None
                        peak_atr = None
                        stage = 1
                continue
        
        d["atr_signal"] = atr_signals
        out.append(d)
        
    res = pd.concat(out)
    res["rs"] = res["ret_open"] - res["bench_ret"]
    return res


def entry_signal(row) -> str | None:
    """Return 'long', 'short', or None for this bar (evaluated on bar close)."""
    if row["bar_idx"] < config.SKIP_OPEN_BARS:
        return None

    if config.MODE == "streak":
        # N consecutive price+volume rises -> BUY (matches the live streak rule)
        return "long" if row["streak"] >= config.LIVE_CONSEC_UPS else None

    if config.MODE == "atr_pullback":
        return "long" if row["atr_signal"] == "long" else None

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
