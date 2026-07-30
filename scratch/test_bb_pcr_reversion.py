import datetime as dt
import pandas as pd
import numpy as np
import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import zerodha as Z

def run_combined_backtest():
    print("Connecting to Zerodha Kite client...")
    kc = Z.kite()
    
    today = dt.date.today()
    nifty_token = 256265
    
    # Expiry for volume data (August 4th)
    expiry_date = dt.date(2026, 8, 4)
    strikes = [24200, 24250, 24300, 24350]
    
    start_dt = dt.datetime.combine(today, dt.time(9, 15))
    end_dt = dt.datetime.combine(today, dt.time(15, 30))
    
    # 1. Fetch Nifty Spot Candles
    print("Fetching today's Nifty Spot candles...")
    try:
        nifty_candles = kc.historical_data(nifty_token, start_dt, end_dt, "minute")
    except Exception as e:
        print(f"Error fetching Spot data: {e}")
        return
        
    if not nifty_candles:
        print("No Spot candles fetched.")
        return
        
    df_spot = pd.DataFrame(nifty_candles)
    df_spot['date'] = pd.to_datetime(df_spot['date'])
    df_spot.set_index('date', inplace=True)
    
    # Calculate Bollinger Bands
    df_spot['sma20'] = df_spot['close'].rolling(window=20).mean()
    df_spot['std20'] = df_spot['close'].rolling(window=20).std()
    df_spot['upper_bb'] = df_spot['sma20'] + 2.0 * df_spot['std20']
    df_spot['lower_bb'] = df_spot['sma20'] - 2.0 * df_spot['std20']
    
    # 2. Fetch Option Volume Tokens and Candles
    print(f"Resolving option contracts expiring {expiry_date}...")
    insts = Z.get_nfo_instruments(kc)
    option_tokens = {}
    for s in strikes:
        for opt_type in ["CE", "PE"]:
            token = Z.get_option_token(kc, "NIFTY", expiry_date, s, opt_type)
            if token:
                option_tokens[f"{s}_{opt_type}"] = token
                
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
            
    df_vol = pd.DataFrame(vol_data)
    df_vol.fillna(0, inplace=True)
    
    # Sum Puts and Calls
    pe_cols = [c for c in df_vol.columns if "PE" in c]
    ce_cols = [c for c in df_vol.columns if "CE" in c]
    df_vol['put_vol'] = df_vol[pe_cols].sum(axis=1)
    df_vol['call_vol'] = df_vol[ce_cols].sum(axis=1)
    
    # Calculate Volume PCR
    df_vol['raw_vol_pcr'] = df_vol['put_vol'] / df_vol['call_vol']
    df_vol['raw_vol_pcr'].replace([np.inf, -np.inf], np.nan, inplace=True)
    df_vol['raw_vol_pcr'].fillna(1.0, inplace=True)
    df_vol['vol_pcr_ema'] = df_vol['raw_vol_pcr'].ewm(span=15, adjust=False).mean()
    
    # Merge Spot and Volume PCR
    df = df_spot.join(df_vol[['vol_pcr_ema', 'raw_vol_pcr']], how='inner')
    print(f"Aligned dataset contains {len(df)} candles.")
    
    # 3. Scan for Combined Reversion Signals
    signals = []
    
    for i in range(20, len(df) - 15):
        row = df.iloc[i]
        close = row['close']
        upper = row['upper_bb']
        lower = row['lower_bb']
        pcr_ema = row['vol_pcr_ema']
        time_str = df.index[i].strftime("%H:%M")
        
        signal_type = None
        reason = ""
        
        # CE Entry: Price is at or below Lower BB AND Volume PCR is spiked (Bearish panic)
        # Note: We use 1.25 on next-week contracts as our extreme threshold
        if close <= lower and pcr_ema >= 1.25:
            signal_type = "BUY CE (COMBINED REVERSION)"
            reason = f"Price ₹{close:.2f} <= Lower BB ₹{lower:.2f} & Vol PCR {pcr_ema:.2f} >= 1.25"
            
        # PE Entry: Price is at or above Upper BB AND Volume PCR is low (Bullish exhaustion)
        # Note: We use 0.75 on next-week contracts as our extreme threshold
        elif close >= upper and pcr_ema <= 0.75:
            signal_type = "BUY PE (COMBINED REVERSION)"
            reason = f"Price ₹{close:.2f} >= Upper BB ₹{upper:.2f} & Vol PCR {pcr_ema:.2f} <= 0.75"
            
        if signal_type:
            # Simulate trade entry at open of the next candle
            entry_row = df.iloc[i+1]
            entry_price = entry_row['open']
            entry_time = df.index[i+1]
            
            trade_closed = False
            points = 0
            exit_time = None
            exit_price = None
            result = "TIMEOUT"
            
            # Trace trade performance
            for j in range(i+2, min(i+17, len(df))):
                check_row = df.iloc[j]
                current_price = check_row['close']
                sma = check_row['sma20']
                
                if "BUY CE" in signal_type:
                    # Target: cross SMA upwards
                    if current_price >= sma:
                        exit_price = current_price
                        points = current_price - entry_price
                        result = "PROFIT (Reverted to SMA)"
                        exit_time = df.index[j]
                        trade_closed = True
                        break
                    # Stop loss: 15 points
                    elif current_price <= entry_price - 15.0:
                        exit_price = current_price
                        points = current_price - entry_price
                        result = "STOP LOSS"
                        exit_time = df.index[j]
                        trade_closed = True
                        break
                else: # BUY PE
                    # Target: cross SMA downwards
                    if current_price <= sma:
                        exit_price = current_price
                        points = entry_price - current_price
                        result = "PROFIT (Reverted to SMA)"
                        exit_time = df.index[j]
                        trade_closed = True
                        break
                    # Stop loss: 15 points
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
                
            # Prevent duplicate contiguous signals
            if not signals or (entry_time - signals[-1]['entry_time']).total_seconds() > 600:
                signals.append({
                    "time": time_str,
                    "type": signal_type,
                    "reason": reason,
                    "entry_time": entry_time,
                    "entry_price": entry_price,
                    "exit_time": exit_time,
                    "exit_price": exit_price,
                    "points": round(points, 2),
                    "result": result
                })
                
    # Print combined report
    print("\n" + "="*80)
    print(f"📊 COMBINED BOLLINGER + VOL PCR BACKTEST REPORT (TODAY: {today})")
    print("="*80)
    print(f"{'Time':<8} | {'Type':<25} | {'Entry':<8} | {'Exit':<8} | {'Points':<8} | {'Result':<25}")
    print("-"*80)
    
    total_points = 0.0
    wins = 0
    losses = 0
    
    for s in signals:
        p_str = f"{s['points']:+}"
        print(f"{s['time']:<8} | {s['type'][:25]:<25} | {s['entry_price']:<8.2f} | {s['exit_price']:<8.2f} | {p_str:<8} | {s['result']:<25}")
        total_points += s['points']
        if s['points'] > 0:
            wins += 1
        elif s['points'] < 0:
            losses += 1
            
    total_trades = len(signals)
    win_rate = (wins / total_trades * 100) if total_trades > 0 else 0
    
    print("-"*80)
    print(f"Total Trades: {total_trades} | Wins: {wins} | Losses: {losses} | Win Rate: {win_rate:.1f}%")
    print(f"Total Combined Points Gained: {total_points:+.2f} Nifty Spot Points")
    print("="*80)

if __name__ == "__main__":
    run_combined_backtest()
