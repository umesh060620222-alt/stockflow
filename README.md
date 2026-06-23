# stockflow — intraday momentum paper-trading engine

Tests one question for ₹0: **does a ~10-minute momentum signal predict real
intraday moves on liquid NSE stocks, *after* costs?** Pulls today's bars (free,
no broker), replays them bar-by-bar through entry/exit + risk gates, and reports
net-of-cost expectancy. Nothing here trades real money.

## Run
```
pip install -r requirements.txt
python run.py            # fetch today's bars from yfinance, replay, report
python run.py --cached   # reuse last fetch (offline) — for fast threshold tuning
python run.py --csv      # also dump trades.csv
```

## Web UI
```
python app.py     # http://127.0.0.1:8000 — set params, pick momentum/mean-reversion, Run
```
Tune thresholds in the browser and read the net-of-cost verdict + trades live.

## Deploy on Railway
Push this repo, create a Railway service from it. It runs `python app.py` (Procfile),
binds `0.0.0.0:$PORT`. The deployed app works on **yfinance** out of the box (no
secrets). For **zerodha** on the server you also need the daily access token there
(local-only today) — left as a follow-up; `kite_secrets.py` / `access_token.json`
are gitignored and never pushed.

## The loop
1. `python run.py` after market close → get the day's verdict.
2. If `expectancy < 0`: tune `config.py` (VOL_MULT, MOM_LOOKBACK, TARGET/STOP, etc.)
   and re-run `python run.py --cached` (instant, offline) until expectancy turns
   positive **on held-out days** — not just curve-fit to one session.
3. Collect many sessions (run daily, or pull `period="5d"`/`"7d"` with `interval="1m"`).
   One day / 31 trades is NOT significant. Want 100s of trades across many days.
4. Only once it's robustly +EV across many days → wire a broker WebSocket for live
   paper trading, then small real capital with manual approval.

## Signal (config.py)
Entry (on bar close): volume > `VOL_MULT`× avg **and** price across VWAP **and**
momentum **and** relative-strength vs Nifty. Exit: target / stop / time-stop /
VWAP-stall. Costs (`COST_PCT` + `SLIPPAGE_PCT`) subtracted from every trade.

## Reality
- Most exits being `time` = the move isn't following through → entry needs work.
- Gross can be positive while net is negative — costs are the enemy. That's the point.
- yfinance intraday ≈ a recorded live feed for this test, but has minor gaps; a
  broker feed is the upgrade for true real-time.
- Going live: this is the swap point in `data.py` (fetch → WebSocket recorder).
```
```
