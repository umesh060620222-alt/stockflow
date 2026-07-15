import datetime as dt

def main():
    print("Fetching Nifty Spot candles from Zerodha...")
    import zerodha
    kc = zerodha.kite()
    
    # Nifty Spot instrument token is 256265 (NSE Spot)
    today = dt.date.today()
    try:
        data = kc.historical_data(
            instrument_token=256265,
            from_date=today,
            to_date=today,
            interval="minute"
        )
    except Exception as e:
        print(f"Error fetching historical data: {e}")
        return
        
    if not data:
        print("No Nifty Spot data retrieved from Zerodha.")
        return
        
    print(f"Retrieved {len(data)} candles.")
    
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
    
    for row in data:
        time_str = row['date'].strftime("%H:%M")
        if time_str < "09:46":
            continue
            
        low = float(row['low'])
        high = float(row['high'])
        close = float(row['close'])
        
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
            
    print("\nTrade is still open!")

if __name__ == "__main__":
    main()
