"""Simulate our live strategy's entry/SL/target (TD_ENTRY_OFFSET=0.3%, TD_SL=0.5%,
TD_TARGET=0.3%) as a SELL fired at R1, for stocks that actually touched R1 today.
Walks forward through subsequent 5-min candles to see whether SL or TARGET hits
first. Conservative: if both are breached in the same candle, counts as SL (loss)."""
import datetime
import zerodha as Z
import scanner as SC

SYMS = [s for s in SC.TRADING_LIST if s not in ("HCLTECH","WIPRO","TECHM","TATAMOTORS")]

SL_PCT = 0.005
TARGET_PCT = 0.003
TD_PULLBACK = 0.001  # matches the live app: 0.1% retrace off the running peak before entry confirms
kc = Z.kite()
today = datetime.date.today()

def simulate(candles, r1):
    # SELL at resistance, matching the live app's actual logic: once price reaches
    # R1, keep tracking the running peak going forward (not a fixed offset above
    # R1) — entry only confirms once price pulls back 0.1% off that peak.
    peak = None
    for i, c in enumerate(candles):
        if peak is None:
            if c["high"] < r1:
                continue
            peak = c["high"]
            continue  # this candle set the peak — can't also confirm a pullback from it
        if c["high"] > peak:
            peak = c["high"]
            continue  # this candle extended the peak — same reasoning
        # candle made no new high — safe to treat its low as a genuine pullback check
        pullback_level = peak * (1 - TD_PULLBACK)
        if c["low"] > pullback_level:
            continue
        entry = pullback_level
        sl = entry * (1 + SL_PCT)
        target = entry * (1 - TARGET_PCT)
        entry_time = c["date"].strftime("%H:%M")
        for w in candles[i:]:
            if w["high"] >= sl:
                return entry, sl, target, "LOSS (SL hit)", entry_time, w["date"].strftime("%H:%M")
            if w["low"] <= target:
                return entry, sl, target, "WIN (target hit)", entry_time, w["date"].strftime("%H:%M")
        return entry, sl, target, "still open at day end", entry_time, "-"
    return None, None, None, "R1 never reached / pullback never confirmed", None, None

def simulate_buy(candles, r1):
    # BUY breakout — the reference day closed strong (upper part of its range), so
    # treat R1 as a launchpad, not resistance: once price breaks above R1, track the
    # post-breakout trough, buy once price bounces 0.1% off that trough.
    trough = None
    broke_out = False
    for i, c in enumerate(candles):
        if not broke_out:
            if c["high"] < r1:
                continue
            broke_out = True
            trough = c["low"]
            continue
        if c["low"] < trough:
            trough = c["low"]
            continue
        bounce_level = trough * (1 + TD_PULLBACK)
        if c["high"] < bounce_level:
            continue
        entry = bounce_level
        sl = entry * (1 - SL_PCT)
        target = entry * (1 + TARGET_PCT)
        entry_time = c["date"].strftime("%H:%M")
        for w in candles[i:]:
            if w["low"] <= sl:
                return entry, sl, target, "LOSS (SL hit)", entry_time, w["date"].strftime("%H:%M")
            if w["high"] >= target:
                return entry, sl, target, "WIN (target hit)", entry_time, w["date"].strftime("%H:%M")
        return entry, sl, target, "still open at day end", entry_time, "-"
    return None, None, None, "R1 never reached / bounce never confirmed", None, None

rows = []
wins=losses=0
for sym in SYMS:
    try:
        q = kc.ltp(f"NSE:{sym}")[f"NSE:{sym}"]
        tok = q["instrument_token"]
        hist = kc.historical_data(tok, today - datetime.timedelta(days=10), today - datetime.timedelta(days=1), "day")
        target_day = hist[-1]   # most recent completed trading day = "yesterday"
        y = hist[-2]            # the trading day before that, used for R1
        H, L, C = y["high"], y["low"], y["close"]
        PP = (H + L + C) / 3
        R1 = 2*PP - L
        bullish = (C - L) / (H - L) > 0.7 if H != L else False
        d = target_day["date"].date()
        candles = kc.historical_data(tok, d, d, "minute")
        if bullish:
            entry, sl, target, result, entry_time, exit_time = simulate_buy(candles, R1)
            side = "BUY"
        else:
            entry, sl, target, result, entry_time, exit_time = simulate(candles, R1)
            side = "SELL"
        rows.append((sym, side, R1, entry, sl, target, entry_time, exit_time, result))
        if "WIN" in result: wins+=1
        elif "LOSS" in result: losses+=1
    except Exception as e:
        print(f"{sym:<12} ERROR: {e}")

print(f"{'SYM':<12}{'side':<5}{'R1':>9}{'entry':>9}{'SL':>9}{'target':>9}  {'entry@':>8}{'exit@':>8}   result")
for sym,side,R1,entry,sl,target,et,xt,res in rows:
    if entry is None:
        print(f"{sym:<12}{side:<5}{R1:>9.1f}{'':>9}{'':>9}{'':>9}  {'':>8}{'':>8}   {res}")
    else:
        print(f"{sym:<12}{side:<5}{R1:>9.1f}{entry:>9.1f}{sl:>9.1f}{target:>9.1f}  {et:>8}{xt:>8}   {res}")
print(f"\nTotal yesterday: {wins} wins, {losses} losses")

print("\n--- LOSERS ---")
for sym,side,R1,entry,sl,target,et,xt,res in rows:
    if "LOSS" in res:
        print(f"{sym:<12} {side}  entry={entry:.1f}  SL={sl:.1f}  entry@{et}  exit@{xt}")
