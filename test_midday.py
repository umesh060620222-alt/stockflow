import sys
import datetime as dt

def main():
    print("Preparing continuous trading backtest script...")
    # Read original app.py
    with open("app.py", "r", encoding="utf-8") as f:
        content = f.read()
        
    # Replace the time window line
    old_line = 'is_valid_time = ("10:00" <= time_str < "11:00") or ("14:00" <= time_str < "15:30")'
    new_line = 'is_valid_time = "10:00" <= time_str < "15:30"'
    
    if old_line not in content:
        print("Error: Could not find the time check line in app.py!")
        return
        
    content_new = content.replace(old_line, new_line)
    
    # Save to a temporary file
    with open("app_temp.py", "w", encoding="utf-8") as f:
        f.write(content_new)
        
    print("Running continuous 30-day options simulation...")
    import app_temp
    overrides = {
        "capital": 40000.0,
        "period": "30d",
        "source": "zerodha",
        "lot_size_mode": "auto"
    }
    out = app_temp.run_options_algo(overrides)
    if "error" in out:
        print(f"Error: {out['error']}")
        return
        
    s = out["summary"]
    print("\n--- CONTINUOUS TRADING (10:00 - 15:30) 30-DAY SUMMARY ---")
    print(f"Total Trades: {s['total_trades']}")
    print(f"Wins / Losses / BE: {s['wins']} / {s['losses']} / {s['be']}")
    print(f"Win Rate: {s['win_rate']}%")
    print(f"Net P&L: Rs. {s['total_pnl']:.2f}")
    
    print("\nDaily Summaries (Continuous):")
    print("Date\tTrades\tWins\tLosses\tBE\tNet P&L")
    for d in out["daily"]:
        if d.get("trades", 0) > 0:
            print(f"{d['date']}\t{d['trades']}\t{d['wins']}\t{d['losses']}\t{d['be']}\tRs. {d['pnl']:.2f}")

if __name__ == "__main__":
    main()
