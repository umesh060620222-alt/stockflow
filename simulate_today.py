import datetime as dt
import pandas as pd

def main():
    print("Running today's Nifty options simulation...")
    import zerodha
    kc = zerodha.kite()
    
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
        print("No Nifty Spot data retrieved.")
        return
        
    df = pd.DataFrame(data)
    df.index = pd.to_datetime(df['date'])
    df.index = df.index.tz_localize(None) # Remove timezone for ease
    
    # Calculate indicators
    df['prev_close'] = df['close'].shift(1)
    df['tr'] = df.apply(
        lambda r: max(
            r['high'] - r['low'],
            abs(r['high'] - r['prev_close']) if not pd.isna(r['prev_close']) else 0,
            abs(r['low'] - r['prev_close']) if not pd.isna(r['prev_close']) else 0
        ),
        axis=1
    )
    df['atr'] = df['tr'].rolling(window=14).mean()
    df['ema'] = df['close'].ewm(span=20, adjust=False).mean()
    
    nifty_open = float(df['open'].iloc[0])
    print(f"Nifty Open: {nifty_open:.2f}")
    
    # Run the exact options strategy logic
    candles = df.to_dict("records")
    
    # Long state machine
    l_stage = 1
    l_peak = None
    l_entry_price = None
    l_trigger_time = None
    
    # Short state machine
    s_stage = 1
    s_trough = None
    s_entry_price = None
    s_trigger_time = None
    
    sim_trades = []
    
    print("\n--- SIMULATED TRADES TODAY ---")
    
    for row in candles:
        ts = row['date']
        time_str = ts.strftime("%H:%M")
        
        # Pull values
        high = float(row['high'])
        low = float(row['low'])
        close = float(row['close'])
        nifty_ema = float(row['ema'])
        atr = float(row['atr'])
        
        # Check start time (09:25 AM to 15:30 PM)
        is_valid_time = "09:25" <= time_str < "15:30"
        
        is_nifty_above_ema = close > nifty_ema
        is_nifty_below_ema = close < nifty_ema
        is_nifty_green_today = close > nifty_open
        is_nifty_red_today = close < nifty_open
        
        # ─── LONG SETUP ───
        if l_stage == 1:
            if is_valid_time and is_nifty_above_ema and is_nifty_green_today:
                if l_peak is None or high > l_peak:
                    l_peak = high
                pullback_target = l_peak - (1.0 * atr)
                if low <= pullback_target:
                    l_stage = 2
                    l_trigger_time = time_str
                    l_entry_price = pullback_target
        elif l_stage == 2:
            if close > nifty_ema:
                sim_trades.append({
                    "time": l_trigger_time,
                    "side": "BUY CALL (CE)",
                    "entry_spot": l_entry_price,
                    "atr": atr,
                    "ema": nifty_ema
                })
                # Reset
                l_stage = 1
                l_peak = None
            elif close <= nifty_ema:
                # Cancel setup if it closes below EMA
                l_stage = 1
                l_peak = None
                
        # ─── SHORT SETUP ───
        if s_stage == 1:
            if is_valid_time and is_nifty_below_ema and is_nifty_red_today:
                if s_trough is None or low < s_trough:
                    s_trough = low
                pullback_target = s_trough + (1.0 * atr)
                if high >= pullback_target:
                    s_stage = 2
                    s_trigger_time = time_str
                    s_entry_price = pullback_target
        elif s_stage == 2:
            if close < nifty_ema:
                sim_trades.append({
                    "time": s_trigger_time,
                    "side": "BUY PUT (PE)",
                    "entry_spot": s_entry_price,
                    "atr": atr,
                    "ema": nifty_ema
                })
                # Reset
                s_stage = 1
                s_trough = None
            elif close >= nifty_ema:
                # Cancel setup if it closes above EMA
                s_stage = 1
                s_trough = None
                
    for t in sim_trades:
        print(f"[{t['time']}] {t['side']} Entry Target: {t['entry_spot']:.2f} (ATR={t['atr']:.2f}, EMA={t['ema']:.2f})")

if __name__ == "__main__":
    main()
