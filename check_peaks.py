import datetime as dt
import pandas as pd

def main():
    import zerodha
    kc = zerodha.kite()
    today = dt.date.today()
    try:
        data = kc.historical_data(256265, today, today, "minute")
    except Exception as e:
        print(f"Error: {e}")
        return
        
    df = pd.DataFrame(data)
    df.index = pd.to_datetime(df['date'])
    
    # Localize index to timezone-naive for between_time
    df.index = df.index.tz_localize(None)
    
    # Nifty Spot trading window since 09:25 AM
    df_window = df.between_time('09:25', '12:15')
    
    max_high = df_window['high'].max()
    min_low = df_window['low'].min()
    
    print(f"Max High: {max_high:.2f}")
    print(f"Min Low: {min_low:.2f}")

if __name__ == "__main__":
    main()
