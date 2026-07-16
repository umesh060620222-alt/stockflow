import config, data as D, strategy as S, engine as E
import pandas as pd

def main():
    raw = D.load_cached(interval="1m")
    bench = raw.get(config.BENCHMARK)
    bench_ret = None
    if bench is not None:
        bench_ret = bench.groupby("date")["close"].transform(lambda s: s / s.iloc[0] - 1.0)
        
    syms = [s for s in config.UNIVERSE if s in raw and s != config.BENCHMARK]
    
    # Configure the best settings
    config.MODE = "atr_pullback"
    config.ATR_DROP_MULT = 2.5
    config.ATR_BOUNCE_MULT = 0.7
    config.VOL_MULT = 1.5
    config.USE_NIFTY_FILTER = False
    config.TARGET_PCT = 0.008
    config.STOP_PCT = 0.004
    config.TIME_STOP_MIN = 390
    config.USE_VWAP_STALL_EXIT = False
    
    prepared = {}
    for sym in syms:
        df = raw[sym].copy()
        prepared[sym] = S.add_indicators(df, bench_ret)
        
    res = E.run(prepared)
    
    print("\n" + "="*95)
    print(f"{'SYMBOL':<15}{'ENTRY PRICE':>12}{'EXIT PRICE':>12}  {'RESULT':<10}{'ENTRY@':>8}{'EXIT@':>8}{'HELD':>6}")
    print("-"*95)
    
    for t in res["trades"]:
        sym_clean = t["symbol"].replace(".NS", "")
        # Calculate raw prices from the percentage returns (gross)
        # Entry price is t["entry_price"]
        # Exit price is t["exit_price"]
        # Result is determined by checking net_pct
        result = "WIN" if t["net_pct"] > 0 else "LOSS"
        entry_time = t["entry_ts"].strftime("%H:%M")
        exit_time = t["exit_ts"].strftime("%H:%M")
        held = f"{t['minutes']:.0f}m"
        
        print(f"{sym_clean:<15}{t['entry_price']:>12.2f}{t['exit_price']:>12.2f}  {result:<10}{entry_time:>8}{exit_time:>8}{held:>6}")
        
    print("="*95)
    print(f"SUMMARY: Wins: {res['summary']['longs'] - res['summary']['by_reason'].get('stop', 0)} | Losses: {res['summary']['by_reason'].get('stop', 0)} | Total: {res['summary']['n_trades']}")
    print("="*95)

if __name__ == "__main__":
    main()
