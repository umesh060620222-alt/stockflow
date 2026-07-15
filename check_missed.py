import yfinance as yf
import datetime as dt

def main():
    print("Downloading Nifty Spot 1-minute candles for today...")
    df = yf.download("^NSEI", period="1d", interval="1m")
    if df.empty:
        print("Error: No data retrieved.")
        return
        
    df = df.between_time("09:45", "10:15")
    print(df)
    
    # Trade settings
    entry_spot = 24193.82
    atr = 7.60
    sl_points = 7.60
    target_points = 15.20
    
    sl = entry_spot - sl_points
    target = entry_spot + target_points
    trail_trigger = entry_spot + 0.30 * target_points
    
    print(f"\nTrade Parameters:")
    print(f"Entry Spot: {entry_spot:.2f}")
    print(f"Initial SL: {sl:.2f}")
    print(f"Target: {target:.2f}")
    print(f"Trail Trigger Point: {trail_trigger:.2f}")
    
    reached_trail = False
    current_sl = sl
    
    print("\nTracing price path:")
    # Iterate through candles starting from 09:46
    for idx, row in df.iterrows():
        time_str = idx.strftime("%H:%M")
        if time_str < "09:46":
            continue
            
        low = float(row['Low'])
        high = float(row['High'])
        close = float(row['Close'])
        
        # Check trail trigger
        if not reached_trail and high >= trail_trigger:
            reached_trail = True
            current_sl = entry_spot
            print(f"[{time_str}] Nifty High hit {high:.2f} (crossed trail trigger {trail_trigger:.2f}). SL trailed to entry {current_sl:.2f}")
            
        # Check SL hit
        if low <= current_sl:
            verdict = "BREAKEVEN" if reached_trail else "LOSS"
            print(f"[{time_str}] Nifty Low hit {low:.2f} (breached SL {current_sl:.2f}). Trade closed as {verdict}!")
            return
            
        # Check Target hit
        if high >= target:
            print(f"[{time_str}] Nifty High hit {high:.2f} (breached target {target:.2f}). Trade closed as WIN!")
            return
            
    print("\nTrade is still open at 10:15!")

if __name__ == "__main__":
    main()
