import matplotlib; matplotlib.use('Agg')
import test_trading_strategy as mod

up_p, up_bv, up_sv = mod.gen_uptrend()
prev_close = up_p[0]
ticks = []
state = {'ever_buy': False, 'ever_sell': False, 'class': None}
prev_bv = prev_sv = None

for i, (ltp, bv, sv) in enumerate(zip(up_p, up_bv, up_sv)):
    dBuy  = (bv - prev_bv)  if prev_bv is not None else None
    dSell = (sv - prev_sv)  if prev_sv is not None else None
    chg_pct = (ltp - prev_close) / prev_close * 100
    if dBuy != 0 or dSell != 0:
        ticks.append({'ltp': ltp, 'buyVol': bv, 'sellVol': sv,
                      'chgPct': chg_pct, 'ratio': bv/sv if sv else 0,
                      'dBuy': dBuy, 'dSell': dSell})
    prev_bv = bv; prev_sv = sv
    if state['class'] != 'ignored' and len(ticks) >= 10:
        sig = mod.compute_signal(ticks, prev_close, ltp, bv, sv)
        if sig == 'Buying':  state['ever_buy'] = True
        if sig == 'Selling': state['ever_sell'] = True
        if state['ever_buy'] and state['ever_sell']:   state['class'] = 'ignored'
        elif state['ever_buy']:  state['class'] = 'buyer'
        elif state['ever_sell']: state['class'] = 'seller'
    else:
        sig = None
    if i in [10,30,80,148,149,151,160,175,200,250,300,350,400,449]:
        phase = 1 if i < 150 else (2 if i < 300 else 3)
        print(f"tick={i:3d} p{phase} ltp={ltp:.1f} sig={str(sig):8s} cls={str(state['class']):8s} eB={state['ever_buy']} eS={state['ever_sell']}")
