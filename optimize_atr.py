import config, data as D, strategy as S, engine as E
import pandas as pd
import sys

def sweep():
    print("Loading cached 1m data for optimization sweep...")
    # Load cached 1m data
    raw = D.load_cached(interval="1m")
    if not raw or config.BENCHMARK not in raw:
        print("Error: Cached data missing. Run a yfinance 1m fetch first.")
        return
        
    bench = raw.get(config.BENCHMARK)
    bench_ret = None
    if bench is not None:
        bench_ret = bench.groupby("date")["close"].transform(lambda s: s / s.iloc[0] - 1.0)
        
    # Standard universe
    syms = [s for s in config.UNIVERSE if s in raw and s != config.BENCHMARK]
    
    # Define parameter grid
    modes = ["atr_pullback"]
    atr_drops = [1.0, 1.5, 2.0, 2.5]
    atr_bounces = [0.3, 0.5, 0.7]
    vol_mults = [1.0, 1.5, 2.0]
    nifty_filters = [True, False]
    
    # Risk profiles: (Target, Stop)
    risk_profiles = [
        (0.003, 0.003),  # 1:1 tight (0.3% / 0.3%)
        (0.005, 0.005),  # 1:1 medium (0.5% / 0.5%)
        (0.006, 0.003),  # 2:1 ratio (0.6% / 0.3%)
        (0.008, 0.004)   # 2:1 ratio (0.8% / 0.4%)
    ]
    
    results = []
    
    # Temporarily disable exits other than target/stop for pure testing
    config.TIME_STOP_MIN = 390
    config.USE_VWAP_STALL_EXIT = False
    config.MODE = "atr_pullback"
    
    total_runs = len(atr_drops) * len(atr_bounces) * len(vol_mults) * len(nifty_filters) * len(risk_profiles)
    print(f"Sweeping {total_runs} combinations...")
    
    count = 0
    for drop in atr_drops:
        for bounce in atr_bounces:
            for vol_m in vol_mults:
                for nifty in nifty_filters:
                    for target, stop in risk_profiles:
                        count += 1
                        
                        # Apply overrides
                        config.ATR_DROP_MULT = drop
                        config.ATR_BOUNCE_MULT = bounce
                        config.VOL_MULT = vol_m
                        config.USE_NIFTY_FILTER = nifty
                        config.TARGET_PCT = target
                        config.STOP_PCT = stop
                        
                        # Prepare data with overridden indicators
                        prepared = {}
                        for sym in syms:
                            df = raw[sym].copy()
                            prepared[sym] = S.add_indicators(df, bench_ret)
                            
                        # Run backtest
                        res = E.run(prepared)
                        s = res["summary"]
                        
                        if s.get("n_trades", 0) > 2:  # Filter out configs with too few trades to be significant
                            results.append({
                                "drop": drop,
                                "bounce": bounce,
                                "vol_mult": vol_m,
                                "nifty_filter": nifty,
                                "target": f"{target*100:.1f}%",
                                "stop": f"{stop*100:.1f}%",
                                "trades": s["n_trades"],
                                "win_rate": s["win_rate_pct"],
                                "gross_pct": s["gross_total_pct"],
                                "net_pct": s["net_total_pct"]
                            })
                            
    # Convert and sort
    df_results = pd.DataFrame(results)
    if df_results.empty:
        print("No configurations generated enough trades.")
        return
        
    df_results = df_results.sort_values(by="net_pct", ascending=False)
    
    print("\n" + "="*95)
    print(f"{'DROP':<6}{'BOUNCE':<8}{'VOL_M':<6}{'NIFTY':<8}{'TARGET':<8}{'STOP':<8}{'TRADES':<8}{'WIN%':<8}{'GROSS%':<10}{'NET%':<10}")
    print("-"*95)
    for idx, r in df_results.head(15).iterrows():
        print(f"{r['drop']:<6.1f}{r['bounce']:<8.1f}{r['vol_mult']:<6.1f}{str(r['nifty_filter']):<8}{r['target']:<8}{r['stop']:<8}{int(r['trades']):<8}{r['win_rate']:<8.1f}{r['gross_pct']:<+10.2f}{r['net_pct']:<+10.2f}")
    print("="*95)
    
    best = df_results.iloc[0]
    print(f"\nBEST CONFIGURATION:")
    print(f"  - Pullback Drop: {best['drop']}x ATR")
    print(f"  - Entry Bounce: {best['bounce']}x ATR")
    print(f"  - Vol Surge Gate: {best['vol_mult']}x average")
    print(f"  - Nifty Filter: {best['nifty_filter']}")
    print(f"  - Target/Stop: {best['target']} / {best['stop']} (R:R Ratio)")
    print(f"  - Performance: {best['trades']} trades | Win Rate: {best['win_rate']:.1f}% | Net Return: {best['net_pct']:+.2f}% (AFTER slippage and brokerage)")
    print("="*95)

if __name__ == "__main__":
    sweep()
