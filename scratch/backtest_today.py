import datetime as dt
import pandas as pd
import numpy as np
import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import zerodha as Z

def run_backtest():
    print("Connecting to Zerodha Kite client...")
    kc = Z.kite()
    
    # Nifty 50 Index token is 256265
    nifty_token = 256265
    
    today = dt.date.today()
    start_dt = dt.datetime.combine(today, dt.time(9, 15))
    end_dt = dt.datetime.combine(today, dt.time(15, 30))
    
    print(f"Fetching 1-minute historical Nifty Spot candles for today ({today})...")
    try:
        candles = kc.historical_data(nifty_token, start_dt, end_dt, "minute")
    except Exception as e:
        print(f"Error fetching historical data: {e}")
        return
        
    if not candles:
        print("No candles fetched today. Market might not be open or API returned empty list.")
        return
        
    print(f"Loaded {len(candles)} candles.")
    
    df = pd.DataFrame(candles)
    # Parse dates
    df['date'] = pd.to_datetime(df['date'])
    
    # Calculate Bollinger Bands (20 period, 2 StdDev)
    df['sma20'] = df['close'].rolling(window=20).mean()
    df['std20'] = df['close'].rolling(window=20).std()
    df['upper_bb'] = df['sma20'] + 2.0 * df['std20']
    df['lower_bb'] = df['sma20'] - 2.0 * df['std20']
    
    # Calculate RSI (14)
    delta = df['close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
    rs = gain / loss
    df['rsi'] = 100 - (100 / (1 + rs))
    
    # Print the signals and evaluate reversion
    signals = []
    
    for i in range(20, len(df) - 15):
        row = df.iloc[i]
        close = row['close']
        upper = row['upper_bb']
        lower = row['lower_bb']
        rsi = row['rsi']
        time_str = row['date'].strftime("%H:%M")
        
        signal_type = None
        reason = ""
        
        # Check Oversold (CE Buy Signal)
        if close <= lower or rsi <= 30:
            signal_type = "BUY CE (BULLISH REVERSION)"
            reason = f"Price ₹{close:.2f} <= Lower BB ₹{lower:.2f} or RSI {rsi:.1f} <= 30"
        
        # Check Overbought (PE Buy Signal)
        elif close >= upper or rsi >= 70:
            signal_type = "BUY PE (BEARISH REVERSION)"
            reason = f"Price ₹{close:.2f} >= Upper BB ₹{upper:.2f} or RSI {rsi:.1f} >= 70"
            
        if signal_type:
            # We simulate entering at the open of the next candle (i+1)
            entry_row = df.iloc[i+1]
            entry_price = entry_row['open']
            entry_time = entry_row['date']
            
            # Evaluate trade results over the next 15 minutes
            trade_closed = False
            points = 0
            exit_time = None
            exit_price = None
            result = "TIMEOUT"
            
            for j in range(i+2, min(i+17, len(df))):
                check_row = df.iloc[j]
                current_price = check_row['close']
                sma = check_row['sma20']
                
                # Check for Reversion back to SMA (Profit Target)
                if signal_type == "BUY CE (BULLISH REVERSION)":
                    # We want price to go UP to the SMA
                    if current_price >= sma:
                        exit_price = current_price
                        points = current_price - entry_price
                        result = "PROFIT (Reverted to SMA)"
                        exit_time = check_row['date']
                        trade_closed = True
                        break
                    # Stop loss: 15 points drop
                    elif current_price <= entry_price - 15.0:
                        exit_price = current_price
                        points = current_price - entry_price
                        result = "STOP LOSS"
                        exit_time = check_row['date']
                        trade_closed = True
                        break
                else: # BUY PE
                    # We want price to go DOWN to the SMA
                    if current_price <= sma:
                        exit_price = current_price
                        points = entry_price - current_price
                        result = "PROFIT (Reverted to SMA)"
                        exit_time = check_row['date']
                        trade_closed = True
                        break
                    # Stop loss: 15 points rise
                    elif current_price >= entry_price + 15.0:
                        exit_price = current_price
                        points = entry_price - current_price
                        result = "STOP LOSS"
                        exit_time = check_row['date']
                        trade_closed = True
                        break
            
            if not trade_closed:
                # Exited at timeout (end of 15 minutes)
                exit_row = df.iloc[min(i+16, len(df)-1)]
                exit_price = exit_row['close']
                exit_time = exit_row['date']
                if signal_type == "BUY CE (BULLISH REVERSION)":
                    points = exit_price - entry_price
                else:
                    points = entry_price - exit_price
                result = "TIMEOUT (15m)"
            
            # Prevent duplicate contiguous signals (simple cooldown of 10 minutes)
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
                
    # Print detailed report
    print("\n" + "="*80)
    print(f"📊 INTRADAY MEAN REVERSION BACKTEST REPORT (TODAY: {today})")
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
    print(f"Total Points Gained: {total_points:+.2f} Nifty Spot Points")
    print("="*80)

if __name__ == "__main__":
    run_backtest()
