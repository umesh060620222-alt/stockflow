import datetime as dt
import pandas as pd
import numpy as np
import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import zerodha as Z

def run_compounding_simulation():
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
    
    print("\nProcessing historical data day-by-day...")
    for i, day in enumerate(test_days):
        yest_idx = trading_days.index(day) - 1
        yesterday = trading_days[yest_idx]
        
        start_dt = dt.datetime.combine(day, dt.time(9, 15))
        end_dt = dt.datetime.combine(day, dt.time(15, 30))
        
        # Fetch Yesterday's Close
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
            
        # Today's Candles
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
        
        # Resolve Direction
        if gap_pct >= 0.008:
            is_call = False
        elif gap_pct <= -0.008:
            is_call = True
        elif gap > 0:
            is_call = True
        else:
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
        
        # Check initial Target 40 or SL -50
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
            elif sar_hit_sl:
                sar_pnl = -50.0
            else:
                sar_exit_price = df_sar.iloc[-1]['close']
                if is_call:
                    sar_pnl = sl_price - sar_exit_price
                else:
                    sar_pnl = sar_exit_price - sl_price
        else:
            if is_call:
                pnl = eod_price - entry_price
            else:
                pnl = entry_price - eod_price
                
        results.append({
            "date": day,
            "net_pnl": pnl + sar_pnl
        })
        
    df_res = pd.DataFrame(results)
    
    # 4. Compounding simulation starting with different capital pools
    def simulate_compounding(start_capital):
        cap = start_capital
        history = []
        
        for idx, row in df_res.iterrows():
            # Lot size calculation (minimum 1 lot floor)
            lots = int(cap // 9750)
            if lots < 1:
                lots = 1
                
            shares = lots * 65
            day_pnl_pts = row['net_pnl']
            day_pnl_rs = day_pnl_pts * shares
            
            old_cap = cap
            cap += day_pnl_rs
            
            history.append({
                "date": row['date'],
                "lots": lots,
                "pnl_pts": day_pnl_pts,
                "pnl_rs": day_pnl_rs,
                "balance": cap
            })
            
        return cap, history
        
    # Simulate for 2 lots starting capital (₹19,500)
    cap_2lots, hist_2lots = simulate_compounding(19500.0)
    df_2 = pd.DataFrame(hist_2lots)
    
    # Simulate for 4 lots starting capital (₹39,000)
    cap_4lots, hist_4lots = simulate_compounding(39000.0)
    df_4 = pd.DataFrame(hist_4lots)
    
    # Print compound report
    print("\n" + "="*95)
    print("📈 DAILY COMPOUNDING SIMULATION (PAST 90 DAYS / 4 MONTHS)")
    print("="*95)
    print("ℹ️ Capital per lot = ₹9,750 (65 shares @ ₹150 premium)")
    print("-"*95)
    
    print("🚀 STARTING WITH 2 LOTS (Initial Capital: ₹19,500.00)")
    print(f"   ▸ Final Account Balance:     ₹{cap_2lots:,.2f}")
    print(f"   ▸ Net Profit:                ₹{cap_2lots - 19500:,.2f}")
    print(f"   ▸ Final Lot Size:            {int(cap_2lots // 9750)} lots")
    print(f"   ▸ Growth Multiplier:         {cap_2lots / 19500:.2f}x")
    print("-"*95)
    
    print("🚀 STARTING WITH 4 LOTS (Initial Capital: ₹39,000.00)")
    print(f"   ▸ Final Account Balance:     ₹{cap_4lots:,.2f}")
    print(f"   ▸ Net Profit:                ₹{cap_4lots - 39000:,.2f}")
    print(f"   ▸ Final Lot Size:            {int(cap_4lots // 9750)} lots")
    print(f"   ▸ Growth Multiplier:         {cap_4lots / 39000:.2f}x")
    print("-"*95)
    
    # Print milestone growth steps for the 2 lots version
    print("\n📋 Capital Growth Milestones (2 Lots Start):")
    print(f"{'Trading Day':<12} | {'Date':<10} | {'Lots Traded':<11} | {'Day P&L (Pts)':<13} | {'Day P&L (Rs)':<12} | {'Account Balance'}")
    print("-"*95)
    
    # Print every 7 trading days to show progression
    for i in range(0, len(df_2), 5):
        r = df_2.iloc[i]
        print(f"Day {i+1:02d}       | {r['date'].strftime('%Y-%m-%d'):<10} | {r['lots']:>2d} lots    | {r['pnl_pts']:>+11.1f}   | ₹{r['pnl_rs']:>+9.0f}  | ₹{r['balance']:,.2f}")
    
    # Also print the absolute final day
    r_final = df_2.iloc[-1]
    print(f"Day {len(df_2):02d}       | {r_final['date'].strftime('%Y-%m-%d'):<10} | {r_final['lots']:>2d} lots    | {r_final['pnl_pts']:>+11.1f}   | ₹{r_final['pnl_rs']:>+9.0f}  | ₹{r_final['balance']:,.2f}")
    print("="*95)

if __name__ == "__main__":
    run_compounding_simulation()
