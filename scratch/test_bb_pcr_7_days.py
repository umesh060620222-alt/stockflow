import datetime as dt
import pandas as pd
import numpy as np
import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import zerodha as Z

def run_7_day_backtest():
    print("Connecting to Zerodha Kite client...")
    kc = Z.kite()
    
    # We use the August Monthly Expiry (expiry August 25, 2026) for liquid historical PCR data
    monthly_expiry = dt.date(2026, 8, 25)
    nifty_token = 256265
    strikes = [24200, 24250, 24300, 24350]
    
    # Fetch NFO instruments cache
    insts = Z.get_nfo_instruments(kc)
    option_tokens = {}
    for s in strikes:
        for opt_type in ["CE", "PE"]:
            token = Z.get_option_token(kc, "NIFTY", monthly_expiry, s, opt_type)
            if token:
                option_tokens[f"{s}_{opt_type}"] = token
                
    # Get past 7 trading days
    today = dt.date.today()
    trading_days = []
    current_date = today
    while len(trading_days) < 7:
        if current_date.weekday() < 5:
            trading_days.append(current_date)
        current_date = current_date - dt.timedelta(days=1)
    trading_days.reverse()
    
    all_trades = []
    
    for day in trading_days:
        print(f"\nProcessing {day.strftime('%Y-%m-%d')}...")
        start_dt = dt.datetime.combine(day, dt.time(9, 15))
        end_dt = dt.datetime.combine(day, dt.time(15, 30))
        
        # 1. Fetch Spot
        try:
            spot_candles = kc.historical_data(nifty_token, start_dt, end_dt, "minute")
            if not spot_candles:
                continue
            df_spot = pd.DataFrame(spot_candles)
            df_spot['date'] = pd.to_datetime(df_spot['date'])
            df_spot.set_index('date', inplace=True)
        except Exception:
            continue
            
        # Calculate Bollinger Bands
        df_spot['sma20'] = df_spot['close'].rolling(window=20).mean()
        df_spot['std20'] = df_spot['close'].rolling(window=20).std()
        df_spot['upper_bb'] = df_spot['sma20'] + 2.0 * df_spot['std20']
        df_spot['lower_bb'] = df_spot['sma20'] - 2.0 * df_spot['std20']
        
        # 2. Fetch Option Volume
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
            continue
            
        df_vol = pd.DataFrame(vol_data)
        df_vol.fillna(0, inplace=True)
        
        pe_cols = [c for c in df_vol.columns if "PE" in c]
        ce_cols = [c for c in df_vol.columns if "CE" in c]
        df_vol['put_vol'] = df_vol[pe_cols].sum(axis=1)
        df_vol['call_vol'] = df_vol[ce_cols].sum(axis=1)
        
        df_vol['raw_vol_pcr'] = df_vol['put_vol'] / df_vol['call_vol']
        df_vol['raw_vol_pcr'].replace([np.inf, -np.inf], np.nan, inplace=True)
        df_vol['raw_vol_pcr'].fillna(1.0, inplace=True)
        df_vol['vol_pcr_ema'] = df_vol['raw_vol_pcr'].ewm(span=15, adjust=False).mean()
        
        # Merge
        df = df_spot.join(df_vol[['vol_pcr_ema']], how='inner')
        
        # Scan
        for i in range(20, len(df) - 15):
            row = df.iloc[i]
            close = row['close']
            upper = row['upper_bb']
            lower = row['lower_bb']
            pcr_ema = row['vol_pcr_ema']
            
            signal_type = None
            
            # Use exactly 0.30 and 1.50 as requested
            if close <= lower and pcr_ema >= 1.50:
                signal_type = "BUY CE"
            elif close >= upper and pcr_ema <= 0.30:
                signal_type = "BUY PE"
                
            if signal_type:
                entry_row = df.iloc[i+1]
                entry_price = entry_row['open']
                entry_time = df.index[i+1]
                
                trade_closed = False
                points = 0
                exit_time = None
                exit_price = None
                result = "TIMEOUT"
                
                for j in range(i+2, min(i+17, len(df))):
                    check_row = df.iloc[j]
                    current_price = check_row['close']
                    sma = check_row['sma20']
                    
                    if "BUY CE" in signal_type:
                        if current_price >= sma:
                            exit_price = current_price
                            points = current_price - entry_price
                            result = "PROFIT (Reverted to SMA)"
                            exit_time = df.index[j]
                            trade_closed = True
                            break
                        elif current_price <= entry_price - 15.0:
                            exit_price = current_price
                            points = current_price - entry_price
                            result = "STOP LOSS"
                            exit_time = df.index[j]
                            trade_closed = True
                            break
                    else: # BUY PE
                        if current_price <= sma:
                            exit_price = current_price
                            points = entry_price - current_price
                            result = "PROFIT (Reverted to SMA)"
                            exit_time = df.index[j]
                            trade_closed = True
                            break
                        elif current_price >= entry_price + 15.0:
                            exit_price = current_price
                            points = entry_price - current_price
                            result = "STOP LOSS"
                            exit_time = df.index[j]
                            trade_closed = True
                            break
                            
                if not trade_closed:
                    exit_row = df.iloc[min(i+16, len(df)-1)]
                    exit_price = exit_row['close']
                    exit_time = df.index[min(i+16, len(df)-1)]
                    if "BUY CE" in signal_type:
                        points = exit_price - entry_price
                    else:
                        points = entry_price - exit_price
                    result = "TIMEOUT (15m)"
                    
                if not all_trades or (entry_time - all_trades[-1]['entry_time']).total_seconds() > 600 or all_trades[-1]['date'] != day:
                    all_trades.append({
                        "date": day,
                        "time": entry_time.strftime("%H:%M"),
                        "type": signal_type,
                        "entry_time": entry_time,
                        "entry_price": entry_price,
                        "exit_price": exit_price,
                        "points": round(points, 2),
                        "result": result
                    })
                    
    # Print 7-day report
    print("\n" + "="*80)
    print("📊 COMBINED BB + PCR 7-DAY BACKTEST REPORT (Limits: 0.30 & 1.50)")
    print("="*80)
    print(f"{'Date':<10} | {'Time':<5} | {'Type':<6} | {'Entry':<8} | {'Exit':<8} | {'Points':<8} | {'Result':<22}")
    print("-"*80)
    
    total_points = 0.0
    wins = 0
    losses = 0
    
    for t in all_trades:
        p_str = f"{t['points']:+}"
        print(f"{t['date'].strftime('%Y-%m-%d'):<10} | {t['time']:<5} | {t['type']:<6} | {t['entry_price']:<8.2f} | {t['exit_price']:<8.2f} | {p_str:<8} | {t['result']:<22}")
        total_points += t['points']
        if t['points'] > 0:
            wins += 1
        elif t['points'] < 0:
            losses += 1
            
    total_trades = len(all_trades)
    win_rate = (wins / total_trades * 100) if total_trades > 0 else 0
    
    print("-"*80)
    print(f"Total Trades: {total_trades} | Wins: {wins} | Losses: {losses} | Win Rate: {win_rate:.1f}%")
    print(f"Total Points Gained: {total_points:+.2f} Nifty Spot Points")
    print("="*80)

if __name__ == "__main__":
    run_7_day_backtest()
