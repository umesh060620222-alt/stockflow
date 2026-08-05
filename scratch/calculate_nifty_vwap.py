import datetime as dt
import pandas as pd
import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import zerodha as Z

def calculate_vwap():
    print("Connecting to Zerodha Kite client...")
    kc = Z.kite()
    
    today = dt.date.today()
    
    # 1. Resolve Nifty Futures token (nearest expiry)
    instruments = kc.instruments("NFO")
    nifty_futs = [i for i in instruments if i.get("name") == "NIFTY" and i.get("instrument_type") == "FUT"]
    if not nifty_futs:
        print("Error: Could not resolve Nifty Futures instruments.")
        return
        
    nifty_futs = sorted(nifty_futs, key=lambda x: x.get("expiry"))
    target_fut = nifty_futs[0]
    fut_token = int(target_fut["instrument_token"])
    fut_symbol = target_fut["tradingsymbol"]
    
    # 2. Fetch today's 1-minute candles
    s_dt = dt.datetime.combine(today, dt.time(9, 15))
    e_dt = dt.datetime.now()
    
    print(f"Fetching 1-minute candles for {fut_symbol} (Token: {fut_token}) from {s_dt} to {e_dt}...")
    candles = kc.historical_data(fut_token, s_dt, e_dt, "minute")
    if not candles:
        print("Error: No candle data returned.")
        return
        
    df = pd.DataFrame(candles)
    
    # 3. Calculate VWAP
    # VWAP = Sum(Typical Price * Volume) / Sum(Volume)
    # Typical Price = (High + Low + Close) / 3
    df['typical_price'] = (df['high'] + df['low'] + df['close']) / 3.0
    df['pv'] = df['typical_price'] * df['volume']
    
    total_pv = df['pv'].sum()
    total_volume = df['volume'].sum()
    
    if total_volume == 0:
        print("Error: Total volume is zero. Cannot calculate VWAP.")
        return
        
    vwap = total_pv / total_volume
    current_ltp = df.iloc[-1]['close']
    
    # Also fetch Nifty Spot LTP for reference
    spot_quote = kc.quote(["NSE:NIFTY 50"])
    spot_ltp = spot_quote.get("NSE:NIFTY 50", {}).get("last_price")
    
    print("\n" + "="*50)
    print("📈 NIFTY INTRADAY METRICS (REAL-TIME)")
    print("="*50)
    print(f"  ▸ Nifty Spot LTP:        {spot_ltp:.2f}")
    print(f"  ▸ Nifty Futures LTP:     {current_ltp:.2f} ({fut_symbol})")
    print(f"  ▸ Total Volume:          {total_volume:,.0f} contracts")
    print(f"  🔥 CURRENT VWAP:         {vwap:.2f}")
    print("="*50)

if __name__ == "__main__":
    calculate_vwap()
