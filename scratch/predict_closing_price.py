import datetime as dt
import pandas as pd
import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import zerodha as Z

def predict_closing():
    print("Connecting to Zerodha Kite client...")
    kc = Z.kite()
    
    today = dt.date.today()
    nifty_token = 256265
    
    # Fetch Nifty Spot 1-minute candles from 15:00 to 15:07 PM
    s_dt = dt.datetime.combine(today, dt.time(15, 0))
    e_dt = dt.datetime.now()
    
    print(f"Fetching closing window candles from {s_dt.strftime('%H:%M:%S')} to {e_dt.strftime('%H:%M:%S')}...")
    candles = kc.historical_data(nifty_token, s_dt, e_dt, "minute")
    
    if not candles:
        # Fallback: get current quote
        quote = kc.quote(["NSE:NIFTY 50"])
        ltp = quote.get("NSE:NIFTY 50", {}).get("last_price")
        print(f"No historical candles available yet for 3 PM window. Current Spot LTP: {ltp:.2f}")
        return
        
    df = pd.DataFrame(candles)
    df['date'] = pd.to_datetime(df['date'])
    
    # Calculate simple average of the closing window so far
    avg_price = df['close'].mean()
    current_ltp = df.iloc[-1]['close']
    
    print("\n" + "="*50)
    print("🔮 NIFTY DAILY CLOSE PREDICTOR (3:00 - 3:30 PM)")
    print("="*50)
    print(f"  ▸ Current Spot LTP:       {current_ltp:.2f}")
    print(f"  ▸ Number of Candles:      {len(df)} min of data")
    print(f"  ▸ Average Price So Far:   {avg_price:.2f}")
    print(f"  🔥 PROJECTED DAILY CLOSE: {avg_price:.2f} (±15 pts depending on final 20 mins)")
    print("="*50)

if __name__ == "__main__":
    predict_closing()
