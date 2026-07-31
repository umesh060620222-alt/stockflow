import datetime as dt
import pandas as pd
import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import zerodha as Z

def print_weekly_trades():
    print("Connecting to Zerodha Kite client...")
    kc = Z.kite()
    
    nifty_token = 256265
    today = dt.date.today()
    
    # We want this week: July 27 (Mon) to July 30 (Thu)
    start_date = dt.date(2026, 7, 27)
    end_date = dt.date(2026, 7, 30)
    
    # We need to trace back to Friday, July 24 to get yesterday's close reference for July 27
    daily_candles = kc.historical_data(nifty_token, dt.date(2026, 7, 24), end_date, "day")
    daily_close_map = {c['date'].date(): c['close'] for c in daily_candles}
    
    trading_days = [dt.date(2026, 7, 27), dt.date(2026, 7, 28), dt.date(2026, 7, 29), dt.date(2026, 7, 30)]
    
    print("\n" + "="*95)
    print("📋 MINUTE-BY-MINUTE TRADE TIMELINE FOR THIS WEEK (2 LOTS / 130 SHARES)")
    print("="*95)
    
    for day in trading_days:
        # Find yesterday's close reference (excluding weekends)
        yesterday = day - dt.timedelta(days=1)
        while yesterday.weekday() >= 5 or yesterday not in daily_close_map:
            yesterday = yesterday - dt.timedelta(days=1)
            
        yesterday_close = daily_close_map.get(yesterday)
        
        # Fetch today's 1-minute candles
        s_dt = dt.datetime.combine(day, dt.time(9, 15))
        e_dt = dt.datetime.combine(day, dt.time(15, 30))
        
        try:
            today_candles = kc.historical_data(nifty_token, s_dt, e_dt, "minute")
            if not today_candles:
                print(f"Skipping {day}: No candle data.")
                continue
            df = pd.DataFrame(today_candles)
            df['date'] = pd.to_datetime(df['date'])
            df.set_index('date', inplace=True)
        except Exception as e:
            print(f"Skipping {day}: {e}")
            continue
            
        today_open = df.iloc[0]['open']
        gap = today_open - yesterday_close
        gap_pct = gap / yesterday_close
        
        # Resolve Direction
        if gap_pct >= 0.008:
            is_call = False
            trade_desc = "PE (Fade Extreme Gap Up)"
        elif gap_pct <= -0.008:
            is_call = True
            trade_desc = "CE (Fade Extreme Gap Down)"
        elif gap > 0:
            is_call = True
            trade_desc = "CE (Follow Normal Gap Up)"
        else:
            is_call = False
            trade_desc = "PE (Follow Normal Gap Down)"
            
        # 10:00 AM Entry
        target_dt = pd.to_datetime(f"{day} 10:00:00").tz_localize(df.index.tz)
        if target_dt not in df.index:
            active_indices = df.index[df.index >= target_dt]
            if len(active_indices) > 0:
                target_dt = active_indices[0]
            else:
                continue
                
        entry_price = df.loc[target_dt, 'open']
        entry_time_str = target_dt.strftime("%H:%M:%S")
        
        df_trade = df.loc[target_dt:]
        
        # Simulate Trade timeline
        hit_target = False
        hit_sl = False
        exit_time = None
        exit_price = None
        
        # Walk minute-by-minute
        for idx, row in df_trade.iterrows():
            if is_call:
                profit = row['high'] - entry_price
                drawdown = entry_price - row['low']
            else:
                profit = entry_price - row['low']
                drawdown = row['high'] - entry_price
                
            if drawdown >= 50.0:
                hit_sl = True
                exit_time = idx
                exit_price = entry_price - 50.0 if is_call else entry_price + 50.0
                break
            elif profit >= 40.0:
                hit_target = True
                exit_time = idx
                exit_price = entry_price + 40.0 if is_call else entry_price - 40.0
                break
                
        print(f"\n📅 Date: {day.strftime('%A, %b %d, %Y')}")
        print(f"   ▸ Yesterday Close: {yesterday_close:.2f} | Today Open: {today_open:.2f} (Gap: {gap:+.2f} pts, {gap_pct*100:+.2f}%)")
        print(f"   ▸ Strategy Signal: {trade_desc}")
        print(f"   ▸ Initial Entry:   {entry_time_str} @ Spot {entry_price:.2f}")
        
        if hit_target:
            pts = 40.0
            rs = pts * 130
            print(f"   ▸ Initial Exit:    {exit_time.strftime('%H:%M:%S')} @ Spot {exit_price:.2f} [🎯 TARGET HIT]")
            print(f"   💸 Net Day P&L:    {pts:>+5.1f} pts (₹{rs:>+6.0f})")
        elif hit_sl:
            print(f"   ▸ Initial Exit:    {exit_time.strftime('%H:%M:%S')} @ Spot {exit_price:.2f} [❌ STOP-LOSS HIT]")
            
            # Immediately enter SAR
            sar_entry_price = exit_price
            sar_entry_time_str = exit_time.strftime("%H:%M:%S")
            sar_desc = "BUY PE (Reverse to Short)" if is_call else "BUY CE (Reverse to Long)"
            
            print(f"   🔄 SAR Entry:      {sar_entry_time_str} @ Spot {sar_entry_price:.2f} ({sar_desc})")
            
            df_sar = df_trade.loc[exit_time:]
            sar_hit_target = False
            sar_hit_sl = False
            sar_exit_time = None
            sar_exit_price = None
            
            for s_idx, s_row in df_sar.iterrows():
                if is_call:
                    sar_profit = sar_entry_price - s_row['low']
                    sar_drawdown = s_row['high'] - sar_entry_price
                else:
                    sar_profit = s_row['high'] - sar_entry_price
                    sar_drawdown = sar_entry_price - s_row['low']
                    
                if sar_profit >= 50.0:
                    sar_hit_target = True
                    sar_exit_time = s_idx
                    sar_exit_price = sar_entry_price - 50.0 if is_call else sar_entry_price + 50.0
                    break
                elif sar_drawdown >= 50.0:
                    sar_hit_sl = True
                    sar_exit_time = s_idx
                    sar_exit_price = sar_entry_price + 50.0 if is_call else sar_entry_price - 50.0
                    break
                    
            if sar_hit_target:
                pts = 0.0 # -50 + 50
                rs = 0.0
                print(f"   🔄 SAR Exit:        {sar_exit_time.strftime('%H:%M:%S')} @ Spot {sar_exit_price:.2f} [🎯 SAR TARGET HIT]")
                print(f"   💸 Net Day P&L:    {pts:>+5.1f} pts (₹{rs:>+6.0f} - BREAKEVEN)")
            elif sar_hit_sl:
                pts = -100.0
                rs = pts * 130
                print(f"   🔄 SAR Exit:        {sar_exit_time.strftime('%H:%M:%S')} @ Spot {sar_exit_price:.2f} [❌ SAR STOP-LOSS HIT]")
                print(f"   💸 Net Day P&L:    {pts:>+5.1f} pts (₹{rs:>+6.0f} - DOUBLE LOSS)")
            else:
                # EOD Exit
                eod_exit_time = df_sar.index[-1]
                sar_exit_price = df_sar.iloc[-1]['close']
                if is_call:
                    sar_pnl = sar_entry_price - sar_exit_price
                else:
                    sar_pnl = sar_exit_price - sar_entry_price
                pts = -50.0 + sar_pnl
                rs = pts * 130
                print(f"   🔄 SAR Exit:        {eod_exit_time.strftime('%H:%M:%S')} @ Spot {sar_exit_price:.2f} [⏰ EOD CLOSE TIMEOUT]")
                print(f"   💸 Net Day P&L:    {pts:>+5.1f} pts (₹{rs:>+6.0f})")
        else:
            # EOD close on initial trade
            eod_exit_time = df_trade.index[-1]
            exit_price = df_trade.iloc[-1]['close']
            if is_call:
                pts = exit_price - entry_price
            else:
                pts = entry_price - exit_price
            rs = pts * 130
            print(f"   ▸ Initial Exit:    {eod_exit_time.strftime('%H:%M:%S')} @ Spot {exit_price:.2f} [⏰ EOD CLOSE TIMEOUT]")
            print(f"   💸 Net Day P&L:    {pts:>+5.1f} pts (₹{rs:>+6.0f})")
            
    print("\n" + "="*95)

if __name__ == "__main__":
    print_weekly_trades()
