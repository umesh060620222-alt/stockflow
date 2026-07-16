import datetime as dt
import pandas as pd

def main():
    import zerodha
    kc = zerodha.kite()
    
    # Fetch Nifty Spot historical data (1-minute candles)
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
        
    if len(data) < 15:
        print("Not enough candles today to calculate 14-period ATR.")
        return
        
    df = pd.DataFrame(data)
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
    
    current_atr = df['atr'].iloc[-1]
    current_ltp = df['close'].iloc[-1]
    
    print(f"NIFTY_SPOT_LTP={current_ltp:.2f}")
    print(f"NIFTY_SPOT_ATR={current_atr:.2f}")

if __name__ == "__main__":
    main()
