"""
Simple peak/trough midpoint strategy test.
No warmup needed — just track running high and low.
BUY when price pulls back to midpoint. SL=trough, TARGET=peak.
SELL (reverse) when price bounces to midpoint. SL=peak, TARGET=trough.
"""
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

# 3-trade BUY: rise → pullback → entry at mid → target, repeat x3
buy_prices  = [10, 20, 30, 40, 50, 40, 30, 40, 50,
               60, 70, 80, 90, 100, 80, 70, 80, 90, 100,
               110, 120, 130, 140, 150, 130, 120, 130, 140, 150]

# 3-trade SELL: fall → bounce → entry at mid → target, repeat x3
sell_prices = [150, 140, 130, 120, 110, 120, 130, 120, 110,
               100, 90, 80, 70, 60, 70, 80, 90, 80, 70, 60,
               50, 40, 30, 20, 10, 20, 30, 40, 30, 20, 10]


def run(prices, side):
    peak = trough = None
    trade = None
    trades = []
    log = []
    stopped = False   # no new trades after SL hit

    for i, p in enumerate(prices):
        if peak is None:
            peak = trough = p
        elif side == 'BUY':
            if p > peak:   peak = p; trough = p   # new peak → reset trough for this swing
            elif p < trough: trough = p
        else:  # SELL
            if p < trough: trough = p; peak = p   # new trough → reset peak for this swing
            elif p > peak: peak = p

        mid = (peak + trough) / 2 if peak != trough else None

        action = ''

        # exit check
        if trade:
            if trade['side'] == 'BUY':
                if p <= trade['sl']:
                    action = 'SL HIT — no more trades'
                    trade['exit']=p; trade['result']='SL'; trades.append(trade); trade=None
                    stopped = True          # SL → stop trading
                elif p >= trade['target']:
                    action = 'TARGET'
                    trade['exit']=p; trade['result']='TARGET'; trades.append(trade); trade=None
                    peak = p; trough = p    # TARGET → restart from exit price
            else:
                if p >= trade['sl']:
                    action = 'SL HIT — no more trades'
                    trade['exit']=p; trade['result']='SL'; trades.append(trade); trade=None
                    stopped = True
                elif p <= trade['target']:
                    action = 'TARGET'
                    trade['exit']=p; trade['result']='TARGET'; trades.append(trade); trade=None
                    peak = p; trough = p

        # entry check — only if not stopped
        if not trade and not stopped and mid is not None:
            if side == 'BUY'  and p <= mid and p > trough:
                sl  = round(p * 0.995, 2)   # -0.5%
                tgt = round(p * 1.005, 2)   # +0.5%
                trade = {'side':'BUY',  'entry':p, 'entry_i':i, 'sl':sl, 'target':tgt}
                action = f'ENTRY  SL={sl}  TARGET={tgt}'
                peak = p; trough = p
            elif side == 'SELL' and p >= mid and p < peak:
                sl  = round(p * 1.005, 2)   # +0.5%
                tgt = round(p * 0.995, 2)   # -0.5%
                trade = {'side':'SELL', 'entry':p, 'entry_i':i, 'sl':sl, 'target':tgt}
                action = f'ENTRY  SL={sl}  TARGET={tgt}'
                peak = p; trough = p

        log.append({'i':i,'p':p,'peak':peak,'trough':trough,'mid':mid,'action':action,'stopped':stopped})

    if trade:
        trade['exit']=prices[-1]; trade['result']='OPEN'; trades.append(trade)

    return trades, log


def plot(ax, prices, trades, title, color):
    ax.set_facecolor('#111318')
    ax.tick_params(colors='#9ca3af')
    for sp in ax.spines.values(): sp.set_edgecolor('#1e2027')

    xs = list(range(len(prices)))
    ax.plot(xs, prices, color=color, lw=2, marker='o', ms=5, zorder=3)

    pmin, pmax = min(prices), max(prices)
    label_y_step = (pmax - pmin) * 0.22   # stagger info boxes vertically

    win = los = 0
    for idx, t in enumerate(trades):
        ei = t['entry_i']
        ep = t['entry']
        xi = next((j for j, p in enumerate(prices) if j > ei and
                   ((t['side']=='BUY'  and (p <= t['sl'] or p >= t['target'])) or
                    (t['side']=='SELL' and (p >= t['sl'] or p <= t['target'])))), len(prices)-1)
        xp = prices[xi]
        col = '#4ade80' if t['result'] == 'TARGET' else '#f87171'
        if t['result'] == 'TARGET': win += 1
        elif t['result'] == 'SL':   los += 1

        # entry + exit vertical markers
        ax.axvline(ei, color='#fbbf24', lw=1, ls='--', alpha=0.5)
        ax.axvline(xi, color=col,       lw=1, ls='--', alpha=0.5)
        ax.plot(ei, ep, marker='^' if t['side']=='BUY' else 'v', ms=10,
                color='#fbbf24', zorder=5)
        ax.plot(xi, xp, marker='x', ms=9, color=col, zorder=5, mew=2)

        # SL and TARGET lines on the chart
        ax.hlines(t['sl'],     ei, xi, colors='#f87171', lw=1.5, ls='--', alpha=0.85)
        ax.hlines(t['target'], ei, xi, colors='#4ade80', lw=1.5, ls='--', alpha=0.85)

        # info box staggered below/above price line
        box_y = pmin - label_y_step * (1 + idx % 3)
        box_txt = f"#{idx+1} ENTRY {ep:.1f} | SL {t['sl']:.2f} | TGT {t['target']:.2f} | {t['result']} {xp:.1f}"
        ax.annotate(box_txt, xy=(ei, ep), xytext=((ei+xi)/2, box_y),
                    fontsize=8, color='#e5e7eb',
                    bbox=dict(boxstyle='round,pad=0.3', fc='#1e2330', ec=col, lw=1),
                    arrowprops=dict(arrowstyle='->', color='#fbbf24', lw=0.8),
                    ha='center', va='top')

    total = win + los
    ax.set_xticks(xs)
    ax.set_xticklabels([str(p) for p in prices], color='#9ca3af', fontsize=8)
    ax.set_title(f"{title}  |  {total} trades  {win}W {los}L", color='#fbbf24', fontsize=11)
    ax.set_ylabel('Price', color='#9ca3af')
    ax.grid(color='#1e2027', lw=0.5)
    ax.set_xlim(-0.5, len(prices) + 0.5)
    ax.set_ylim(pmin - label_y_step * 5, pmax + label_y_step)


# run both scenarios
buy_trades,  buy_log  = run(buy_prices,  'BUY')
sell_trades, sell_log = run(sell_prices, 'SELL')

# print tick-by-tick
for label, log, trades in [('BUY scenario', buy_log, buy_trades), ('SELL scenario', sell_log, sell_trades)]:
    print(f'\n--- {label} ---')
    print(f"{'i':>3}  {'price':>5}  {'peak':>5}  {'trough':>6}  {'mid':>5}  action")
    for r in log:
        mid = f"{r['mid']:.0f}" if r['mid'] else '-'
        print(f"  {r['i']:2d}   {r['p']:5.0f}   {r['peak']:5.0f}   {r['trough']:6.0f}   {mid:>5}  {r['action']}")
    print()
    for t in trades:
        print(f"  {t['side']}  entry={t['entry']}  SL={t['sl']}  TARGET={t['target']}  exit={t['exit']}  {t['result']}")

# plot
fig, axes = plt.subplots(2, 1, figsize=(11, 8))
fig.patch.set_facecolor('#0d0f14')
plot(axes[0], buy_prices,  buy_trades,  'BUY — 3 trades (entry at 50% pullback, SL=-0.5%, TGT=+0.5%)', '#4ade80')
plot(axes[1], sell_prices, sell_trades, 'SELL — 3 trades (entry at 50% bounce, SL=+0.5%, TGT=-0.5%)',  '#f87171')
plt.tight_layout(pad=2)
plt.savefig('test_simple.png', dpi=150, facecolor='#0d0f14', bbox_inches='tight')
print('\nSaved: test_simple.png')
