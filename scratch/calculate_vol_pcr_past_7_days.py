import datetime as dt
import pandas as pd
import numpy as np
import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import zerodha as Z

def run_backtest_7_days():
    print("Connecting to Zerodha Kite client...")
    kc = Z.kite()
    
    # We use the August Monthly Expiry which is active and highly liquid for all past 7 days
    # The monthly contract of August 2026 is August 25, 2026
    monthly_expiry = dt.date(2026, 8, 25)
    print(f"Resolving Nifty monthly options for expiry: {monthly_expiry}")
    
    # Strikes to track
    strikes = [24200, 24250, 24300, 24350]
    
    # Fetch NFO instruments cache
    insts = Z.get_nfo_instruments(kc)
    if not insts:
        print("Failed to fetch NFO instruments cache.")
        return
        
    option_tokens = {}
    for s in strikes:
        for opt_type in ["CE", "PE"]:
            token = Z.get_option_token(kc, "NIFTY", monthly_expiry, s, opt_type)
            if token:
                key = f"{s}_{opt_type}"
                option_tokens[key] = token
                
    if not option_tokens:
        print("Could not resolve monthly option tokens.")
        return
        
    print(f"Resolved tokens for {len(option_tokens)} monthly contracts.")
    
    # Get the past 7 trading days (excluding weekends)
    today = dt.date.today()
    trading_days = []
    current_date = today
    
    while len(trading_days) < 7:
        if current_date.weekday() < 5:  # Monday to Friday
            trading_days.append(current_date)
        current_date = current_date - dt.timedelta(days=1)
        
    trading_days.reverse()
    
    print("\n" + "="*80)
    print("📊 7-DAY VOLUME PCR EXTREMES REPORT (MONTHLY CONTRACT BASIS)")
    print("="*80)
    print(f"{'Date':<12} | {'Min PCR (Time)':<22} | {'Max PCR (Time)':<22} | {'Reversion Trend'}")
    print("-"*80)
    
    for day in trading_days:
        start_dt = dt.datetime.combine(day, dt.time(9, 15))
        end_dt = dt.datetime.combine(day, dt.time(15, 30))
        
        vol_data = {}
        for key, token in option_tokens.items():
            try:
                candles = kc.historical_data(token, start_dt, end_dt, "minute")
                if candles:
                    df_c = pd.DataFrame(candles)
                    df_c['date'] = pd.to_datetime(df_c['date'])
                    df_c.set_index('date', inplace=True)
                    vol_data[key] = df_c['volume']
            except Exception:
                pass
                
        if not vol_data:
            print(f"{day.strftime('%Y-%m-%d'):<12} | {'No Data (Market Holiday/API Error)':<50}")
            continue
            
        df_vol = pd.DataFrame(vol_data)
        df_vol.fillna(0, inplace=True)
        
        pe_cols = [c for c in df_vol.columns if "PE" in c]
        df_vol['put_vol'] = df_vol[pe_cols].sum(axis=1)
        
        ce_cols = [c for c in df_vol.columns if "CE" in c]
        df_vol['call_vol'] = df_vol[ce_cols].sum(axis=1)
        
        df_vol['raw_vol_pcr'] = df_vol['put_vol'] / df_vol['call_vol']
        df_vol['raw_vol_pcr'].replace([np.inf, -np.inf], np.nan, inplace=True)
        df_vol['raw_vol_pcr'].fillna(1.0, inplace=True)
        df_vol['vol_pcr_ema'] = df_vol['raw_vol_pcr'].ewm(span=15, adjust=False).mean()
        
        pcr_ema_series = df_vol['vol_pcr_ema']
        
        max_idx = pcr_ema_series.idxmax()
        max_val = pcr_ema_series.loc[max_idx]
        
        min_idx = pcr_ema_series.idxmin()
        min_val = pcr_ema_series.loc[min_idx]
        
        min_str = f"{min_val:.2f} ({min_idx.strftime('%H:%M')})"
        max_str = f"{max_val:.2f} ({max_idx.strftime('%H:%M')})"
        
        # Simple classification of the day's trend based on extremes
        if min_val <= 0.40 and max_val <= 1.20:
            trend = "Strongly Bullish (No capitulation)"
        elif max_val >= 1.60 and min_val >= 0.80:
            trend = "Strongly Bearish (No exhaustion)"
        else:
            trend = "Two-Way Reversion Day"
            
        print(f"{day.strftime('%Y-%m-%d'):<12} | {min_str:<22} | {max_str:<22} | {trend}")
        
    print("="*80)

if __name__ == "__main__":
    run_backtest_7_days()
