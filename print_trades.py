import matplotlib
matplotlib.use("Agg")
import test_trading_strategy as mod

up_p, up_bv, up_sv = mod.gen_uptrend()
dn_p, dn_bv, dn_sv = mod.gen_downtrend()
up_tr, _, _ = mod.run_strategy(up_p, up_bv, up_sv, "Uptrend")
dn_tr, _, _ = mod.run_strategy(dn_p, dn_bv, dn_sv, "Downtrend")

for label, trades in [("Uptrend", up_tr), ("Downtrend", dn_tr)]:
    print("--- " + label + " ---")
    for tr in trades:
        pnl = (tr["exit_price"]-tr["entry"])/tr["entry"]*(1 if tr["side"]=="BUY" else -1)*100
        mid = tr.get('mid', 0)
        print(f"  {tr['side']}  H={tr.get('peak_at_entry',0):.2f} L={tr.get('trough_at_entry',0):.2f} mid={mid:.2f}"
              f"  entry={tr['entry']:.2f}  SL={tr['sl']:.2f}  target={tr['target']:.2f}"
              f"  exit={tr['exit_price']:.2f}  {tr['result']}  {pnl:+.3f}%")
