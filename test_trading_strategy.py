"""
Test the trading strategy logic with synthetic tick data.
Simulates the exact same Buying/Selling classification and 0.2% pullback entry
logic used in the Trading tab.

Usage:
    python test_trading_strategy.py
"""
import numpy as np
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches

# ── Strategy constants (mirror JS) ───────────────────────────────────────────
TD_PULL      = 0.002  # entry at 50% of swing (midpoint)
TD_SL        = 0.001  # 0.1% stop loss
TD_TARGET    = 0.002  # 0.2% profit target
TD_SWING_MIN = 0.003  # swing (H-L) must be >= 0.3% before midpoint entry is valid
RATIO_BUY  = 1.2    # buyVol/sellVol >= 1.2 → bullish
RATIO_SELL = 0.8    # buyVol/sellVol <= 0.8 → bearish
DRIFT_MIN  = 5      # |upTicks - downTicks| in last 20 to count as drifting
CHG_THRESH = 0.001  # 0.1% chg from prevClose for up/dn


# ── Tick generation ────────────────────────────────────────────────────────────

def gen_uptrend(n=450, base=1000.0, seed=42):
    """Phase1: up 150t (+12%) → peak.
       Phase2: pullback 150t (-8%) → hits 50% retracement → BUY entry.
       Phase3: resume up 150t (+8%) → hits target (original peak)."""
    rng = np.random.default_rng(seed)
    prices = [base]
    buy_vols = [100000]
    sell_vols = [80000]
    for i in range(1, n):
        if   i < 150: phase = 1   # up
        elif i < 300: phase = 2   # pullback
        else:         phase = 3   # resume up
        drift = {1: 0.0008, 2: -0.0005, 3: 0.0006}[phase]
        noise = rng.normal(0, 0.0004)  # enough zigzag so not all pullback ticks go down
        prices.append(prices[-1] * (1 + drift + noise))
        # order book oscillates — net buy pressure during up, net sell pressure during pullback
        bv_delta = int(rng.integers(-200, 2500)) if phase != 2 else int(rng.integers(-400, 1000))
        sv_delta = int(rng.integers(-500,  600)) if phase != 2 else int(rng.integers(-100,  800))
        buy_vols.append(max(10000, buy_vols[-1] + bv_delta))
        sell_vols.append(max(10000, sell_vols[-1] + sv_delta))
    return np.array(prices), np.array(buy_vols), np.array(sell_vols)


def gen_downtrend(n=450, base=1000.0, seed=99):
    """Phase1: down 150t (-12%) → trough.
       Phase2: bounce 150t (+8%) → hits 50% retracement → SELL entry.
       Phase3: resume down 150t (-8%) → hits target (original trough)."""
    rng = np.random.default_rng(seed)
    prices = [base]
    buy_vols = [80000]
    sell_vols = [120000]
    for i in range(1, n):
        if   i < 150: phase = 1   # down
        elif i < 300: phase = 2   # bounce
        else:         phase = 3   # resume down
        drift = {1: -0.0008, 2: 0.0005, 3: -0.0006}[phase]
        noise = rng.normal(0, 0.0004)
        prices.append(prices[-1] * (1 + drift + noise))
        bv_delta = int(rng.integers(100, 400))  if phase != 2 else int(rng.integers(400, 800))
        sv_delta = int(rng.integers(800, 2000)) if phase != 2 else int(rng.integers(300, 600))
        buy_vols.append(buy_vols[-1] + bv_delta)
        sell_vols.append(sell_vols[-1] + sv_delta)
    return np.array(prices), np.array(buy_vols), np.array(sell_vols)


# ── Signal logic (mirrors JS tdSignal) ────────────────────────────────────────

def compute_signal(ticks, prev_close, ltp, buy_vol, sell_vol):
    """Returns 'Buying', 'Selling', or None.
    Ratio+chgPct only — no drifting — so a pullback on a buy-dominant stock
    doesn't flip classification to 'Selling' just because ticks trend down."""
    if len(ticks) < 3:
        return None
    chg_pct = (ltp - prev_close) / prev_close * 100 if prev_close else 0
    ratio = buy_vol / sell_vol if sell_vol > 0 else 0
    up = chg_pct > 0.1
    dn = chg_pct < -0.1
    last20 = ticks[-20:]
    sell_growing = (sum(1 for t in last20 if t['dSell'] and t['dSell'] > 0) > len(last20) * 0.6)
    if up and ratio >= RATIO_BUY and not sell_growing:
        return 'Buying'
    if dn and ratio <= RATIO_SELL:
        return 'Selling'
    return None


# ── Strategy runner ────────────────────────────────────────────────────────────

def run_strategy(prices, buy_vols, sell_vols, label):
    prev_close = prices[0]
    ticks = []
    prev_bv = prev_sv = None

    state = {'ever_buy': False, 'ever_sell': False, 'class': None}
    peak = trough = None
    trade = None
    trades = []

    signals = []   # per tick: 'Buying'|'Selling'|None
    classes = []   # per tick: 'buyer'|'seller'|'ignored'|None

    for i, (ltp, bv, sv) in enumerate(zip(prices, buy_vols, sell_vols)):
        dBuy  = (bv - prev_bv)  if prev_bv is not None else None
        dSell = (sv - prev_sv)  if prev_sv is not None else None
        chg_pct = (ltp - prev_close) / prev_close * 100

        if dBuy != 0 or dSell != 0:
            ticks.append({'ltp': ltp, 'buyVol': bv, 'sellVol': sv,
                          'chgPct': chg_pct, 'ratio': bv/sv if sv else 0,
                          'dBuy': dBuy, 'dSell': dSell})

        prev_bv = bv; prev_sv = sv

        # classify
        sig = compute_signal(ticks, prev_close, ltp, bv, sv)
        signals.append(sig)

        if state['class'] != 'ignored':
            if sig == 'Buying':  state['ever_buy'] = True
            if sig == 'Selling': state['ever_sell'] = True
            if state['ever_buy'] and state['ever_sell']:
                state['class'] = 'ignored'; peak = None; trough = None
            elif state['ever_buy'] and state['class'] != 'buyer':
                state['class'] = 'buyer'; peak = ltp
            elif state['ever_sell'] and state['class'] != 'seller':
                state['class'] = 'seller'; trough = ltp

        cls = state['class']
        classes.append(cls)

        # running max and min — both accumulate independently, never reset each other
        if cls in ('buyer', 'seller'):
            if peak is None or ltp > peak:     peak = ltp
            if trough is None or ltp < trough: trough = ltp

        # check exit
        if trade:
            hit = None
            if trade['side'] == 'BUY':
                if ltp <= trade['sl']:       hit = 'SL'
                elif ltp >= trade['target']: hit = 'TARGET'
            else:
                if ltp >= trade['sl']:       hit = 'SL'
                elif ltp <= trade['target']: hit = 'TARGET'
            if hit:
                trade['exit_i'] = i; trade['exit_price'] = ltp; trade['result'] = hit
                trades.append(trade); trade = None
                peak = ltp; trough = ltp   # restart from exit

        # check entry — 50% retracement (midpoint of peak/trough range)
        # SL = -0.5% from entry; target = +0.5% from entry
        if not trade and peak is not None and trough is not None and peak != trough:
            mid = (peak + trough) / 2
            if cls == 'buyer' and ltp <= mid:
                sl  = round(ltp * 0.995, 2)
                tgt = round(ltp * 1.005, 2)
                trade = {'side':'BUY','entry':ltp,'entry_i':i,
                         'sl':sl,'target':tgt,
                         'peak_at_entry':peak,'trough_at_entry':trough,'mid':mid}
                peak = ltp; trough = ltp
            elif cls == 'seller' and ltp >= mid:
                sl  = round(ltp * 1.005, 2)
                tgt = round(ltp * 0.995, 2)
                trade = {'side':'SELL','entry':ltp,'entry_i':i,
                         'sl':sl,'target':tgt,
                         'peak_at_entry':peak,'trough_at_entry':trough,'mid':mid}
                peak = ltp; trough = ltp

    if trade:  # still open at end
        trade['exit_i'] = len(prices)-1; trade['exit_price'] = prices[-1]; trade['result'] = 'OPEN'
        trades.append(trade)

    return trades, signals, classes


# ── Plot ───────────────────────────────────────────────────────────────────────

def plot_scenario(ax, prices, trades, signals, classes, label, color):
    ax.set_facecolor('#111318')
    ax.tick_params(colors='#6b7280')
    for spine in ax.spines.values(): spine.set_edgecolor('#1e2027')
    ax.yaxis.label.set_color('#9ca3af')
    ax.title.set_color('#fbbf24')

    xs = np.arange(len(prices))
    ax.plot(xs, prices, color=color, lw=1.2, zorder=2, label='Price')

    # shade buying/selling regions
    for i, cls in enumerate(classes):
        if cls == 'buyer':
            ax.axvspan(i, i+1, color='#4ade80', alpha=0.05, lw=0)
        elif cls == 'seller':
            ax.axvspan(i, i+1, color='#f87171', alpha=0.05, lw=0)
        elif cls == 'ignored':
            ax.axvspan(i, i+1, color='#6b7280', alpha=0.05, lw=0)

    # plot trades
    win = los = 0
    price_range = max(prices) - min(prices)
    for idx, t in enumerate(trades):
        ei, xi = t['entry_i'], t['exit_i']
        ep, xp = t['entry'], t['exit_price']
        hit_col = '#4ade80' if t['result'] == 'TARGET' else ('#f87171' if t['result'] == 'SL' else '#fbbf24')
        if t['result'] == 'TARGET': win += 1
        elif t['result'] == 'SL':   los += 1

        lbl_offset = price_range * 0.03 * (idx % 3)   # stagger labels vertically

        # entry marker + label (staggered)
        ax.axvline(ei, color='#fbbf24', lw=0.8, alpha=0.5, ls=':')
        ax.text(ei + 1, ep + lbl_offset, f"ENTRY ₹{ep:.1f}", fontsize=7.5,
                color='#fbbf24', va='bottom')

        # SL line entry→exit only
        ax.hlines(t['sl'], ei, xi, colors='#f87171', lw=1.2, ls='--', alpha=0.9)
        ax.text(xi + 1, t['sl'] - lbl_offset, f"SL ₹{t['sl']:.1f}", fontsize=7.5,
                color='#f87171', va='center')

        # TARGET line entry→exit only
        ax.hlines(t['target'], ei, xi, colors='#4ade80', lw=1.2, ls='--', alpha=0.9)
        ax.text(xi + 1, t['target'] + lbl_offset, f"TGT ₹{t['target']:.1f}", fontsize=7.5,
                color='#4ade80', va='center')

        # exit marker
        ax.axvline(xi, color=hit_col, lw=0.8, alpha=0.5, ls=':')
        ax.text(xi + 1, xp - lbl_offset, f"{t['result']} ₹{xp:.1f}", fontsize=7.5,
                color=hit_col, va='top')

        ax.plot([ei, xi], [ep, xp], color=hit_col, lw=0.8, alpha=0.3, zorder=3, ls=':')

    total = win + los
    ax.set_title(f'{label}  |  trades: {total}  win: {win}  loss: {los}'
                 + (f'  WR: {win/total*100:.0f}%' if total else ''), fontsize=11)
    ax.set_xlabel('Tick', color='#6b7280')
    ax.set_ylabel('Price (₹)', color='#6b7280')
    ax.grid(color='#1e2027', lw=0.5)


def main():
    up_prices,   up_bv,   up_sv   = gen_uptrend()
    dn_prices,   dn_bv,   dn_sv   = gen_downtrend()

    up_trades, up_sigs, up_cls = run_strategy(up_prices,   up_bv,   up_sv,   'Uptrend')
    dn_trades, dn_sigs, dn_cls = run_strategy(dn_prices,   dn_bv,   dn_sv,   'Downtrend')

    fig, axes = plt.subplots(2, 1, figsize=(14, 9))
    fig.patch.set_facecolor('#0d0f14')

    plot_scenario(axes[0], up_prices,   up_trades, up_sigs, up_cls,
                  'Uptrend — Buyer classification + pullback entries', '#4ade80')
    plot_scenario(axes[1], dn_prices,   dn_trades, dn_sigs, dn_cls,
                  'Downtrend — Seller classification + rally entries', '#f87171')

    buy_patch    = mpatches.Patch(color='#4ade80', alpha=0.3, label='Buyer zone')
    sell_patch   = mpatches.Patch(color='#f87171', alpha=0.3, label='Seller zone')
    target_patch = mpatches.Patch(color='#4ade80', label='TARGET')
    sl_patch     = mpatches.Patch(color='#f87171', label='SL')
    entry_patch  = mpatches.Patch(color='#fbbf24', label='ENTRY')
    fig.legend(handles=[buy_patch, sell_patch, entry_patch, target_patch, sl_patch],
               loc='upper center', ncol=5, fontsize=9,
               facecolor='#1a1c20', edgecolor='#374151', labelcolor='white')

    plt.tight_layout(rect=[0, 0, 1, 0.95])
    plt.savefig('test_trading_strategy.png', dpi=150, bbox_inches='tight',
                facecolor='#0d0f14')
    print('Saved: test_trading_strategy.png')

    # print trade summary
    for label, trades in [('Uptrend', up_trades), ('Downtrend', dn_trades)]:
        print(f'\n--- {label} trades ---')
        for t in trades:
            pnl = (t['exit_price']-t['entry'])/t['entry']*(1 if t['side']=='BUY' else -1)*100
            print(f"  {t['side']:4s}  entry={t['entry']:.2f}  SL={t['sl']:.2f}  target={t['target']:.2f}"
                  f"  exit={t['exit_price']:.2f}  {t['result']:6s}  {pnl:+.3f}%"
                  f"  tick {t['entry_i']}->{t['exit_i']}")

    plt.show()


if __name__ == '__main__':
    main()
