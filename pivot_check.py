"""One-off check: compute classic pivot points from yesterday's OHLC for a basket
of stocks, then check today's actual 5-min candles to see whether R1/S1 acted as
real resistance/support (price approached within 0.15% and reversed >=0.2%
within the next 6 candles / 30 min)."""
import datetime, sys
import zerodha as Z

SYMS = ["ICICIBANK","AXISBANK","SBIN","HCLTECH","HDFCBANK",
        "TATASTEEL","BAJFINANCE","VEDL","DLF","KOTAKBANK"]

BUFFER = float(sys.argv[1]) if len(sys.argv) > 1 else 0.0015

kc = Z.kite()
today = datetime.date.today()

def check_level(candles, level, is_resistance):
    """Did price come within BUFFER of level and then reverse >=0.2% within 6 candles?"""
    tol = level * BUFFER
    for i, c in enumerate(candles):
        near = (abs(c["high"] - level) <= tol) if is_resistance else (abs(c["low"] - level) <= tol)
        touched = (c["high"] >= level - tol) if is_resistance else (c["low"] <= level + tol)
        if not (near or touched):
            continue
        window = candles[i:i+6]
        if len(window) < 2:
            continue
        if is_resistance:
            worst = max(w["high"] for w in window)
            after_low = min(w["low"] for w in window[1:])
            if worst <= level * 1.003 and (level - after_low) / level >= 0.002:
                return "HELD (reversed)"
            if worst > level * 1.003:
                return "BROKEN"
        else:
            worst = min(w["low"] for w in window)
            after_high = max(w["high"] for w in window[1:])
            if worst >= level * 0.997 and (after_high - level) / level >= 0.002:
                return "HELD (reversed)"
            if worst < level * 0.997:
                return "BROKEN"
    return "not tested"

rows = []
for sym in SYMS:
    try:
        q = kc.ltp(f"NSE:{sym}")[f"NSE:{sym}"]
        tok = q["instrument_token"]
        hist = kc.historical_data(tok, today - datetime.timedelta(days=7), today - datetime.timedelta(days=1), "day")
        y = hist[-1]
        H, L, C = y["high"], y["low"], y["close"]
        PP = (H + L + C) / 3
        R1 = 2*PP - L
        S1 = 2*PP - H
        candles = kc.historical_data(tok, today, today, "5minute")
        if not candles:
            rows.append((sym, PP, R1, S1, "no data", "no data"))
            continue
        r1_result = check_level(candles, R1, True)
        s1_result = check_level(candles, S1, False)
        rows.append((sym, round(PP,1), round(R1,1), round(S1,1), r1_result, s1_result))
    except Exception as e:
        rows.append((sym, None, None, None, f"ERROR: {e}", ""))

print(f"{'SYM':<12}{'PP':>8}{'R1':>8}{'S1':>8}   {'R1 result':<16}{'S1 result'}")
for r in rows:
    sym, pp, r1, s1, r1r, s1r = r
    print(f"{sym:<12}{str(pp):>8}{str(r1):>8}{str(s1):>8}   {r1r:<16}{s1r}")
