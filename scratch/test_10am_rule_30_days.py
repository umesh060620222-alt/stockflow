import datetime as dt
import pandas as pd
import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import zerodha as Z

def run_30_day_10am_backtest():
    print("Connecting to Zerodha Kite client...")
    kc = Z.kite()
    
    nifty_token = 256265
    today = dt.date.today()
    
    # Get past 31 trading days (excluding weekends) to have 30 testable days
    trading_days = []
    current_date = today
    while len(trading_days) < 32:
        if current_date.weekday() < 5:
            trading_days.append(current_date)
        current_date = current_date - dt.timedelta(days=1)
    trading_days.reverse()
    
    # We test the last 30 trading days of this list
    test_days = trading_days[-30:]
    
    print("\n" + "="*95)
    print("📊 10:00 AM GAP RULE 30-DAY BACKTEST REPORT")
    print("="*95)
    print(f"{'Date':<10} | {'Gap Direction':<13} | {'10AM Spot':<9} | {'EOD Spot':<9} | {'EOD P&L':<8} | {'Max Peak':<8} | {'Target (+20)'}")
    print("-"*95)
    
    success_count = 0
    total_trades = 0
    total_eod_points = 0.0
    
    for i, day in enumerate(test_days):
        # Yesterday is the element in trading_days immediately before this day
        yest_idx = trading_days.index(day) - 1
        yesterday = trading_days[yest_idx]
        
        start_dt = dt.datetime.combine(day, dt.time(9, 15))
        end_dt = dt.datetime.combine(day, dt.time(15, 30))
        
        # 1. Fetch Yesterday's Close
        try:
            yest_candles = kc.historical_data(nifty_token, yesterday, yesterday, "day")
            if yest_candles:
                yesterday_close = yest_candles[0]['close']
            else:
                y_start = dt.datetime.combine(yesterday, dt.time(9, 15))
                y_end = dt.datetime.combine(yesterday, dt.time(15, 30))
                y_candles = kc.historical_data(nifty_token, y_start, y_end, "minute")
                yesterday_close = y_candles[-1]['close'] if y_candles else None
        except Exception:
            continue
        if yesterday_close is None:
            continue
            
        # 2. Fetch Today's Candles
        try:
            today_candles = kc.historical_data(nifty_token, start_dt, end_dt, "minute")
            if not today_candles:
                continue
            df = pd.DataFrame(today_candles)
            df['date'] = pd.to_datetime(df['date'])
            df.set_index('date', inplace=True)
        except Exception:
            continue
            
        today_open = df.iloc[0]['open']
        gap = today_open - yesterday_close
        is_gap_up = gap > 0
        gap_str = f"Gap Up ({gap:+.1f})" if is_gap_up else f"Gap Down ({gap:+.1f})"
        
        # Resolve 10:00 AM Entry Spot
        target_dt = pd.to_datetime(f"{day} 10:00:00").tz_localize(df.index.tz)
        if target_dt not in df.index:
            active_indices = df.index[df.index >= target_dt]
            if len(active_indices) > 0:
                target_dt = active_indices[0]
            else:
                continue
                
        entry_price = df.loc[target_dt, 'open']
        df_trade = df.loc[target_dt:]
        eod_price = df_trade.iloc[-1]['close']
        
        # Calculate P&L
        if is_gap_up:
            eod_pnl = eod_price - entry_price
            highest_price = df_trade['high'].max()
            max_profit = highest_price - entry_price
        else:
            eod_pnl = entry_price - eod_price
            lowest_price = df_trade['low'].min()
            max_profit = entry_price - lowest_price
            
        hit_target = max_profit >= 20.0
        target_str = "✅ HIT" if hit_target else "❌ MISSED"
        
        if hit_target:
            success_count += 1
        total_trades += 1
        total_eod_points += eod_pnl
        
        pnl_str = f"{eod_pnl:+.2f}"
        peak_str = f"{max_profit:+.2f}"
        
        print(f"{day.strftime('%Y-%m-%d'):<10} | {gap_str:<13} | {entry_price:<9.2f} | {eod_price:<9.2f} | {pnl_str:<8} | {peak_str:<8} | {target_str}")
        
    print("-"*95)
    win_rate = (success_count / total_trades * 100) if total_trades > 0 else 0
    print(f"Total Trades: {total_trades} | +20 Target Hits: {success_count} | Win Rate: {win_rate:.1f}%")
    print(f"Total EOD Points Gained: {total_eod_points:+.2f} Nifty Spot Points")
    print("="*95)

if __name__ == "__main__":
    run_30_day_10am_backtest()
