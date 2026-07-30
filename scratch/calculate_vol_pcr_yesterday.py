import datetime as dt
import pandas as pd
import numpy as np
import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import zerodha as Z

def reconstruct_pcr():
    print("Connecting to Zerodha Kite client...")
    kc = Z.kite()
    
    # Yesterday's date
    yesterday = dt.date.today() - dt.timedelta(days=1)
    
    # Check if yesterday was a weekend, adjust if needed
    if yesterday.weekday() >= 5: # Saturday or Sunday
        print("Yesterday was a weekend! Finding the last trading day...")
        yesterday = yesterday - dt.timedelta(days=2 if yesterday.weekday() == 6 else 1)
        
    print(f"Resolving Nifty weekly options active on {yesterday}...")
    
    try:
        # Resolve next week's expiry date (active on yesterday)
        expiry_date = Z.get_expiry_date(kc, yesterday + dt.timedelta(days=1))
        print(f"Active weekly expiry used: {expiry_date}")
    except Exception as e:
        print(f"Failed to find expiry date: {e}")
        return
        
    # Strikes to track
    strikes = [24200, 24250, 24300, 24350]
    
    # Fetch NFO instruments cache
    insts = Z.get_nfo_instruments(kc)
    if not insts:
        print("Failed to fetch NFO instruments cache.")
        return
        
    option_tokens = {}
    
    # Resolve tokens
    for s in strikes:
        for opt_type in ["CE", "PE"]:
            token = Z.get_option_token(kc, "NIFTY", expiry_date, s, opt_type)
            if token:
                key = f"{s}_{opt_type}"
                option_tokens[key] = token
                
    if not option_tokens:
        print(f"Could not resolve option tokens for expiry {expiry_date}.")
        return
        
    print(f"Resolved tokens for {len(option_tokens)} option contracts.")
    
    start_dt = dt.datetime.combine(yesterday, dt.time(9, 15))
    end_dt = dt.datetime.combine(yesterday, dt.time(15, 30))
    
    # Dictionary to hold volume data
    vol_data = {}
    
    for key, token in option_tokens.items():
        print(f"Fetching volume candles for {key}...")
        try:
            candles = kc.historical_data(token, start_dt, end_dt, "minute")
            if candles:
                df_c = pd.DataFrame(candles)
                df_c['date'] = pd.to_datetime(df_c['date'])
                df_c.set_index('date', inplace=True)
                vol_data[key] = df_c['volume']
        except Exception as e:
            print(f"Failed to fetch volume for {key}: {e}")
            
    if not vol_data:
        print("No option volume data fetched.")
        return
        
    # Align all data series into a single DataFrame
    df_vol = pd.DataFrame(vol_data)
    df_vol.fillna(0, inplace=True)
    
    # Sum Put Volumes
    pe_cols = [c for c in df_vol.columns if "PE" in c]
    df_vol['put_vol'] = df_vol[pe_cols].sum(axis=1)
    
    # Sum Call Volumes
    ce_cols = [c for c in df_vol.columns if "CE" in c]
    df_vol['call_vol'] = df_vol[ce_cols].sum(axis=1)
    
    # Calculate raw 1-minute Volume PCR
    df_vol['raw_vol_pcr'] = df_vol['put_vol'] / df_vol['call_vol']
    df_vol['raw_vol_pcr'].replace([np.inf, -np.inf], np.nan, inplace=True)
    df_vol['raw_vol_pcr'].fillna(1.0, inplace=True)
    
    # Calculate a 15-period rolling EMA
    df_vol['vol_pcr_ema'] = df_vol['raw_vol_pcr'].ewm(span=15, adjust=False).mean()
    
    # Find highest and lowest values
    pcr_ema_series = df_vol['vol_pcr_ema']
    
    max_idx = pcr_ema_series.idxmax()
    max_val = pcr_ema_series.loc[max_idx]
    
    min_idx = pcr_ema_series.idxmin()
    min_val = pcr_ema_series.loc[min_idx]
    
    print("\n" + "="*80)
    print(f"📊 YESTERDAY'S VOLUME PCR EXTREMES REPORT ({yesterday})")
    print("="*80)
    print(f"📈 HIGHEST Volume PCR (Bearish Capitulation Peak):")
    print(f"   ▸ Value:     {max_val:.2f}")
    print(f"   ▸ Time:      {max_idx.strftime('%H:%M:%S')}")
    print(f"   ▸ Put Vol:   {int(df_vol.loc[max_idx, 'put_vol']):,}")
    print(f"   ▸ Call Vol:  {int(df_vol.loc[max_idx, 'call_vol']):,}")
    
    print(f"\n📉 LOWEST Volume PCR (Bullish Momentum Peak):")
    print(f"   ▸ Value:     {min_val:.2f}")
    print(f"   ▸ Time:      {min_idx.strftime('%H:%M:%S')}")
    print(f"   ▸ Put Vol:   {int(df_vol.loc[min_idx, 'put_vol']):,}")
    print(f"   ▸ Call Vol:  {int(df_vol.loc[min_idx, 'call_vol']):,}")
    print("="*80)
    
    # Print notable spikes (>= 1.30 or <= 0.70)
    print("\n📝 NOTABLE EXTREME SPIKES LOG (EMA >= 1.30 or EMA <= 0.70):")
    print("-"*80)
    print(f"{'Time':<12} | {'Raw PCR':<10} | {'EMA PCR':<10} | {'Put Volume':<12} | {'Call Volume':<12}")
    print("-"*80)
    
    last_printed_time = None
    for idx, row in df_vol.iterrows():
        raw_pcr = row['raw_vol_pcr']
        ema_pcr = row['vol_pcr_ema']
        
        if ema_pcr >= 1.30 or ema_pcr <= 0.70:
            if last_printed_time is None or (idx - last_printed_time).total_seconds() >= 60:
                print(f"{idx.strftime('%H:%M:%S'):<12} | {raw_pcr:<10.2f} | {ema_pcr:<10.2f} | {int(row['put_vol']):<12,} | {int(row['call_vol']):<12,}")
                last_printed_time = idx
    print("="*80)

if __name__ == "__main__":
    reconstruct_pcr()
