import datetime as dt
import pandas as pd
import numpy as np
import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import zerodha as Z

def run_reverse_large_gaps_backtest():
    print("Connecting to Zerodha Kite client...")
    kc = Z.kite()
    
    nifty_token = 256265
    today = dt.date.today()
    
    # Get past 91 trading days (excluding weekends)
    trading_days = []
    current_date = today
    while len(trading_days) < 92:
        if current_date.weekday() < 5:
            trading_days.append(current_date)
        current_date = current_date - dt.timedelta(days=1)
    trading_days.reverse()
    
    test_days = trading_days[-90:]
    
    results = []
    
    print("\n" + "="*115)
    print("📊 10:00 AM GAP RULE 90-DAY BACKTEST (FADE LARGE GAPS >0.8% / FOLLOW NORMAL GAPS)")
    print("="*115)
    print(f"{'Date':<10} | {'Gap %':<7} | {'Action':<15} | {'10AM Spot':<9} | {'EOD Spot':<9} | {'Max Peak':<8} | {'Drawdown':<8} | {'T:+20':<6} | {'T:+40':<6}")
    print("-"*115)
    
    for i, day in enumerate(test_days):
        yest_idx = trading_days.index(day) - 1
        yesterday = trading_days[yest_idx]
        
        start_dt = dt.datetime.combine(day, dt.time(9, 15))
        end_dt = dt.datetime.combine(day, dt.time(15, 30))
        
        # 1. Fetch Yesterday's Close
        yesterday_close = None
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
            pass
            
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
        gap_pct = gap / yesterday_close
        
        # Resolve trade direction based on gap percentage
        if gap_pct >= 0.008:
            # Large Gap Up: FADE (Buy PE)
            trade_side = "BUY PE (Fade)"
            is_call = False
        elif gap_pct <= -0.008:
            # Large Gap Down: FADE (Buy CE)
            trade_side = "BUY CE (Fade)"
            is_call = True
        elif gap > 0:
            # Normal Gap Up: FOLLOW (Buy CE)
            trade_side = "BUY CE (Follow)"
            is_call = True
        else:
            # Normal Gap Down: FOLLOW (Buy PE)
            trade_side = "BUY PE (Follow)"
            is_call = False
            
        # Resolve 10:00 AM Entry
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
        
        # Resolve target hits and drawdowns before target hits
        t20_hit = False
        t20_idx = None
        for idx, row in df_trade.iterrows():
            if is_call:
                profit = row['high'] - entry_price
            else:
                profit = entry_price - row['low']
            if profit >= 20.0:
                t20_hit = True
                t20_idx = idx
                break
                
        if t20_hit:
            df_before_t20 = df_trade.loc[:t20_idx]
            if is_call:
                t20_drawdown = entry_price - df_before_t20['low'].min()
            else:
                t20_drawdown = df_before_t20['high'].max() - entry_price
        else:
            if is_call:
                t20_drawdown = entry_price - df_trade['low'].min()
            else:
                t20_drawdown = df_trade['high'].max() - entry_price
                
        t40_hit = False
        t40_idx = None
        for idx, row in df_trade.iterrows():
            if is_call:
                profit = row['high'] - entry_price
            else:
                profit = entry_price - row['low']
            if profit >= 40.0:
                t40_hit = True
                t40_idx = idx
                break
                
        if t40_hit:
            df_before_t40 = df_trade.loc[:t40_idx]
            if is_call:
                t40_drawdown = entry_price - df_before_t40['low'].min()
            else:
                t40_drawdown = df_before_t40['high'].max() - entry_price
        else:
            if is_call:
                t40_drawdown = entry_price - df_trade['low'].min()
            else:
                t40_drawdown = df_trade['high'].max() - entry_price
                
        # For EOD stats display
        if is_call:
            max_profit = df_trade['high'].max() - entry_price
        else:
            max_profit = entry_price - df_trade['low'].min()
            
        t20_str = "✅ HIT" if t20_hit else "❌ MS"
        t40_str = "✅ HIT" if t40_hit else "❌ MS"
        
        results.append({
            "date": day,
            "gap_pct": gap_pct,
            "trade_side": trade_side,
            "entry_price": entry_price,
            "eod_price": eod_price,
            "max_profit": max_profit,
            "t20_drawdown": t20_drawdown,
            "t40_drawdown": t40_drawdown,
            "t20_hit": t20_hit,
            "t40_hit": t40_hit
        })
        
        print(f"{day.strftime('%Y-%m-%d'):<10} | {gap_pct*100:<+6.2f}% | {trade_side:<15} | {entry_price:<9.2f} | {eod_price:<9.2f} | {max_profit:<+8.1f} | -{t20_drawdown:<7.1f} | {t20_str:<6} | {t40_str:<6}")
        
    print("-"*115)
    
    total_trades = len(results)
    if total_trades == 0:
        return
        
    # Evaluate Target 20 Stats with -50 SL
    t20_wins = 0
    t20_losses = 0
    t20_points = 0.0
    for r in results:
        if r["t20_hit"] and r["t20_drawdown"] <= 50.0:
            t20_wins += 1
            t20_points += 20.0
        else:
            t20_losses += 1
            t20_points -= 50.0
            
    t20_win_rate = (t20_wins / total_trades) * 100
    
    # Evaluate Target 40 Stats with -50 SL
    t40_wins = 0
    t40_losses = 0
    t40_points = 0.0
    for r in results:
        if r["t40_hit"] and r["t40_drawdown"] <= 50.0:
            t40_wins += 1
            t40_points += 40.0
        else:
            t40_losses += 1
            t40_points -= 50.0
            
    t40_win_rate = (t40_wins / total_trades) * 100
    
    print(f"📊 HYBRID STRATEGY SUMMARY (FADE >0.8% GAPS / FOLLOW NORMAL):")
    print(f"   Total Traded Days:         {total_trades}")
    print("-"*115)
    print(f"🎯 Target +20 Points (with -50 SL):")
    print(f"   ▸ Win Rate:                {t20_win_rate:.1f}% ({t20_wins} Wins / {t20_losses} Losses)")
    print(f"   ▸ Net Points P&L:          {t20_points:+.2f} Nifty Spot Points")
    print(f"   ▸ 1 Lot Profit (75 shares): +₹{t20_points*75:.2f}")
    print("-"*115)
    print(f"🎯 Target +40 Points (with -50 SL):")
    print(f"   ▸ Win Rate:                {t40_win_rate:.1f}% ({t40_wins} Wins / {t40_losses} Losses)")
    print(f"   ▸ Net Points P&L:          {t40_points:+.2f} Nifty Spot Points")
    print(f"   ▸ 1 Lot Profit (75 shares): +₹{t40_points*75:.2f}")
    print("="*115)

if __name__ == "__main__":
    run_reverse_large_gaps_backtest()
