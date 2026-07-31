import datetime as dt
import pandas as pd
import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import zerodha as Z

def analyze_win_loss():
    print("Connecting to Zerodha Kite client...")
    kc = Z.kite()
    
    nifty_token = 256265
    today = dt.date.today()
    
    # Get past 91 trading days
    trading_days = []
    current_date = today
    while len(trading_days) < 92:
        if current_date.weekday() < 5:
            trading_days.append(current_date)
        current_date = current_date - dt.timedelta(days=1)
    trading_days.reverse()
    
    test_days = trading_days[-90:]
    
    results = []
    
    for i, day in enumerate(test_days):
        yest_idx = trading_days.index(day) - 1
        yesterday = trading_days[yest_idx]
        
        start_dt = dt.datetime.combine(day, dt.time(9, 15))
        end_dt = dt.datetime.combine(day, dt.time(15, 30))
        
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
        
        if gap_pct >= 0.008:
            is_call = False
        elif gap_pct <= -0.008:
            is_call = True
        elif gap > 0:
            is_call = True
        else:
            is_call = False
            
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
        
        hit_target = False
        hit_sl = False
        hit_idx = None
        
        for idx, row in df_trade.iterrows():
            if is_call:
                profit = row['high'] - entry_price
                drawdown = entry_price - row['low']
            else:
                profit = entry_price - row['low']
                drawdown = row['high'] - entry_price
                
            if drawdown >= 50.0:
                hit_sl = True
                hit_idx = idx
                break
            elif profit >= 40.0:
                hit_target = True
                hit_idx = idx
                break
                
        pnl = 0.0
        sar_taken = False
        sar_result = "-"
        sar_pnl = 0.0
        
        if hit_target:
            pnl = 40.0
        elif hit_sl:
            pnl = -50.0
            sar_taken = True
            if is_call:
                sl_price = entry_price - 50.0
            else:
                sl_price = entry_price + 50.0
                
            df_sar = df_trade.loc[hit_idx:]
            sar_hit_target = False
            sar_hit_sl = False
            
            for s_idx, s_row in df_sar.iterrows():
                if is_call:
                    sar_profit = sl_price - s_row['low']
                    sar_drawdown = s_row['high'] - sl_price
                else:
                    sar_profit = s_row['high'] - sl_price
                    sar_drawdown = sl_price - s_row['low']
                    
                if sar_profit >= 50.0:
                    sar_hit_target = True
                    break
                elif sar_drawdown >= 50.0:
                    sar_hit_sl = True
                    break
                    
            if sar_hit_target:
                sar_pnl = 50.0
                sar_result = "✅ BREAKEVEN"
            elif sar_hit_sl:
                sar_pnl = -50.0
                sar_result = "❌ DBL WHIP"
            else:
                sar_exit_price = df_sar.iloc[-1]['close']
                if is_call:
                    sar_pnl = sl_price - sar_exit_price
                else:
                    sar_pnl = sar_exit_price - sl_price
                sar_result = "TIMEOUT (EOD)"
        else:
            if is_call:
                pnl = eod_price - entry_price
            else:
                pnl = entry_price - eod_price
                
        results.append({
            "date": day,
            "initial_pnl": pnl,
            "sar_taken": sar_taken,
            "sar_result": sar_result,
            "sar_pnl": sar_pnl,
            "net_pnl": pnl + sar_pnl
        })
        
    df_res = pd.DataFrame(results)
    
    # Analyze final net day outcomes
    total_days = len(df_res)
    green_days = len(df_res[df_res['net_pnl'] > 0.0])
    flat_days = len(df_res[df_res['net_pnl'] == 0.0])
    red_days = len(df_res[df_res['net_pnl'] < 0.0])
    
    # Specific detail counts
    initial_wins = len(df_res[df_res['initial_pnl'] == 40.0])
    sar_breakevens = len(df_res[df_res['sar_result'] == "✅ BREAKEVEN"])
    double_whipsaws = len(df_res[df_res['sar_result'] == "❌ DBL WHIP"])
    
    print("\n" + "="*80)
    print("📊 WIN / LOSS / BREAKEVEN DETAILED METRICS (PAST 90 DAYS)")
    print("="*80)
    print(f"Total Traded Days:                    {total_days} days")
    print(f"🟢 Green Days (Net Profit > 0):       {green_days} days ({green_days/total_days*100:.1f}%)")
    print(f"🟡 Flat Days (Net P&L = 0 / Breakeven): {flat_days} days ({flat_days/total_days*100:.1f}%)")
    print(f"🔴 Red Days (Net Loss < 0):           {red_days} days ({red_days/total_days*100:.1f}%)")
    print("-"*80)
    print("📋 Micro-Breakdown of Days:")
    print(f"  1. Initial Wins (Hit +40 target first):   {initial_wins} days")
    print(f"  2. Recovered Breakevens (Hit +50 on SAR): {sar_breakevens} days")
    print(f"  3. Double Whip-saws (Hit -50 on SAR):     {double_whipsaws} days")
    print(f"  4. Other Days (EOD exits / minor P&L):    {total_days - initial_wins - sar_breakevens - double_whipsaws} days")
    print("="*80)

if __name__ == "__main__":
    analyze_win_loss()
