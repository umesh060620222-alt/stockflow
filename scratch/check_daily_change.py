import datetime as dt
import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import zerodha as Z

def check_change():
    print("Connecting to Zerodha Kite client...")
    kc = Z.kite()
    
    nifty_token = 256265
    today = dt.date.today()
    
    # 1. Fetch yesterday's Nifty Spot close
    yesterday = today - dt.timedelta(days=1)
    while yesterday.weekday() >= 5:
        yesterday -= dt.timedelta(days=1)
        
    y_candles = kc.historical_data(nifty_token, yesterday, yesterday, "day")
    if not y_candles:
        # fallback
        print("Error: Could not retrieve yesterday close.")
        return
    yesterday_close = y_candles[0]['close']
    
    # 2. Fetch today's current Nifty Spot LTP
    quote = kc.quote(["NSE:NIFTY 50"])
    spot_ltp = quote.get("NSE:NIFTY 50", {}).get("last_price")
    
    if not spot_ltp:
        print("Error: Could not retrieve Nifty Spot LTP.")
        return
        
    change_pts = spot_ltp - yesterday_close
    change_pct = (change_pts / yesterday_close) * 100.0
    
    print("\n" + "="*50)
    print("📊 NIFTY DAILY CHANGE REPORT")
    print("="*50)
    print(f"  ▸ Yesterday Close:  {yesterday_close:.2f}")
    print(f"  ▸ Today Spot LTP:     {spot_ltp:.2f}")
    print(f"  ▸ Net Change:         {change_pts:+.2f} points ({change_pct:+.2f}%)")
    
    if change_pts > 0:
        print("\n  🔥 NIFTY IS UP TODAY! 🟢")
    else:
        print("\n  ❄️ NIFTY IS DOWN TODAY! 🔴")
    print("="*50)

if __name__ == "__main__":
    check_change()
