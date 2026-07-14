import sys
sys.path.append("C:/code/stockflow")
import app
import json

def main():
    print("Running 30-day options simulation...")
    overrides = {
        "capital": 40000.0,
        "period": "30d",
        "source": "zerodha",
        "lot_size_mode": "auto"
    }
    out = app.run_options_algo(overrides)
    if "error" in out:
        print(f"Error: {out['error']}")
        return
        
    s = out["summary"]
    print("\n--- 30-Day Options Simulation Summary ---")
    print(f"Total Trades: {s['total_trades']}")
    print(f"Wins / Losses / BE: {s['wins']} / {s['losses']} / {s['be']}")
    print(f"Win Rate: {s['win_rate']}%")
    print(f"Net P&L: Rs. {s['total_pnl']:.2f}")
    
    print("\nDaily Summaries:")
    print("Date\tTrades\tWins\tLosses\tBE\tNet P&L")
    for d in out["daily"]:
        print(f"{d['date']}\t{d['trades']}\t{d['wins']}\t{d['losses']}\t{d['be']}\tRs. {d['pnl']:.2f}")

if __name__ == "__main__":
    main()
