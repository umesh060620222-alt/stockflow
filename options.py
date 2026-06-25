"""Options idea for the top picks — a directional trade derived from the
recommendation, priced with Black-Scholes off each stock's OWN recent realized
volatility (NSE option chains aren't in yfinance, and a live NSE feed is fragile
on a cloud host — so premiums here are THEORETICAL ESTIMATES, not live quotes).

Top BUY  -> at-the-money CALL.   Top SELL -> at-the-money PUT.
Max loss on a long option = the premium paid; that framing is surfaced to the UI.
"""
from __future__ import annotations
import math, datetime as dt

RISK_FREE = {"IN": 0.065, "US": 0.045}     # rough policy-rate proxies
TRADING_DAYS = 252


def _norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_price(S, K, T, r, sigma, kind):
    """Black-Scholes European option price + delta. T in years, sigma annualized."""
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        intrinsic = max(0.0, (S - K) if kind == "call" else (K - S))
        return intrinsic, (1.0 if kind == "call" and S > K else 0.0)
    d1 = (math.log(S / K) + (r + sigma * sigma / 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if kind == "call":
        price = S * _norm_cdf(d1) - K * math.exp(-r * T) * _norm_cdf(d2)
        delta = _norm_cdf(d1)
    else:
        price = K * math.exp(-r * T) * _norm_cdf(-d2) - S * _norm_cdf(-d1)
        delta = _norm_cdf(d1) - 1.0
    return price, delta


def realized_vol(close_series, lookback=30):
    """Annualized realized vol from daily log returns; clamped to a sane band."""
    try:
        c = close_series.dropna().tail(lookback + 1)
        if len(c) < 10:
            return 0.30
        rets = [math.log(c.iloc[i] / c.iloc[i - 1]) for i in range(1, len(c))]
        mean = sum(rets) / len(rets)
        var = sum((x - mean) ** 2 for x in rets) / (len(rets) - 1)
        sigma = math.sqrt(var) * math.sqrt(TRADING_DAYS)
        return max(0.10, min(1.50, sigma))
    except Exception:
        return 0.30


def _strike_step(price):
    for hi, step in ((100, 2.5), (250, 5), (500, 10), (1000, 20), (2500, 50), (5000, 100)):
        if price < hi:
            return step
    return 100.0


def _atm_strike(price):
    step = _strike_step(price)
    return round(price / step) * step


def next_monthly_expiry(today, market):
    """NSE = last Thursday of the month; US = 3rd Friday. Rolls to next month
    once this month's expiry has passed. Returns (date, days_to_expiry)."""
    def last_weekday(year, month, weekday):           # weekday: Mon=0 .. Sun=6
        d = dt.date(year, month, 28)
        while d.month == month:
            d += dt.timedelta(days=1)
        d -= dt.timedelta(days=1)                      # last day of month
        while d.weekday() != weekday:
            d -= dt.timedelta(days=1)
        return d

    def third_friday(year, month):
        d = dt.date(year, month, 1)
        fridays = 0
        while True:
            if d.weekday() == 4:
                fridays += 1
                if fridays == 3:
                    return d
            d += dt.timedelta(days=1)

    y, mo = today.year, today.month
    pick = last_weekday(y, mo, 3) if market == "IN" else third_friday(y, mo)
    if pick <= today:                                  # this month's expiry gone -> next month
        y, mo = (y + 1, 1) if mo == 12 else (y, mo + 1)
        pick = last_weekday(y, mo, 3) if market == "IN" else third_friday(y, mo)
    return pick, (pick - today).days


def suggest(symbol, price, close_series, direction, market="IN", cur="₹"):
    """Build the suggested option trade for a pick.
    direction: 'call' (bullish, top buy) or 'put' (bearish, top sell)."""
    if price is None or price <= 0:
        return None
    today = dt.date.today()
    expiry, dte = next_monthly_expiry(today, market)
    T = max(dte, 1) / 365.0
    r = RISK_FREE.get(market, 0.05)
    iv = realized_vol(close_series) if close_series is not None else 0.30
    strike = _atm_strike(price)
    premium, delta = bs_price(price, strike, T, r, iv, direction)
    premium = round(premium, 2)
    if direction == "call":
        breakeven = round(strike + premium, 2)
        move_needed = round((breakeven / price - 1) * 100, 1)
    else:
        breakeven = round(strike - premium, 2)
        move_needed = round((1 - breakeven / price) * 100, 1)
    return {
        "kind": direction.upper(),                     # CALL | PUT
        "side": "BUY",                                 # long premium only — defined risk
        "strike": strike,
        "expiry": str(expiry),
        "dte": dte,
        "premium": premium,                            # per share (×lot size for total cost)
        "iv_pct": round(iv * 100, 1),
        "delta": round(delta, 2),
        "breakeven": breakeven,
        "move_needed_pct": move_needed,                # underlying move to breakeven by expiry
        "max_loss": premium,                           # long option: most you can lose = premium
        "currency": cur,
        "note": "Theoretical (Black-Scholes off recent realized vol) — not a live quote. "
                "Premium per share is your max loss; ×lot size for the full ticket.",
    }
