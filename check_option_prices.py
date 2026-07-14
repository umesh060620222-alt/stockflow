import sys
import os

# Ensure stockflow import path is active
sys.path.append("/home/ubuntu/stockflow")

import zerodha as Z
import datetime as dt
import pytz

def check_prices():
    print("Connecting to Zerodha...")
    try:
        kc = Z.kite()
    except Exception as e:
        print(f"Connection failed: {e}")
        return

    print("Fetching NFO instruments...")
    insts = Z.get_nfo_instruments(kc)
    
    def get_token_for(symbol):
        parts = symbol.split()
        strike = parts[-2]
        opt_type = parts[-1]
        
        for i in insts:
            if i["name"] == "NIFTY":
                exp = i.get("expiry")
                if exp:
                    if isinstance(exp, str):
                        exp_date = dt.datetime.strptime(exp, "%Y-%m-%d").date()
                    else:
                        exp_date = exp
                    if exp_date.day == 14 and exp_date.month == 7 and exp_date.year == 2026:
                        if abs(float(i["strike"]) - float(strike)) < 0.1 and i["instrument_type"] == opt_type:
                            return i
        return None

    targets = [
        ("NIFTY 14 JUL 24150 PE", "10:01", "10:01"),
        ("NIFTY 14 JUL 24050 PE", "10:23", "10:33"),
        ("NIFTY 14 JUL 24100 CE", "10:40", "11:25")
    ]

    for sym, entry_t, exit_t in targets:
        matched = get_token_for(sym)
        if not matched:
            print(f"Could not find contract token for {sym}")
            continue
        token = matched["instrument_token"]
        tradingsymbol = matched["tradingsymbol"]
        print(f"\n--- {sym} (Token: {token}, Symbol: {tradingsymbol}) ---")
        
        # Fetch historical data for today
        to_d = dt.datetime.now()
        from_d = to_d - dt.timedelta(days=1)
        
        try:
            rows = kc.historical_data(token, from_d, to_d, "minute")
            if not rows:
                print("No historical candles returned.")
                continue
                
            for r in rows:
                ts = r["date"].astimezone(pytz.timezone("Asia/Kolkata"))
                t_str = ts.strftime("%H:%M")
                if t_str in (entry_t, exit_t):
                    print(f"[{t_str}] Open: {r['open']:.2f}, High: {r['high']:.2f}, Low: {r['low']:.2f}, Close: {r['close']:.2f}")
        except Exception as e:
            print(f"Failed to fetch historical data: {e}")

if __name__ == "__main__":
    check_prices()
