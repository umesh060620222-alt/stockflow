"""Run peak/trough midpoint strategy on today's real RELIANCE data."""
import yfinance as yf
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

# ── fetch data ─────────────────────────────────────────────────────────────────
ticker = 'BAJFINANCE.NS'
df = yf.download(ticker, period='1d', interval='1m', progress=False)
if isinstance(df.columns, pd.MultiIndex):
    df.columns = df.columns.get_level_values(0)
df = df.dropna()
# convert to IST and strip tz so matplotlib shows IST times
df.index = df.index.tz_convert('Asia/Kolkata').tz_localize(None)
prices = df['Close'].values.tolist()
times  = df.index.tolist()
print(f"{len(prices)} candles  open={prices[0]:.1f}  close={prices[-1]:.1f}")

# ── strategy (same logic as test_simple) ──────────────────────────────────────
def run(prices, side):
    peak = trough = None
    trade = None
    trades = []
    stopped = False

    for i, p in enumerate(prices):
        if peak is None:
            peak = trough = p
        elif side == 'BUY':
            if p > peak:   peak = p; trough = p
            elif p < trough: trough = p
        else:
            if p < trough: trough = p; peak = p
            elif p > peak: peak = p

        mid = (peak + trough) / 2 if peak != trough else None

        if trade:
            if trade['side'] == 'BUY':
                if p <= trade['sl']:
                    trade.update(exit_i=i, exit_p=p, result='SL')
                    trades.append(trade); trade = None; stopped = True
                elif p >= trade['target']:
                    trade.update(exit_i=i, exit_p=p, result='TARGET')
                    trades.append(trade); trade = None
                    peak = p; trough = p
            else:
                if p >= trade['sl']:
                    trade.update(exit_i=i, exit_p=p, result='SL')
                    trades.append(trade); trade = None; stopped = True
                elif p <= trade['target']:
                    trade.update(exit_i=i, exit_p=p, result='TARGET')
                    trades.append(trade); trade = None
                    peak = p; trough = p

        if not trade and not stopped and mid is not None:
            if side == 'BUY' and p <= mid and p > trough:
                sl  = round(p * 0.995, 2)
                tgt = round(p * 1.005, 2)
                trade = dict(side='BUY', entry_i=i, entry=p, sl=sl, target=tgt)
                peak = p; trough = p
            elif side == 'SELL' and p >= mid and p < peak:
                sl  = round(p * 1.005, 2)
                tgt = round(p * 0.995, 2)
                trade = dict(side='SELL', entry_i=i, entry=p, sl=sl, target=tgt)
                peak = p; trough = p

    if trade:
        trade.update(exit_i=len(prices)-1, exit_p=prices[-1], result='OPEN')
        trades.append(trade)

    return trades

buy_trades  = run(prices, 'BUY')
sell_trades = run(prices, 'SELL')

for label, trades in [('BUY', buy_trades), ('SELL', sell_trades)]:
    print(f"\n{label} trades:")
    for t in trades:
        pnl = (t['exit_p']-t['entry'])/t['entry']*(1 if t['side']=='BUY' else -1)*100
        print(f"  {t['side']}  entry={t['entry']:.2f}  SL={t['sl']:.2f}  TGT={t['target']:.2f}"
              f"  exit={t['exit_p']:.2f}  {t['result']}  {pnl:+.3f}%"
              f"  {times[t['entry_i']].strftime('%H:%M')} -> {times[t['exit_i']].strftime('%H:%M')}")

# ── plot ───────────────────────────────────────────────────────────────────────
fig, ax = plt.subplots(figsize=(16, 7))
fig.patch.set_facecolor('#0d0f14')
ax.set_facecolor('#111318')
ax.tick_params(colors='#9ca3af')
for sp in ax.spines.values(): sp.set_edgecolor('#1e2027')

ax.plot(times, prices, color='#60a5fa', lw=1.0, zorder=2)

pmin, pmax = min(prices), max(prices)
all_trades = [(t, '#4ade80') for t in buy_trades] + [(t, '#f87171') for t in sell_trades]
all_trades.sort(key=lambda x: x[0]['entry_i'])

import matplotlib.patches as mpatches
import matplotlib.dates as mdates2

tick = (pmax - pmin) * 0.008   # label offset above circle

def circle_label(x, y, label, price, color):
    ax.plot(x, y, 'o', ms=13, color='none', markeredgecolor=color, markeredgewidth=1.8, zorder=6)
    ax.text(x, y + tick*2, f"{label}\n{price:.2f}", fontsize=7.5, color=color,
            ha='center', va='bottom', zorder=7,
            bbox=dict(boxstyle='round,pad=0.2', fc='#0d0f14', ec='none'))

for idx, (t, ecol) in enumerate(all_trades):
    ei, xi = t['entry_i'], t['exit_i']
    et, xt = times[ei], times[xi]
    ep, xp = t['entry'], t['exit_p']
    rcol = '#4ade80' if t['result'] == 'TARGET' else ('#f87171' if t['result'] == 'SL' else '#fbbf24')

    # circles at entry time: gold=ENTRY, red=SL, green=TGT
    circle_label(et, ep,          'ENTRY', ep,          '#fbbf24')
    circle_label(et, t['sl'],     'SL',    t['sl'],     '#f87171')
    circle_label(et, t['target'], 'TGT',   t['target'], '#4ade80')

    # circle at exit (result)
    circle_label(xt, xp, t['result'], xp, rcol)

ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))
ax.xaxis.set_major_locator(mdates.MinuteLocator(byminute=[0,30]))
ax.set_title(f'{ticker} — Today 1-min  |  strategy: 50% pullback entry  SL±0.5%',
             color='#fbbf24', fontsize=12)
ax.set_ylabel('Price (₹)', color='#9ca3af')
ax.grid(color='#1e2027', lw=0.5)
plt.tight_layout()
plt.savefig('strategy_live.png', dpi=150, facecolor='#0d0f14', bbox_inches='tight')
print('\nSaved: strategy_live.png')
