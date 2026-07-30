"""Nifty Options Auto-Trader — Live and Paper trading for the Pullback Strategy.

Polls Nifty Spot Index at 1-second intervals, compiles 1-minute candles,
calculates 15-EMA + ATR, manages the multi-directional state machine,
and triggers option positions using actual premium quotes from Zerodha.
"""
from __future__ import annotations
import datetime as dt
import threading
import time
import math
import logging
import pandas as pd
import pytz
import config
import zerodha as Z

log = logging.getLogger("options_autotrader")

class OptionsAutoTrader:
    def __init__(self):
        self.lock = threading.RLock()
        self.running = False
        self.mode = "paper"  # "paper" | "live"
        self.capital = 40000.0
        self.lot_size_mode = "auto" # "auto" | "fixed"
        self.fixed_lots = 1
        self.state = "idle"  # idle | warmup | scanning | in-trade | error
        self.logs = []
        self.candles = []    # list of dicts: {"open", "high", "low", "close", "atr", "nifty_ema", "date"}
        self.active_trade = None  # None or dict of active trade parameters
        self.completed_trades = []
        self.vol_pcr_active_trade = None
        self.vol_pcr_completed_trades = []
        self.prev_vol_pcr = None
        self.vol_pcr_above_ce_ticks = 0
        self.vol_pcr_below_pe_ticks = 0
        self.vol_pcr_mode = 'paper'
        self.thread = None
        self._stop = False
        self.nifty_open = None
        self.l_stage = 1
        self.l_peak = None
        self.l_trough = None
        self.l_peak_atr = None
        self.s_stage = 1
        self.s_trough = None
        self.s_peak = None
        self.s_trough_atr = None
        self.fut_tok = None
        self.has_vol_conf = True
        self._oi_metrics = {"pcr": 1.0, "pe_oi": 0, "ce_oi": 0, "strike": 0, "oi_trend": "NEUTRAL"}
        self._load_state()

    def _ist(self):
        return dt.datetime.now(pytz.timezone("Asia/Kolkata"))

    def _save_state(self):
        try:
            import json
            state = {
                "date": self._ist().strftime("%Y-%m-%d"),
                "completed_trades": self.completed_trades,
                "logs": self.logs,
                "nifty_open": self.nifty_open,
                "active_trade": self.active_trade,
            "vol_pcr_active_trade": self.vol_pcr_active_trade,
            "vol_pcr_completed_trades": self.vol_pcr_completed_trades,
            "vol_pcr_mode": self.vol_pcr_mode
            }
            with open("state_autotrader.json", "w") as f:
                json.dump(state, f)
        except Exception as e:
            log.error(f"Failed to save autotrader state: {e}")

    def _load_state(self):
        try:
            import os, json
            if os.path.exists("state_autotrader.json"):
                with open("state_autotrader.json", "r") as f:
                    state = json.load(f)
                today = self._ist().strftime("%Y-%m-%d")
                if state.get("date") == today:
                    self.completed_trades = state.get("completed_trades", [])
                    self.logs = state.get("logs", [])
                    self.nifty_open = state.get("nifty_open")
                    self.active_trade = state.get("active_trade")
                    self.vol_pcr_active_trade = state.get("vol_pcr_active_trade")
                    self.vol_pcr_completed_trades = state.get("vol_pcr_completed_trades", [])
                    self.vol_pcr_mode = state.get("vol_pcr_mode", "paper")
                    ts = self._ist().strftime("%H:%M:%S")
                    self.logs.append(f"[{ts}] Restored today's trade history and logs from state file.")
        except Exception as e:
            log.error(f"Failed to load autotrader state: {e}")

    def _log(self, msg: str):
        ts = self._ist().strftime("%H:%M:%S")
        entry = f"[{ts}] {msg}"
        with self.lock:
            self.logs.append(entry)
            if len(self.logs) > 300:
                self.logs.pop(0)
            self._save_state()
        log.info(entry)

    def start(self, capital=40000.0, mode="paper", lot_size_mode="auto", fixed_lots=1, vol_pcr_mode="paper"):
        # Wait for old thread to terminate if it exists and is alive
        if hasattr(self, "thread") and self.thread and self.thread.is_alive():
            self._stop = True
            self.thread.join(timeout=3.0)
            
        with self.lock:
            if self.running:
                return False
            self.running = True
            self.mode = mode.lower()
            self.vol_pcr_mode = vol_pcr_mode.lower()
            self.capital = float(capital)
            self.lot_size_mode = lot_size_mode.lower()
            self.fixed_lots = int(fixed_lots)
            self.state = "warmup"
            self._stop = False
            self.candles = []
            self.active_trade = None
            self.nifty_open = None
            self.logs = []
            lot_mode_str = "AUTO (Capital-based)" if self.lot_size_mode == "auto" else f"FIXED ({self.fixed_lots} lots)"
            self._log(f"Starting Options Auto-Trader in {self.mode.upper()} mode with Rs. {self.capital} capital (Lot Sizing: {lot_mode_str})...")
            self.thread = threading.Thread(target=self._loop, daemon=True)
            self.thread.start()
            return True

    def stop(self):
        with self.lock:
            if not self.running:
                return False
            self._stop = True
            self.running = False
            self.state = "idle"
            self._log("Stopping Options Auto-Trader...")
            return True

    def _warmup(self, kc):
        """Fetch the last 2 hours of Nifty Spot candles to warm up EMA and ATR. Fallback to yfinance if Zerodha fails."""
        # Resolve Nifty Futures token
        try:
            nfo = Z.get_nfo_instruments(kc)
            nifty_futs = [i for i in nfo if i.get("name") == "NIFTY" and i.get("instrument_type") == "FUT"]
            if nifty_futs:
                nifty_futs = sorted(nifty_futs, key=lambda x: x.get("expiry"))
                self.fut_tok = int(nifty_futs[0]["instrument_token"])
                self._log(f"Warmup: Resolved Nifty Futures token {self.fut_tok} ({nifty_futs[0]['tradingsymbol']})")
        except Exception as e:
            self._log(f"Warmup: Error resolving Nifty Futures token: {e}")
            
        self._log("Fetching warmup historical Nifty index candles...")
        rows = []
        try:
            imap = Z.instrument_map(kc)
            tok = imap.get("NIFTY 50")
            if tok:
                to_d = self._ist().replace(tzinfo=None)
                ist_now = self._ist()
                market_open_today = dt.datetime.combine(ist_now.date(), dt.time(9, 15)).astimezone(ist_now.tzinfo)
                if ist_now < market_open_today:
                    from_d = (market_open_today - dt.timedelta(days=1)).replace(tzinfo=None)
                else:
                    from_d = market_open_today.replace(tzinfo=None)
                rows = kc.historical_data(tok, from_d, to_d, "minute")
        except Exception as e:
            self._log(f"Zerodha historical API failed: {e}. Trying fallback to yfinance...")

        if not rows:
            self._log("Fetching fallback warmup candles from yfinance...")
            try:
                import yfinance as yf
                yf_df = yf.download("^NSEI", period="5d", interval="1m")
                if isinstance(yf_df.columns, pd.MultiIndex):
                    yf_df.columns = yf_df.columns.get_level_values(0)
                today_str = self._ist().strftime("%Y-%m-%d")
                yf_df = yf_df[yf_df.index.strftime("%Y-%m-%d") == today_str]
                if not yf_df.empty:
                    rows = []
                    for idx, r in yf_df.iterrows():
                        rows.append({
                            "date": idx.to_pydatetime(),
                            "open": float(r["Open"]),
                            "high": float(r["High"]),
                            "low": float(r["Low"]),
                            "close": float(r["Close"]),
                        })
            except Exception as yfe:
                self._log(f"Yfinance fallback failed: {yfe}")

        if not rows:
            self._log("Warning: No warmup data returned. Indicators will build from scratch.")
            return

        self._log(f"Warmup loaded {len(rows)} index candles.")
        
        # Accumulate candles
        temp_candles = []
        for r in rows:
            ts = r["date"].astimezone(pytz.timezone("Asia/Kolkata")) if hasattr(r["date"], "tzinfo") and r["date"].tzinfo else r["date"]
            temp_candles.append({
                "open": float(r["open"]),
                "high": float(r["high"]),
                "low": float(r["low"]),
                "close": float(r["close"]),
                "date": ts
            })

        # Recalculate indicators (EMA 15, ATR 14, Volume 10 SMA)
        closes = pd.Series([c["close"] for c in temp_candles])
        ema_series = closes.ewm(span=15, adjust=False).mean().tolist()
        macro_ema_series = closes.ewm(span=75, adjust=False).mean().tolist()
        
        # Fetch futures volume for warmup if we have fut_tok
        fut_vol_map = {}
        if self.fut_tok:
            try:
                to_d = self._ist().replace(tzinfo=None)
                ist_now = self._ist()
                market_open_today = dt.datetime.combine(ist_now.date(), dt.time(9, 15)).astimezone(ist_now.tzinfo)
                if ist_now < market_open_today:
                    from_d = (market_open_today - dt.timedelta(days=1)).replace(tzinfo=None)
                else:
                    from_d = market_open_today.replace(tzinfo=None)
                fut_rows = kc.historical_data(self.fut_tok, from_d, to_d, "minute")
                for r in fut_rows:
                    r_ts = r["date"]
                    if r_ts.tzinfo is not None:
                        r_ts = r_ts.replace(tzinfo=None)
                    fut_vol_map[r_ts] = float(r["volume"])
            except Exception as fe:
                self._log(f"Warmup: Failed to fetch futures volume: {fe}")
                
        # ATR & Volume SMA Calculation
        tr_history = []
        vol_history = []
        prev_close = None
        for i, c in enumerate(temp_candles):
            high, low, close = c["high"], c["low"], c["close"]
            ts_naive = c["date"].replace(tzinfo=None) if hasattr(c["date"], "tzinfo") and c["date"].tzinfo else c["date"]
            
            # Map futures volume
            c["volume"] = fut_vol_map.get(ts_naive, 0.0)
            vol_history.append(c["volume"])
            if len(vol_history) > 10:
                vol_history.pop(0)
            c["vol_sma"] = sum(vol_history) / len(vol_history) if len(vol_history) >= 5 else 0.0
            
            if prev_close is None:
                tr = high - low
            else:
                tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
            prev_close = close
            tr_history.append(tr)
            
            if i < 13:
                c["atr"] = sum(tr_history) / len(tr_history)
            elif i == 13:
                c["atr"] = sum(tr_history) / 14.0
            else:
                prev_atr = temp_candles[i-1]["atr"]
                c["atr"] = (prev_atr * 13.0 + tr) / 14.0
                
            c["nifty_ema"] = ema_series[i]
            c["nifty_macro_ema"] = macro_ema_series[i]

        self.candles = temp_candles
        # Grab Nifty session open from today's first candle
        today_date = self._ist().date()
        today_candles = [c for c in temp_candles if c["date"].date() == today_date]
        if today_candles:
            self.nifty_open = today_candles[0]["open"]
            self._log(f"Locked Nifty Session Open for today: {self.nifty_open}")
        else:
            # Fallback if starting before market open
            self._log("Starting before session open or no today's candles. Will lock open price on first tick.")

        # Replay today's candles to reconstruct active stage counters
        today_trading_candles = [c for c in temp_candles if c["date"].date() == today_date and c["date"].strftime("%H:%M") >= "09:20"]
        
        self.l_stage = 1
        self.l_peak = None
        self.l_trough = None
        self.l_peak_atr = None
        self.s_stage = 1
        self.s_trough = None
        self.s_peak = None
        self.s_trough_atr = None
        
        if today_trading_candles:
            self._log(f"Replaying {len(today_trading_candles)} of today's candles to warm up indicators, but forcing stages to start fresh at Stage 1...")
            # We skip reconstructing stages from the past to prevent executing stale signals on startup.
            self.l_stage = 1
            self.l_peak = None
            self.l_trough = None
            self.s_stage = 1
            self.s_trough = None
            self.s_peak = None
            
            # Record the reset candle index so replay only looks at new candles formed after startup
            self.reset_replay_index = len(self.candles)
            
            self._log(f"Warmup complete. Forced Reconstructed State: Long Stage {self.l_stage}, Short Stage {self.s_stage}")

    def _reset_scanning_state(self):
        with self.lock:
            self.l_stage = 1
            self.l_peak = None
            self.l_trough = None
            self.l_peak_atr = None
            self.s_stage = 1
            self.s_trough = None
            self.s_peak = None
            self.s_trough_atr = None
            self.vol_pcr_above_ce_ticks = 0
            self.vol_pcr_below_pe_ticks = 0
            self.prev_vol_pcr = None
        self._log("TRADER STATE RESET: Re-initialized all scanning stages and tick counters.")

    def _loop(self):
        try:
            kc = Z.kite()
            self._warmup(kc)
            self.state = "scanning"
            self._log("Warm-up complete. Scanning Nifty Spot index for pullback signals.")
        except Exception as e:
            self.state = "error"
            self._log(f"Fatal Startup Error: {e}")
            self.running = False
            return

        # Track tick accumulation for live 1-minute candle
        current_minute = None
        live_open = None
        live_high = None
        live_low = None
        live_close = None

        while not self._stop:
            t0 = time.time()
            try:
                # Combine Spot and Option quotes to reduce request count
                symbols_to_quote = ["NSE:NIFTY 50"]
                with self.lock:
                    if self.active_trade and self.mode == "live" and self.active_trade.get("tradingsymbol"):
                        symbols_to_quote.append(f"NFO:{self.active_trade['tradingsymbol']}")
                    if self.vol_pcr_active_trade and self.vol_pcr_mode == "live" and self.vol_pcr_active_trade.get("tradingsymbol"):
                        symbols_to_quote.append(f"NFO:{self.vol_pcr_active_trade['tradingsymbol']}")
                
                q = kc.quote(symbols_to_quote)
                d_q = q.get("NSE:NIFTY 50")
                if not d_q:
                    time.sleep(1)
                    continue

                ltp = float(d_q.get("last_price") or 0)
                if ltp <= 0:
                    time.sleep(1)
                    continue

                # Set session open price if missing
                if getattr(self, 'nifty_open', None) is None:
                    self.nifty_open = float(d_q.get("ohlc", {}).get("open") or ltp)
                    self._log(f"First Nifty Spot Tick observed. Session Open locked at Rs. {self.nifty_open}")

                # Fetch real-time ATM Put & Call Open Interest (OI) metrics
                self._fetch_oi_metrics(kc, ltp, self.nifty_open)

                ist = self._ist()
                time_str = ist.strftime("%H:%M")
                minute_key = ist.strftime("%Y-%m-%d %H:%M")

                # Real-Time Tick Trigger Checks when in Stage 3
                is_valid_time = "09:20" <= time_str < "15:30"
                if len(self.candles) > 0 and is_valid_time and not self.active_trade:
                    last_c = self.candles[-1]
                    atr_val = last_c.get("atr", 7.0)
                    ema_val = last_c.get("nifty_ema", ltp)
                    macro_ema_val = last_c.get("nifty_macro_ema", ltp)
                    
                    is_nifty_green_today = ltp > self.nifty_open if self.nifty_open else True
                    is_nifty_red_today = ltp < self.nifty_open if self.nifty_open else True
                    is_nifty_above_ema = ltp > ema_val
                    is_nifty_below_ema = ltp < ema_val
                    is_nifty_above_macro_ema = ltp > macro_ema_val
                    is_nifty_below_macro_ema = ltp < macro_ema_val
                    
                    # LONG STATE MACHINE (Real-Time Tick)
                    if self.l_stage == 1:
                        if self.l_peak is None or ltp > self.l_peak:
                            self.l_peak = ltp
                            self.l_peak_atr = atr_val
                        else:
                            self.l_trough = ltp
                            self.l_stage = 2
                    elif self.l_stage == 2:
                        if ltp > self.l_peak:
                            self.l_peak = ltp
                            self.l_peak_atr = atr_val
                            self.l_trough = ltp
                            self.l_stage = 1
                        else:
                            self.l_trough = min(self.l_trough, ltp)
                            drop_required = config.ATR_DROP_MULT * (self.l_peak_atr if self.l_peak_atr else atr_val)
                            if self.l_trough <= self.l_peak - drop_required:
                                self.l_stage = 3
                                self._log(f"[STAGE] Long Stage 2 -> 3. Peak: {self.l_peak:.2f}, Trough: {self.l_trough:.2f} (Required Drop: {drop_required:.2f} pts)")
                    elif self.l_stage == 3:
                        if ltp < self.l_trough:
                            self.l_trough = ltp
                        
                        vol_pcr_val = self._oi_metrics.get("vol_pcr", 1.0) if getattr(self, "_oi_metrics", None) else 1.0
                        raw_10s_val = self._oi_metrics.get("raw_10s_vol_pcr", 1.0) if getattr(self, "_oi_metrics", None) else 1.0
                        
                        # Trigger CE Entry when Volume PCR reverses below 0.80 (Call buying dominates at the trough)
                        if vol_pcr_val <= 0.90:
                            long_trend_ok = is_nifty_above_macro_ema and (not config.USE_NIFTY_FILTER or is_nifty_green_today)
                            curr_t = time.time()
                            pcr_val = self._oi_metrics.get("pcr", 1.0) if getattr(self, "_oi_metrics", None) else 1.0
                            is_pcr_bullish = pcr_val >= 1.20
                            
                            if not hasattr(self, '_last_call_log_time') or curr_t - self._last_call_log_time > 10.0:
                                self._last_call_log_time = curr_t
                                self._log(f"[FILTER CHECK] Long CE Trough Reversal (Vol PCR <= 0.90) reached at Rs.{ltp:.2f}. Filters: EMA Trend (5-Min) ({'OK' if is_nifty_above_macro_ema else 'FAIL'}), Daily Trend ({'OK' if (not config.USE_NIFTY_FILTER or is_nifty_green_today) else 'FAIL'}), Volume ({'OK' if self.has_vol_conf else 'FAIL'}), PCR ({pcr_val:.2f} {'OK' if is_pcr_bullish else 'FAIL'}), Vol PCR (Raw 10s: {raw_10s_val:.2f} | 30s EMA: {vol_pcr_val:.2f} OK)")
                            if long_trend_ok and self.has_vol_conf: # and is_pcr_bullish (Commented out to prevent fill lag)
                                self._enter_position(kc, "BUY CALL (CE)", ltp, atr_val, ema_val)
                                self.l_peak = None
                                self.l_trough = None
                                self.l_stage = 1
                                
                    # SHORT STATE MACHINE (Real-Time Tick)
                    if not self.active_trade or self.state == "waiting-fill":
                        if self.s_stage == 1:
                            if self.s_trough is None or ltp < self.s_trough:
                                self.s_trough = ltp
                                self.s_trough_atr = atr_val
                            else:
                                self.s_peak = ltp
                                self.s_stage = 2
                        elif self.s_stage == 2:
                            if ltp < self.s_trough:
                                self.s_trough = ltp
                                self.s_trough_atr = atr_val
                                self.s_peak = ltp
                                self.s_stage = 1
                            else:
                                self.s_peak = max(self.s_peak, ltp)
                                rally_required = config.ATR_DROP_MULT * (self.s_trough_atr if self.s_trough_atr else atr_val)
                                if self.s_peak >= self.s_trough + rally_required:
                                    self.s_stage = 3
                                    self._log(f"[STAGE] Short Stage 2 -> 3. Trough: {self.s_trough:.2f}, Peak: {self.s_peak:.2f} (Required Rally: {rally_required:.2f} pts)")
                        elif self.s_stage == 3:
                            if ltp > self.s_peak:
                                self.s_peak = ltp
                            
                            vol_pcr_val = self._oi_metrics.get("vol_pcr", 1.0) if getattr(self, "_oi_metrics", None) else 1.0
                            raw_10s_val = self._oi_metrics.get("raw_10s_vol_pcr", 1.0) if getattr(self, "_oi_metrics", None) else 1.0
                            
                            # Trigger PE Entry when Volume PCR reverses above 1.25 (Put buying dominates at the peak)
                            if vol_pcr_val >= 1.10:
                                short_trend_ok = is_nifty_below_macro_ema and (not config.USE_NIFTY_FILTER or is_nifty_red_today)
                                curr_t = time.time()
                                pcr_val = self._oi_metrics.get("pcr", 1.0) if getattr(self, "_oi_metrics", None) else 1.0
                                is_pcr_bearish = pcr_val <= 0.80
                                
                                if not hasattr(self, '_last_put_log_time') or curr_t - self._last_put_log_time > 10.0:
                                    self._last_put_log_time = curr_t
                                    self._log(f"[FILTER CHECK] Short PE Peak Reversal (Vol PCR >= 1.10) reached at Rs.{ltp:.2f}. Filters: EMA Trend (5-Min) ({'OK' if is_nifty_below_macro_ema else 'FAIL'}), Daily Trend ({'OK' if (not config.USE_NIFTY_FILTER or is_nifty_red_today) else 'FAIL'}), Volume ({'OK' if self.has_vol_conf else 'FAIL'}), PCR ({pcr_val:.2f} {'OK' if is_pcr_bearish else 'FAIL'}), Vol PCR (Raw 10s: {raw_10s_val:.2f} | 30s EMA: {vol_pcr_val:.2f} OK)")
                                if short_trend_ok and self.has_vol_conf: # and is_pcr_bearish (Commented out to prevent fill lag)
                                    self._enter_position(kc, "BUY PUT (PE)", ltp, atr_val, ema_val)
                                    self.s_trough = None
                                    self.s_peak = None
                                    self.s_stage = 1
                    
                    # --- VOLUME PCR-ONLY STRATEGY SCANNER ---
                    # Strategy disabled to prevent state reset interference with Main Strategy.
                    vol_pcr_val = self._oi_metrics.get("vol_pcr", 1.0) if getattr(self, "_oi_metrics", None) else 1.0
                    self.prev_vol_pcr = vol_pcr_val

                # Accumulate 1-minute candle
                if current_minute != minute_key:
                    # Minute boundary crossed! Finalize previous candle
                    if current_minute is not None and live_close is not None:
                        new_candle = {
                            "open": live_open,
                            "high": live_high,
                            "low": live_low,
                            "close": live_close,
                            "date": ist - dt.timedelta(minutes=1)
                        }
                        
                        # Recalculate indicators
                        self.candles.append(new_candle)
                        if len(self.candles) > 300:
                            self.candles.pop(0)

                        closes = pd.Series([c["close"] for c in self.candles])
                        ema_val = float(closes.ewm(span=15, adjust=False).mean().iloc[-1])
                        macro_ema_val = float(closes.ewm(span=75, adjust=False).mean().iloc[-1])
                        
                        # Get latest Nifty Futures volume for the completed candle
                        fut_vol = 0.0
                        if self.fut_tok:
                            try:
                                to_d = dt.datetime.now()
                                from_d = to_d - dt.timedelta(minutes=3)
                                rows = kc.historical_data(self.fut_tok, from_d, to_d, "minute")
                                if rows:
                                    completed_ts = new_candle["date"].replace(tzinfo=None)
                                    for r in reversed(rows):
                                        r_dt = r["date"]
                                        if r_dt.tzinfo is not None:
                                            r_dt = r_dt.replace(tzinfo=None)
                                        if abs((r_dt - completed_ts).total_seconds()) < 30:
                                            fut_vol = float(r["volume"])
                                            break
                            except Exception as e:
                                self._log(f"Error fetching live Nifty Futures volume: {e}")
                        
                        new_candle["volume"] = fut_vol
                        
                        # Calculate volume SMA
                        vol_history = [c.get("volume", 0.0) for c in self.candles[-10:]]
                        vol_sma = sum(vol_history) / len(vol_history) if len(vol_history) >= 5 else 0.0
                        new_candle["vol_sma"] = vol_sma
                        
                        # Update live has_vol_conf state
                        if fut_vol > 0.0 and vol_sma > 0.0:
                            self.has_vol_conf = (fut_vol > vol_sma)
                        else:
                            self.has_vol_conf = True  # Safe fallback if API fails

                        # Calculate ATR using Wilder's 14-period smoothing (RMA)
                        if self.candles:
                            prev_atr = self.candles[-1].get("atr")
                            prev_close = self.candles[-1].get("close")
                            new_tr = max(live_high - live_low, abs(live_high - prev_close), abs(live_low - prev_close))
                            if prev_atr is not None:
                                atr_val = (prev_atr * 13.0 + new_tr) / 14.0
                            else:
                                atr_val = new_tr
                        else:
                            atr_val = live_high - live_low
                        
                        new_candle["atr"] = atr_val
                        new_candle["nifty_ema"] = ema_val
                        new_candle["nifty_macro_ema"] = macro_ema_val



                    # Initialize next live 1-minute candle
                    current_minute = minute_key
                    live_open = ltp
                    live_high = ltp
                    live_low = ltp
                    live_close = ltp
                else:
                    # Accumulate ticks
                    live_high = max(live_high, ltp)
                    live_low = min(live_low, ltp)
                    live_close = ltp

                # If in position or waiting for fill, manage at 1-second resolution
                if self.active_trade:
                    if self.state == "waiting-fill":
                        self._manage_pending_order(kc, ltp, quote_data=q)
                    else:
                        self._manage_active_position(kc, ltp, quote_data=q)

                if self.vol_pcr_active_trade:
                    if not self.vol_pcr_active_trade.get("filled", True):
                        self._manage_vol_pcr_pending_order(kc, ltp, quote_data=q)
                    else:
                        self._manage_vol_pcr_position(kc, ltp, quote_data=q)

            except Exception as e:
                self._log(f"Loop error: {e}")
            
            # Poll every second
            time.sleep(max(0.1, 1.0 - (time.time() - t0)))

    def _enter_position(self, kc, side, entry_spot, atr, ema):
        strike = int(round(entry_spot / 50.0) * 50.0)
        opt_type = "CE" if "CALL" in side else "PE"
        today_date = self._ist().date()
        
        # Calculate dynamic Nifty expiry date from instruments list
        expiry_date = Z.get_expiry_date(kc, today_date)
        
        # Next-Week Expiry Roll-Over: If today is the actual expiry day and it is after 12:30 PM, roll over to the next expiry
        if today_date == expiry_date:
            now_ist = self._ist()
            if now_ist.hour > 12 or (now_ist.hour == 12 and now_ist.minute >= 30):
                insts = Z.get_nfo_instruments(kc)
                next_exp = None
                if insts:
                    try:
                        expiries = sorted(list({
                            dt.datetime.strptime(i["expiry"], "%Y-%m-%d").date()
                            for i in insts
                            if i.get("name") == "NIFTY" and i.get("expiry")
                        }))
                        future_exp = [e for e in expiries if e > today_date]
                        if future_exp:
                            next_exp = future_exp[0]
                    except Exception:
                        pass
                expiry_date = next_exp or (today_date + dt.timedelta(days=7))
                self._log(f"Expiry Day Afternoon Roll-over active. Selected next week's contract expiry: {expiry_date.strftime('%Y-%m-%d')}")
                
        expiry_str = expiry_date.strftime("%d %b").upper()
        symbol_str = f"NIFTY {expiry_str} {strike} {opt_type}"

        # Fetch actual current Spot price to calculate target and SL
        current_spot = entry_spot
        if self.mode == "live":
            try:
                q = kc.quote(["NSE:NIFTY 50"])
                d_q = q.get("NSE:NIFTY 50")
                if d_q and d_q.get("last_price"):
                    current_spot = float(d_q["last_price"])
            except Exception:
                pass

        # Check gap between actual current Spot price and theoretical trigger (entry_spot)
        gap = abs(current_spot - entry_spot)
        max_gap = getattr(config, "max_entry_gap_points", 3.0)
        if gap > max_gap:
            self._log(f"Skipping entry: Actual Spot ₹{current_spot:.2f} is too far from Signal Spot ₹{entry_spot:.2f} (Gap: {gap:.2f} pts > Max Allowed: {max_gap:.2f} pts)")
            return

        # Calculate target & stop-loss levels on spot index
        # Standard: Target = 2.0 * ATR (min 14.0 points), SL = 1.0 * ATR (min 7.0 points)
        sl_points = max(2.0 * atr, 14.0)
        target_points = max(2.0 * atr, 14.0)

        if opt_type == "CE":
            spot_sl = entry_spot - sl_points
            spot_target = entry_spot + target_points
        else:
            spot_sl = entry_spot + sl_points
            spot_target = entry_spot - target_points

        # Calculate theoretical target based on theoretical trigger level
        theoretical_target = (entry_spot + target_points) if opt_type == "CE" else (entry_spot - target_points)

        # Skip entry if the Spot price has already crossed the theoretical target (overshot)
        if opt_type == "CE" and current_spot >= theoretical_target:
            self._log(f"Skipping CE entry: Actual Spot ₹{current_spot:.2f} has already crossed theoretical target ₹{theoretical_target:.2f}")
            return
        if opt_type == "PE" and current_spot <= theoretical_target:
            self._log(f"Skipping PE entry: Actual Spot ₹{current_spot:.2f} has already crossed theoretical target ₹{theoretical_target:.2f}")
            return

        self.state = "waiting-fill" if self.mode == "live" else "in-trade"

        # Tuesday premium decay decay estimation
        days_to_expiry = (expiry_date - today_date).days
        decay_factor = 0.005 if days_to_expiry <= 1 else 0.007 if days_to_expiry == 2 else 0.010 if days_to_expiry == 3 else 0.013 if days_to_expiry <= 5 else 0.016
        fallback_premium = entry_spot * decay_factor

        entry_premium = fallback_premium
        tradingsymbol = ""
        lot_size = 75
        
        if self.lot_size_mode == "fixed":
            lots = self.fixed_lots
        else:
            lot_cost = entry_premium * lot_size
            lots = math.floor(self.capital / lot_cost) if lot_cost > 0 else 0
            if lots == 0:
                lots = 1
        
        if self.mode == "live":
            # Cancel old pending order if a new one is being placed
            if self.active_trade and self.active_trade.get("order_id"):
                old_oid = self.active_trade["order_id"]
                self._log(f"[PENDING] New signal fired. Cancelling old pending order {old_oid} to replace with new level...")
                try:
                    kc.cancel_order(variety=kc.VARIETY_REGULAR, order_id=old_oid)
                except Exception as ce:
                    self._log(f"[PENDING] Old order cancel failed or already filled: {ce}")
                self.active_trade = None
            try:
                opt_token = Z.get_option_token(kc, "NIFTY", expiry_date, strike, opt_type)
                if not opt_token:
                    raise ValueError(f"No active option token found for Nifty {expiry_str} {strike} {opt_type}")
                
                # Fetch actual contract trading symbol and lot size from instruments cache
                insts = Z.get_nfo_instruments(kc)
                matched = next((i for i in insts if int(i.get("instrument_token") or 0) == opt_token), None)
                if matched:
                    tradingsymbol = matched["tradingsymbol"]
                    lot_size = int(matched.get("lot_size") or 75)
                
                # Fetch current option LTP
                quote = kc.quote([f"NFO:{tradingsymbol}"])
                opt_q = quote.get(f"NFO:{tradingsymbol}")
                if opt_q:
                    entry_premium = float(opt_q.get("last_price") or entry_premium)

                if self.lot_size_mode == "fixed":
                    lots = self.fixed_lots
                else:
                    lot_cost = entry_premium * lot_size
                    lots = math.floor(self.capital / lot_cost) if lot_cost > 0 else 0
                
                required_capital = lots * entry_premium * lot_size
                if self.capital < required_capital:
                    raise ValueError(f"Insufficient capital. Required: Rs. {required_capital}, Available: Rs. {self.capital}")
                if lots == 0:
                    raise ValueError(f"Trade lot size evaluated to 0. Mode: {self.lot_size_mode}")

                # Calculate ideal premium limit price (applying a fixed 2.0 point discount on option premium to protect against minor chop)
                buy_depth = opt_q.get("depth", {}).get("buy", []) if opt_q else []
                current_bid = float(buy_depth[0]["price"]) if buy_depth else entry_premium
                
                limit_price = max(1.0, round(current_bid, 1))
                
                self._log(f"[LIVE] Placing PASSIVE LIMIT BUY order at Rs. {limit_price} for {lots} lots of {tradingsymbol}...")
                self._log(f"[LIVE] (Signal Spot: Rs.{entry_spot:.2f}, Current Spot: Rs.{current_spot:.2f}, Original Bid: Rs. {current_bid:.2f})")
                oid = kc.place_order(
                    variety          = kc.VARIETY_REGULAR,
                    exchange         = kc.EXCHANGE_NFO,
                    tradingsymbol    = tradingsymbol,
                    transaction_type = kc.TRANSACTION_TYPE_BUY,
                    quantity         = lots * lot_size,
                    product          = kc.PRODUCT_MIS,
                    order_type       = kc.ORDER_TYPE_LIMIT,
                    price            = limit_price,
                )
                self._log(f"[LIVE] Limit order placed successfully. ID: {oid}. Waiting for fill (no timeout)...")
                entry_vol_pcr = self._oi_metrics.get("vol_pcr", 1.0) if getattr(self, "_oi_metrics", None) else 1.0
                with self.lock:
                    self.active_trade = {
                        "side": side,
                        "symbol": symbol_str,
                        "tradingsymbol": tradingsymbol,
                        "entry_time": self._ist().strftime("%H:%M:%S"),
                        "entry_spot": entry_spot,
                        "spot_sl": spot_sl,
                        "spot_target": spot_target,
                        "current_sl": spot_sl,
                        "reached_halfway": False,
                        "entry_premium": limit_price,
                        "current_premium": limit_price,
                        "lots": lots,
                        "lot_size": lot_size,
                        "started_at": time.time(),
                        "order_id": oid,
                        "filled": False,
                        "min_vol_pcr": entry_vol_pcr,
                        "max_vol_pcr": entry_vol_pcr
                    }
                self.state = "waiting-fill"
                return

            except Exception as e:
                self.state = "error"
                self._log(f"[LIVE ERROR] Order Entry Failed: {e}")
                self.active_trade = None
                return
        else:
            # Paper Trading Fills
            lot_cost = entry_premium * lot_size
            lots = math.floor(self.capital / lot_cost) if lot_cost > 0 else 0
            self._log(f"[PAPER] Simulating Buy {lots} lots CE/PE @ Rs. {entry_premium:.2f} premium")

        with self.lock:
            self.active_trade = {
                "side": side,
                "symbol": symbol_str,
                "tradingsymbol": tradingsymbol,
                "entry_time": self._ist().strftime("%H:%M:%S"),
                "entry_spot": current_spot,
                "spot_sl": spot_sl,
                "spot_target": spot_target,
                "current_sl": spot_sl,  # Will trail to entry at halfway
                "reached_halfway": False,
                "entry_premium": entry_premium,
                "current_premium": entry_premium,
                "lots": lots,
                "lot_size": lot_size,
                "started_at": time.time()
            }

    def _fetch_oi_metrics(self, kc, spot_ltp, nifty_open=None):
        """Fetch ATM Put & Call Open Interest from Zerodha to check institutional dip buying."""
        now = time.time()
        # Allow 4-second polling intervals
        if hasattr(self, '_last_oi_fetch') and now - getattr(self, '_last_oi_fetch', 0) < 4.0:
            return getattr(self, '_oi_metrics', {"pcr": 1.0, "pe_oi": 0, "ce_oi": 0, "oi_trend": "NEUTRAL"})
        
        self._last_oi_fetch = now
        try:
            if nifty_open is None:
                try:
                    q_nifty = kc.quote(["NSE:NIFTY 50"]).get("NSE:NIFTY 50", {})
                    nifty_open = float(q_nifty.get("ohlc", {}).get("open") or spot_ltp)
                except:
                    nifty_open = spot_ltp
                    
            today_date = self._ist().date()
            expiry_date = Z.get_expiry_date(kc, today_date)
            
            strike_atm = int(round(spot_ltp / 50.0) * 50.0)
            strike_minus = strike_atm - 50
            strike_plus = strike_atm + 50
            
            floor_strike = int(spot_ltp // 50.0) * 50.0
            ceil_strike = floor_strike + 50.0
            
            # Resolve tokens for ATM, ATM-50, ATM+50
            atm_pe_tok = Z.get_option_token(kc, "NIFTY", expiry_date, strike_atm, "PE")
            atm_ce_tok = Z.get_option_token(kc, "NIFTY", expiry_date, strike_atm, "CE")
            minus_pe_tok = Z.get_option_token(kc, "NIFTY", expiry_date, strike_minus, "PE")
            minus_ce_tok = Z.get_option_token(kc, "NIFTY", expiry_date, strike_minus, "CE")
            plus_pe_tok = Z.get_option_token(kc, "NIFTY", expiry_date, strike_plus, "PE")
            plus_ce_tok = Z.get_option_token(kc, "NIFTY", expiry_date, strike_plus, "CE")
            
            insts = Z.get_nfo_instruments(kc)
            tokens = {}
            for t_val, name_val in [
                (atm_pe_tok, "atm_pe"), (atm_ce_tok, "atm_ce"),
                (minus_pe_tok, "minus_pe"), (minus_ce_tok, "minus_ce"),
                (plus_pe_tok, "plus_pe"), (plus_ce_tok, "plus_ce")
            ]:
                if t_val:
                    sym_val = next((i["tradingsymbol"] for i in insts if int(i.get("instrument_token") or 0) == t_val), None)
                    if sym_val:
                        tokens[name_val] = sym_val

            symbols_list = [f"NFO:{s}" for s in tokens.values()]
            quotes = kc.quote(symbols_list) if symbols_list else {}

            def parse_opt(name):
                q = quotes.get(f"NFO:{tokens.get(name)}", {})
                return {
                    "oi": int(q.get("oi") or 0),
                    "ltp": float(q.get("last_price") or 0),
                    "volume": int(q.get("volume") or 0)
                }

            atm_pe = parse_opt("atm_pe")
            atm_ce = parse_opt("atm_ce")
            minus_pe = parse_opt("minus_pe")
            minus_ce = parse_opt("minus_ce")
            plus_pe = parse_opt("plus_pe")
            plus_ce = parse_opt("plus_ce")

            total_put_vol = atm_pe.get("volume", 0) + minus_pe.get("volume", 0) + plus_pe.get("volume", 0)
            total_call_vol = atm_ce.get("volume", 0) + minus_ce.get("volume", 0) + plus_ce.get("volume", 0)

            # 10-Second Volume Delta Tracking
            if not hasattr(self, '_raw_vol_snapshots'):
                self._raw_vol_snapshots = []
            self._raw_vol_snapshots.append({
                "time": now,
                "put_vol": total_put_vol,
                "call_vol": total_call_vol
            })
            self._raw_vol_snapshots = [s for s in self._raw_vol_snapshots if now - s["time"] <= 30]

            # Find snapshot closest to 10 seconds ago
            target_time = now - 10.0
            past_snap = None
            if len(self._raw_vol_snapshots) > 1:
                past_snap = min(self._raw_vol_snapshots[:-1], key=lambda s: abs(s["time"] - target_time))

            if past_snap and abs(past_snap["time"] - target_time) <= 6.0:
                delta_put = total_put_vol - past_snap["put_vol"]
                delta_call = total_call_vol - past_snap["call_vol"]
            else:
                delta_put = total_put_vol
                delta_call = total_call_vol

            # Raw 10s Volume PCR
            raw_10s_vol_pcr = round(delta_put / delta_call, 2) if delta_call > 0 else 1.0

            # 1-Minute Rolling EMA of 10s Delta Volume PCR
            if not hasattr(self, '_vol_pcr_10s_history'):
                self._vol_pcr_10s_history = []
            self._vol_pcr_10s_history.append((now, raw_10s_vol_pcr))
            self._vol_pcr_10s_history = [(t_h, v_h) for t_h, v_h in self._vol_pcr_10s_history if now - t_h <= 60]

            if len(self._vol_pcr_10s_history) > 1:
                sorted_hist = sorted(self._vol_pcr_10s_history, key=lambda x: x[0])
                values = [x[1] for x in sorted_hist]
                N_periods = 15.0  # 60s / 4s = 15 periods
                alpha_val = 2.0 / (N_periods + 1.0)
                vol_pcr_ema = values[0]
                for val in values[1:]:
                    vol_pcr_ema = (val * alpha_val) + (vol_pcr_ema * (1.0 - alpha_val))
                vol_pcr_ema = round(vol_pcr_ema, 2)
            else:
                vol_pcr_ema = raw_10s_vol_pcr

            vol_pcr = vol_pcr_ema

            pcr_atm = round(atm_pe["oi"] / atm_ce["oi"], 2) if atm_ce["oi"] > 0 else 1.0
            pcr_minus = round(minus_pe["oi"] / minus_ce["oi"], 2) if minus_ce["oi"] > 0 else 1.0
            pcr_plus = round(plus_pe["oi"] / plus_ce["oi"], 2) if plus_ce["oi"] > 0 else 1.0

            # Map floor/ceil PCRs for the dual-strike support checks
            pcr_floor = pcr_atm if floor_strike == strike_atm else (pcr_minus if floor_strike == strike_minus else pcr_plus)
            pcr_ceil = pcr_atm if ceil_strike == strike_atm else (pcr_plus if ceil_strike == strike_plus else pcr_minus)

            pcr = pcr_atm
            pe_oi = atm_pe["oi"]
            ce_oi = atm_ce["oi"]
            pe_ltp = atm_pe["ltp"]
            ce_ltp = atm_ce["ltp"]
            pe_sym = tokens.get("atm_pe")
            ce_sym = tokens.get("atm_ce")

            oi_trend = "INSTITUTIONAL PE BUYING (SUPPORT)" if pcr >= 1.2 else "CE CALL WRITING (RESISTANCE)" if pcr <= 0.8 else "NEUTRAL OI"
            
            # PCR Trajectory Tracking (20-minute rolling window)
            if not hasattr(self, '_pcr_history'):
                self._pcr_history = []
            self._pcr_history.append((now, pcr))
            self._pcr_history = [(t, p) for t, p in self._pcr_history if now - t <= 1200]
            
            pcr_5m_ago = next((p for t, p in reversed(self._pcr_history) if 240 <= (now - t) <= 360), pcr)
            pcr_change_5m = round(pcr - pcr_5m_ago, 2)
            
            pcr_10m_ago = next((p for t, p in reversed(self._pcr_history) if 540 <= (now - t) <= 660), pcr)
            pcr_change_10m = round(pcr - pcr_10m_ago, 2)
            
            # Format combined direction string
            dir_5m = f"RISING (+{pcr_change_5m:.2f})" if pcr_change_5m >= 0.04 else (f"FALLING ({pcr_change_5m:.2f})" if pcr_change_5m <= -0.04 else "STABLE")
            dir_10m = f"RISING (+{pcr_change_10m:.2f})" if pcr_change_10m >= 0.08 else (f"FALLING ({pcr_change_10m:.2f})" if pcr_change_10m <= -0.08 else "STABLE")
            pcr_direction = f"5m: {dir_5m} | 10m: {dir_10m}"

            # Check if there is strong Put support
            best_support_strike = floor_strike if pcr_floor >= 1.05 else (ceil_strike if pcr_ceil >= 1.05 else (strike_atm if pcr_atm >= 1.05 else None))

            # 10:00 AM Pivot Strategy Check
            ist_now = self._ist()
            is_past_10am = ist_now.hour >= 10
            nifty_open = nifty_open or spot_ltp
            is_price_green = spot_ltp >= nifty_open
            price_diff = spot_ltp - nifty_open

            rec = {}
            if is_past_10am:
                # 10:00 AM Pivot Strategy: Trend + PCR Sniper Confirmation (0.5 and 1.5)
                if is_price_green and pcr >= 1.50:
                    sell_s = int(strike_atm - 100)
                    buy_s = sell_s - 100
                    rec = {
                        "action": f"SELL {sell_s} PE / BUY {buy_s} PE (10 AM BULL)",
                        "type": "BULL_PUT_SPREAD",
                        "sell_strike": sell_s,
                        "buy_strike": buy_s,
                        "cushion_pts": int(round(spot_ltp - sell_s)),
                        "reason": f"🔥 10:00 AM Pivot CONFIRMED: Price is GREEN relative to Open (+{price_diff:.1f} pts) & PCR is BULLISH (>1.50: {pcr:.2f}). Sniper Mode Bull Setup."
                    }
                elif not is_price_green and pcr <= 0.50:
                    sell_s = int(strike_atm + 50)
                    buy_s = sell_s + 100
                    rec = {
                        "action": f"SELL {sell_s} CE / BUY {buy_s} CE (10 AM BEAR)",
                        "type": "BEAR_CALL_SPREAD",
                        "sell_strike": sell_s,
                        "buy_strike": buy_s,
                        "cushion_pts": int(round(sell_s - spot_ltp)),
                        "reason": f"🔥 10:00 AM Pivot CONFIRMED: Price is RED relative to Open ({price_diff:.1f} pts) & PCR is BEARISH (<0.50: {pcr:.2f}). Sniper Mode Bear Setup."
                    }
                elif is_price_green and pcr < 0.50:
                    rec = {
                        "action": "STAND ASIDE / CONFLICT",
                        "type": "WAIT_NEUTRAL",
                        "reason": f"⚠️ 10:00 AM Conflict: Price is GREEN relative to Open (+{price_diff:.1f} pts), but PCR is BEARISH (<0.50: {pcr:.2f}). Stand aside."
                    }
                elif not is_price_green and pcr > 1.50:
                    rec = {
                        "action": "STAND ASIDE / CONFLICT",
                        "type": "WAIT_NEUTRAL",
                        "reason": f"⚠️ 10:00 AM Conflict: Price is RED relative to Open ({price_diff:.1f} pts), but PCR is BULLISH (>1.50: {pcr:.2f}). Stand aside."
                    }
                else:
                    rec = {
                        "action": "WAIT / NEUTRAL",
                        "type": "WAIT_NEUTRAL",
                        "reason": f"10:00 AM Pivot Neutral: PCR {pcr:.2f} & Price relative to Open ({price_diff:+.1f} pts) are in a sideways zone (Target PCR: >1.5 or <0.5)."
                    }
            else:
                # Before 10:00 AM: Standard Real-Time Momentum Logic
                if pcr <= 0.75 and pcr_change_5m <= -0.04:
                    rec = {
                        "action": "BUY PE (PUT)",
                        "type": "BUY_PE",
                        "symbol": pe_sym,
                        "strike": strike_atm,
                        "opt_ltp": pe_ltp,
                        "target_opt": round(pe_ltp + 15.0, 2),
                        "sl_opt": round(max(1.0, pe_ltp - 10.0), 2),
                        "reason": f"Call writers in control & PCR is FALLING ({pcr_direction}). PE momentum."
                    }
                elif pcr >= 1.0 and pcr_change_5m >= 0.04:
                    rec = {
                        "action": "BUY CE (CALL)",
                        "type": "BUY_CE",
                        "symbol": ce_sym,
                        "strike": strike_atm,
                        "opt_ltp": ce_ltp,
                        "target_opt": round(ce_ltp + 15.0, 2),
                        "sl_opt": round(max(1.0, ce_ltp - 10.0), 2),
                        "reason": f"Put writers building support & PCR is RISING ({pcr_direction}). CE momentum."
                    }
                elif best_support_strike:
                    sell_s = int(best_support_strike - 100)
                    buy_s = sell_s - 100
                    support_pcr = pcr_floor if best_support_strike == floor_strike else pcr_ceil
                    rec = {
                        "action": f"SELL {sell_s} PE / BUY {buy_s} PE",
                        "type": "BULL_PUT_SPREAD",
                        "sell_strike": sell_s,
                        "buy_strike": buy_s,
                        "cushion_pts": int(round(spot_ltp - sell_s)),
                        "reason": f"Put Support Floor at {int(best_support_strike)} (PCR {support_pcr}). Ultra-Safe {int(round(spot_ltp - sell_s))}-pt Cushion Entry."
                    }
                else:
                    if pcr < 0.9 and pcr_change_5m > 0:
                        rec = {
                            "action": "WAIT / DO NOT BUY PE",
                            "type": "WAIT_RISING_PCR",
                            "reason": f"PCR is RISING ({pcr_direction}) from low level ({pcr}). Put writers building floor — DO NOT BUY PE! Wait for PCR >= 1.0 to Buy CE."
                        }
                    else:
                        rec = {
                            "action": "WAIT / NEUTRAL",
                            "type": "WAIT_NEUTRAL",
                            "reason": f"PCR {pcr} is in neutral range ({pcr_direction}). Awaiting clear institutional trend."
                        }

            self._oi_metrics = {
                "pcr": pcr,
                "vol_pcr": vol_pcr,
                "vol_pcr_ema": vol_pcr_ema,
                "raw_10s_vol_pcr": raw_10s_vol_pcr,
                "total_put_vol": total_put_vol,
                "total_call_vol": total_call_vol,
                "pe_oi": pe_oi,
                "ce_oi": ce_oi,
                "strike": strike_atm,
                "spot": round(spot_ltp, 2),
                "oi_trend": oi_trend,
                "pcr_direction": pcr_direction,
                "pcr_change_5m": pcr_change_5m,
                "pcr_change_10m": pcr_change_10m,
                "ce_sym": ce_sym,
                "pe_sym": pe_sym,
                "ce_ltp": ce_ltp,
                "pe_ltp": pe_ltp,
                "trade_recommendation": rec,
                
                # Multi-strike metrics for charting
                "strike_minus": strike_minus,
                "strike_plus": strike_plus,
                "pcr_minus": pcr_minus,
                "pcr_plus": pcr_plus,
                "pe_oi_minus": minus_pe["oi"],
                "ce_oi_minus": minus_ce["oi"],
                "pe_oi_plus": plus_pe["oi"],
                "ce_oi_plus": plus_ce["oi"]
            }
            return self._oi_metrics
        except Exception as e:
            log.warning(f"Failed to fetch OI metrics: {e}")
            return getattr(self, '_oi_metrics', {"pcr": 1.0, "vol_pcr": 1.0, "vol_pcr_ema": 1.0, "raw_10s_vol_pcr": 1.0, "pe_oi": 0, "ce_oi": 0, "oi_trend": "NEUTRAL"})

    def get_live_oi_metrics(self):
        """Fetch live OI metrics from Zerodha even when autotrader thread is not running."""
        try:
            kc = Z.kite()
            q = kc.quote(["NSE:NIFTY 50"])
            d_q = q.get("NSE:NIFTY 50")
            if d_q and d_q.get("last_price"):
                spot = float(d_q["last_price"])
                nifty_open = float(d_q.get("ohlc", {}).get("open") or spot)
                return self._fetch_oi_metrics(kc, spot, nifty_open)
        except Exception as e:
            log.warning(f"Error fetching live OI: {e}")
        return getattr(self, "_oi_metrics", {"pcr": 1.0, "pe_oi": 0, "ce_oi": 0, "oi_trend": "NEUTRAL"})

    def _manage_pending_order(self, kc, spot_ltp, quote_data=None):
        t = self.active_trade
        if not t:
            return

        is_call = "CALL" in t["side"]
        sl = t["spot_sl"]
        target = t["spot_target"]

        cancel_needed = False
        reason = ""

        # Cancel if pending for more than 60 seconds (prevent stale fills)
        elapsed_sec = time.time() - t["started_at"]
        if elapsed_sec >= 120.0:
            cancel_needed = True
            reason = f"Order pending timeout (120s elapsed)"

        elif is_call:
            if spot_ltp <= sl:
                cancel_needed = True
                reason = f"Spot price hit SL ₹{sl:.2f} before fill"
            elif spot_ltp >= target:
                cancel_needed = True
                reason = f"Spot price hit Target ₹{target:.2f} before fill"
        else:
            if spot_ltp >= sl:
                cancel_needed = True
                reason = f"Spot price hit SL ₹{sl:.2f} before fill"
            elif spot_ltp <= target:
                cancel_needed = True
                reason = f"Spot price hit Target ₹{target:.2f} before fill"

        if cancel_needed:
            self._log(f"[PENDING] {reason}. Cancelling pending order {t['order_id']}...")
            try:
                if self.mode == "live":
                    kc.cancel_order(variety=kc.VARIETY_REGULAR, order_id=t["order_id"])
            except Exception as ce:
                self._log(f"[PENDING] Cancel failed: {ce}")
            self.active_trade = None
            self.state = "scanning"
            return

        # Poll order status from exchange once every 2 seconds
        now = time.time()
        if not hasattr(self, "_last_order_poll"):
            self._last_order_poll = 0

        if now - self._last_order_poll >= 2.0:
            self._last_order_poll = now
            try:
                if self.mode == "live":
                    orders = kc.orders()
                    o = next((x for x in orders if str(x["order_id"]) == str(t["order_id"])), None)
                    if o:
                        status = o["status"]
                        if status == "COMPLETE":
                            t["entry_premium"] = float(o["average_price"])
                            t["current_premium"] = t["entry_premium"]
                            t["started_at"] = time.time()
                            t["filled"] = True
                            self.state = "in-trade"
                            self._log(f"[LIVE] Pending order FILLED at Rs. {t['entry_premium']:.2f}")
                        elif status in ["CANCELLED", "REJECTED"]:
                            self._log(f"[LIVE] Pending order was {status}. Reason: {o.get('status_message', 'none')}")
                            self.active_trade = None
                            self.state = "scanning"
            except Exception as e:
                self._log(f"[PENDING] Error checking order status: {e}")

    def _manage_active_position(self, kc, spot_ltp, quote_data=None):
        t = self.active_trade
        if not t:
            return

        side = t["side"]
        entry_spot = t["entry_spot"]
        target = t["spot_target"]
        sl = t["spot_sl"]
        current_sl = t["current_sl"]
        reached_halfway = t["reached_halfway"]
        is_call = "CALL" in side

        # Update option LTP for floating P&L display
        if self.mode == "live" and t["tradingsymbol"]:
            try:
                opt_key = f"NFO:{t['tradingsymbol']}"
                if quote_data and opt_key in quote_data:
                    opt_q = quote_data[opt_key]
                else:
                    quote = kc.quote([opt_key])
                    opt_q = quote.get(opt_key)
                
                if opt_q:
                    t["current_premium"] = float(opt_q.get("last_price") or t["current_premium"])
            except Exception:
                pass
        else:
            # Paper P&L estimation: change in spot price * delta
            spot_change = (spot_ltp - entry_spot) if is_call else (entry_spot - spot_ltp)
            t["current_premium"] = max(1.0, t["entry_premium"] + (spot_change * 0.50))

        # Check premium-based stop-loss safety floor (max 50% loss on option premium)
        if t["current_premium"] <= 0.50 * t["entry_premium"]:
            self._log(f"Premium Max Loss Trigger: Option premium lost 50% of its value (Current: Rs. {t['current_premium']:.2f}, Entry: Rs. {t['entry_premium']:.2f}). Exiting trade.")
            self._exit_position(kc, "LOSS", spot_ltp)
            return

        # Check time-decay timeout (45 minutes max hold)
        elapsed_mins = (time.time() - t["started_at"]) / 60.0
        if elapsed_mins >= 45.0:
            self._log(f"Time-Decay Trigger: Duration exceeded 45 minutes ({elapsed_mins:.1f}m). Exiting trade.")
            self._exit_position(kc, "TIMEOUT", spot_ltp)
            return

        # Update Volume PCR trailing extremes and run Trailing/Hard exits
        vol_pcr_val = self._oi_metrics.get("vol_pcr", 1.0) if getattr(self, "_oi_metrics", None) else 1.0
        if is_call:
            t["min_vol_pcr"] = min(t.get("min_vol_pcr", vol_pcr_val), vol_pcr_val)
            limit_pcr = t["min_vol_pcr"] + 0.20
            if vol_pcr_val >= limit_pcr or vol_pcr_val >= 1.10:
                self._log(f"Volume PCR Trailing/Hard Exit: Vol PCR EMA hit {vol_pcr_val:.2f} (Lowest PCR: {t['min_vol_pcr']:.2f} + 0.20 offset = {limit_pcr:.2f} | Hard Barrier: 1.10). Exiting trade early.")
                self._exit_position(kc, "LOSS", spot_ltp)
                return
        else:
            t["max_vol_pcr"] = max(t.get("max_vol_pcr", vol_pcr_val), vol_pcr_val)
            limit_pcr = t["max_vol_pcr"] - 0.20
            if vol_pcr_val <= limit_pcr or vol_pcr_val <= 0.90:
                self._log(f"Volume PCR Trailing/Hard Exit: Vol PCR EMA hit {vol_pcr_val:.2f} (Highest PCR: {t['max_vol_pcr']:.2f} - 0.20 offset = {limit_pcr:.2f} | Hard Barrier: 0.90). Exiting trade early.")
                self._exit_position(kc, "LOSS", spot_ltp)
                return

        # Check trailing breakeven condition (commented out)
        # if not reached_halfway:
        #     if is_call:
        #         trail_level = entry_spot + 0.5 * (target - entry_spot)
        #         if spot_ltp >= trail_level:
        #             t["reached_halfway"] = True
        #             t["current_sl"] = entry_spot
        #             self._log(f"Trail Trigger: Spot hit 50% progress (1 ATR) Rs. {spot_ltp:.2f}. Trailing Stop-Loss to entry Rs. {entry_spot:.2f}")
        #     else:
        #         trail_level = entry_spot - 0.5 * (entry_spot - target)
        #         if spot_ltp <= trail_level:
        #             t["reached_halfway"] = True
        #             t["current_sl"] = entry_spot
        #             self._log(f"Trail Trigger: Spot hit 50% progress (1 ATR) Rs. {spot_ltp:.2f}. Trailing Stop-Loss to entry Rs. {entry_spot:.2f}")

        # Check premium profit target of 3.0 points (Everywhere)
        if t["current_premium"] - t["entry_premium"] >= 3.0:
            self._log(f"Premium Target Reached (+3.0pts): Entry: Rs. {t['entry_premium']:.2f}, Current: Rs. {t['current_premium']:.2f}. Booking profits.")
            self._exit_position(kc, "WIN", spot_ltp)
            return

        # Check exit triggers
        exit_triggered = False
        verdict = "OPEN"
        exit_spot = spot_ltp

        if is_call:
            if spot_ltp <= sl:
                exit_triggered = True
                verdict = "LOSS"
                exit_spot = sl
            elif spot_ltp >= target:
                exit_triggered = True
                verdict = "WIN"
                exit_spot = target
        else:
            if spot_ltp >= sl:
                exit_triggered = True
                verdict = "LOSS"
                exit_spot = sl
            elif spot_ltp <= target:
                exit_triggered = True
                verdict = "WIN"
                exit_spot = target

        if exit_triggered:
            self._exit_position(kc, verdict, exit_spot)

    def _exit_position(self, kc, verdict, exit_spot):
        t = self.active_trade
        if not t:
            return

        exit_premium = t["current_premium"]
        total_shares = t["lots"] * t["lot_size"]
        options_brokerage = 40.0
        options_slippage = 0.5

        if self.mode == "live" and t["tradingsymbol"]:
            try:
                # Fetch current option quotes
                quote = kc.quote([f"NFO:{t['tradingsymbol']}"])
                opt_q = quote.get(f"NFO:{t['tradingsymbol']}")
                
                if verdict == "WIN":
                    # Place Limit Sell at Best Ask (demanding the seller's price!)
                    sell_depth = opt_q.get("depth", {}).get("sell", []) if opt_q else []
                    best_ask = float(sell_depth[0]["price"]) if sell_depth else exit_premium
                    limit_price = round(best_ask, 1)
                    
                    self._log(f"[LIVE] Exiting WIN: Placing LIMIT SELL order (at Best Ask) at Rs. {limit_price} for {t['lots']} lots...")
                    oid = kc.place_order(
                        variety          = kc.VARIETY_REGULAR,
                        exchange         = kc.EXCHANGE_NFO,
                        tradingsymbol    = t["tradingsymbol"],
                        transaction_type = kc.TRANSACTION_TYPE_SELL,
                        quantity         = t["lots"] * t["lot_size"],
                        product          = kc.PRODUCT_MIS,
                        order_type       = kc.ORDER_TYPE_LIMIT,
                        price            = limit_price,
                    )
                    self._log(f"[LIVE] Target exit order placed: {oid}. Polling for fill (max 10 seconds)...")
                    
                    filled = False
                    for seconds_elapsed in range(10):
                        time.sleep(1)
                        orders = kc.orders()
                        o = next((x for x in orders if str(x["order_id"]) == str(oid)), None)
                        if o and o["status"] == "COMPLETE":
                            exit_premium = float(o["average_price"])
                            self._log(f"[LIVE] FILLED close @ Rs. {exit_premium:.2f} premium (Matched Ask!)")
                            filled = True
                            break
                    
                    if not filled:
                        self._log(f"[LIVE] Target limit order did not fill in 10 seconds. Modifying to current bid to force exit...")
                        try:
                            q2 = kc.quote([f"NFO:{t['tradingsymbol']}"])
                            q2_q = q2.get(f"NFO:{t['tradingsymbol']}")
                            b_depth = q2_q.get("depth", {}).get("buy", []) if q2_q else []
                            new_bid = float(b_depth[0]["price"]) if b_depth else exit_premium
                            
                            kc.modify_order(
                                variety=kc.VARIETY_REGULAR,
                                order_id=oid,
                                price=round(new_bid, 1)
                            )
                            self._log(f"[LIVE] Modified target order price to current bid Rs. {new_bid:.2f}. Waiting for fill...")
                            time.sleep(1)
                            orders = kc.orders()
                            o = next((x for x in orders if str(x["order_id"]) == str(oid)), None)
                            if o and o["status"] == "COMPLETE":
                                exit_premium = float(o["average_price"])
                                self._log(f"[LIVE] FILLED close @ Rs. {exit_premium:.2f}")
                        except Exception as me:
                            self._log(f"[LIVE] Modify failed or already complete: {me}")
                
                else:
                    # For SL/TIMEOUT/BREAKEVEN: Place Limit Sell at Best Bid to exit instantly with zero slippage below bid
                    buy_depth = opt_q.get("depth", {}).get("buy", []) if opt_q else []
                    best_bid = float(buy_depth[0]["price"]) if buy_depth else exit_premium
                    limit_price = round(best_bid, 1)
                    
                    self._log(f"[LIVE] Exiting {verdict}: Placing LIMIT SELL order (at Best Bid) at Rs. {limit_price} to close {t['lots']} lots...")
                    oid = kc.place_order(
                        variety          = kc.VARIETY_REGULAR,
                        exchange         = kc.EXCHANGE_NFO,
                        tradingsymbol    = t["tradingsymbol"],
                        transaction_type = kc.TRANSACTION_TYPE_SELL,
                        quantity         = t["lots"] * t["lot_size"],
                        product          = kc.PRODUCT_MIS,
                        order_type       = kc.ORDER_TYPE_LIMIT,
                        price            = limit_price,
                    )
                    self._log(f"[LIVE] Exit order placed: {oid}. Polling for fill (max 5 seconds)...")
                    
                    filled = False
                    for seconds_elapsed in range(5):
                        time.sleep(1)
                        orders = kc.orders()
                        o = next((x for x in orders if str(x["order_id"]) == str(oid)), None)
                        if o and o["status"] == "COMPLETE":
                            exit_premium = float(o["average_price"])
                            self._log(f"[LIVE] FILLED close @ Rs. {exit_premium:.2f}")
                            filled = True
                            break
                            
                    if not filled:
                        self._log(f"[LIVE] Warning: SL limit order did not fill in 5 seconds. Modifying price 2% lower to force exit...")
                        try:
                            kc.modify_order(
                                variety=kc.VARIETY_REGULAR,
                                order_id=oid,
                                price=round(limit_price * 0.97, 1)
                            )
                        except Exception as fe:
                            self._log(f"[LIVE] Force exit modify failed: {fe}")
                            
            except Exception as e:
                self.state = "error"
                self._log(f"[LIVE ERROR] Exit Order Failed: {e} — EXIT POSITION MANUALLY!")

        # Compute P&L
        if verdict == "BREAKEVEN":
            pnl = - options_brokerage - (options_slippage * total_shares)
        else:
            pnl_gross = (exit_premium - t["entry_premium"]) * total_shares
            pnl = pnl_gross - options_brokerage - (options_slippage * total_shares)

        completed_trade = {
            "date": self._ist().strftime("%Y-%m-%d"),
            "symbol": t["symbol"],
            "side": t["side"],
            "entry_time": t["entry_time"],
            "exit_time": self._ist().strftime("%H:%M:%S"),
            "duration": f"{int((time.time() - t['started_at']) / 60)}m",
            "entry_spot": t["entry_spot"],
            "exit_spot": exit_spot,
            "entry_premium": t["entry_premium"],
            "exit_premium": exit_premium,
            "result": verdict,
            "pnl": round(pnl),
            "lots": t["lots"]
        }

        with self.lock:
            self.completed_trades.append(completed_trade)
            self.active_trade = None
            self.state = "scanning"
        self._reset_scanning_state()

        self._log(f"TRADE CLOSED: {completed_trade['side']} -> {verdict} | P&L: Rs. {completed_trade['pnl']:+}")



    def _enter_vol_pcr_position(self, kc, side, spot_ltp, atr_val):
        is_call = "CALL" in side
        opt_type = "CE" if is_call else "PE"
        expiry_date = Z.get_expiry_date(kc, self._ist().date())
        today_date = self._ist().date()
        if today_date == expiry_date:
            now_ist = self._ist()
            if now_ist.hour > 12 or (now_ist.hour == 12 and now_ist.minute >= 30):
                insts = Z.get_nfo_instruments(kc)
                next_exp = None
                if insts:
                    try:
                        expiries = sorted(list({
                            dt.datetime.strptime(i["expiry"], "%Y-%m-%d").date()
                            for i in insts
                            if i.get("name") == "NIFTY" and i.get("expiry")
                        }))
                        idx = expiries.index(expiry_date)
                        if idx + 1 < len(expiries):
                            next_exp = expiries[idx + 1]
                    except Exception:
                        pass
                if next_exp:
                    expiry_date = next_exp
        
        strike = int(round(spot_ltp / 50.0) * 50.0)
        opt_token = Z.get_option_token(kc, "NIFTY", expiry_date, strike, opt_type)
        tradingsymbol = ""
        lot_size = 75
        if opt_token:
            insts = Z.get_nfo_instruments(kc)
            matched = next((i for i in insts if int(i.get("instrument_token") or 0) == opt_token), None)
            if matched:
                tradingsymbol = matched["tradingsymbol"]
                lot_size = int(matched.get("lot_size") or 75)
        
        entry_premium = spot_ltp * 0.005
        opt_q = None
        if tradingsymbol:
            try:
                quote = kc.quote([f"NFO:{tradingsymbol}"])
                opt_q = quote.get(f"NFO:{tradingsymbol}")
                if opt_q:
                    entry_premium = float(opt_q.get("last_price") or entry_premium)
            except Exception:
                pass
        
        # Apply the fixed 2.0 point premium discount on entry
        buy_depth = opt_q.get("depth", {}).get("buy", []) if opt_q else []
        current_bid = float(buy_depth[0]["price"]) if buy_depth else entry_premium
        limit_price = max(1.0, round(current_bid - 1.0, 1))
        
        lots = 1
        spot_sl = (spot_ltp - 2.0 * atr_val) if is_call else (spot_ltp + 2.0 * atr_val)
        spot_target = (spot_ltp + 2.0 * atr_val) if is_call else (spot_ltp - 2.0 * atr_val)
        symbol_str = f"NIFTY {expiry_date.strftime('%d %b').upper()} {strike} {opt_type}"
        
        if self.vol_pcr_mode == "live":
            if not tradingsymbol:
                raise ValueError(f"Could not resolve live Nifty Option tradingsymbol for Vol PCR entry.")
            lot_cost = limit_price * lot_size
            required_capital = lot_cost * lots
            if self.capital < required_capital:
                raise ValueError(f"Insufficient capital for Vol PCR Strategy. Required: Rs. {required_capital}, Available: Rs. {self.capital}")
            
            self._log(f"[LIVE VOL PCR] Placing PASSIVE LIMIT BUY order at Rs. {limit_price} for {lots} lots of {tradingsymbol}...")
            try:
                oid = kc.place_order(
                    variety          = kc.VARIETY_REGULAR,
                    exchange         = kc.EXCHANGE_NFO,
                    tradingsymbol    = tradingsymbol,
                    transaction_type = kc.TRANSACTION_TYPE_BUY,
                    quantity         = lots * lot_size,
                    product          = kc.PRODUCT_MIS,
                    order_type       = kc.ORDER_TYPE_LIMIT,
                    price            = limit_price
                )
                entry_vol_pcr = self._oi_metrics.get("vol_pcr", 1.0) if getattr(self, "_oi_metrics", None) else 1.0
                with self.lock:
                    self.vol_pcr_active_trade = {
                        "side": side,
                        "symbol": symbol_str,
                        "tradingsymbol": tradingsymbol,
                        "entry_time": self._ist().strftime("%H:%M:%S"),
                        "entry_spot": spot_ltp,
                        "spot_sl": spot_sl,
                        "spot_target": spot_target,
                        "current_sl": spot_sl,
                        "entry_premium": limit_price,
                        "current_premium": limit_price,
                        "lots": lots,
                        "lot_size": lot_size,
                        "started_at": time.time(),
                        "order_id": oid,
                        "filled": False,
                        "min_vol_pcr": entry_vol_pcr,
                        "max_vol_pcr": entry_vol_pcr,
                        "reached_halfway": False
                    }
                    self._save_state()
            except Exception as e:
                self._log(f"[LIVE VOL PCR ERROR] Failed to place limit order: {e}")
        else:
            # Paper trading fills immediately
            self._log(f"[VOL PCR PAPER] Simulating Buy 1 lots of {symbol_str} @ Rs. {limit_price:.2f} premium (LTP: Rs. {entry_premium:.2f})")
            entry_vol_pcr = self._oi_metrics.get("vol_pcr", 1.0) if getattr(self, "_oi_metrics", None) else 1.0
            with self.lock:
                self.vol_pcr_active_trade = {
                    "side": side,
                    "symbol": symbol_str,
                    "tradingsymbol": tradingsymbol,
                    "entry_time": self._ist().strftime("%H:%M:%S"),
                    "entry_spot": spot_ltp,
                    "spot_sl": spot_sl,
                    "spot_target": spot_target,
                    "current_sl": spot_sl,
                    "entry_premium": limit_price,
                    "current_premium": limit_price,
                    "lots": lots,
                    "lot_size": lot_size,
                    "started_at": time.time(),
                    "filled": True,
                    "min_vol_pcr": entry_vol_pcr,
                    "max_vol_pcr": entry_vol_pcr,
                    "reached_halfway": False
                }
                self._save_state()

    def _manage_vol_pcr_pending_order(self, kc, spot_ltp, quote_data=None):
        t = self.vol_pcr_active_trade
        if not t:
            return

        is_call = "CALL" in t["side"]
        sl = t["spot_sl"]
        target = t["spot_target"]

        cancel_needed = False
        reason = ""

        # Cancel if pending for more than 60 seconds (prevent stale fills)
        elapsed_sec = time.time() - t["started_at"]
        if elapsed_sec >= 120.0:
            cancel_needed = True
            reason = f"Order pending timeout (120s elapsed)"

        elif is_call:
            if spot_ltp <= sl:
                cancel_needed = True
                reason = f"Spot price hit SL Rs. {sl:.2f} before fill"
            elif spot_ltp >= target:
                cancel_needed = True
                reason = f"Spot price hit Target Rs. {target:.2f} before fill"
        else:
            if spot_ltp >= sl:
                cancel_needed = True
                reason = f"Spot price hit SL Rs. {sl:.2f} before fill"
            elif spot_ltp <= target:
                cancel_needed = True
                reason = f"Spot price hit Target Rs. {target:.2f} before fill"

        if cancel_needed:
            self._log(f"[VOL PCR PENDING] {reason}. Cancelling pending order {t['order_id']}...")
            try:
                if self.vol_pcr_mode == "live":
                    kc.cancel_order(variety=kc.VARIETY_REGULAR, order_id=t["order_id"])
            except Exception as ce:
                self._log(f"[VOL PCR PENDING] Cancel failed: {ce}")
            self.vol_pcr_active_trade = None
            self._save_state()
            return

        # Poll order status from exchange once every 2 seconds
        now = time.time()
        if not hasattr(self, "_last_vol_order_poll"):
            self._last_vol_order_poll = 0

        if now - self._last_vol_order_poll >= 2.0:
            self._last_vol_order_poll = now
            try:
                if self.vol_pcr_mode == "live":
                    orders = kc.orders()
                    o = next((x for x in orders if str(x["order_id"]) == str(t["order_id"])), None)
                    if o:
                        status = o["status"]
                        if status == "COMPLETE":
                            t["entry_premium"] = float(o["average_price"])
                            t["current_premium"] = t["entry_premium"]
                            t["started_at"] = time.time()
                            t["filled"] = True
                            self._log(f"[LIVE VOL PCR] Pending order FILLED at Rs. {t['entry_premium']:.2f}")
                            self._save_state()
                        elif status in ["CANCELLED", "REJECTED"]:
                            self._log(f"[LIVE VOL PCR] Pending order was {status}. Reason: {o.get('status_message', 'none')}")
                            self.vol_pcr_active_trade = None
                            self._save_state()
            except Exception as e:
                self._log(f"[VOL PCR PENDING] Error checking order status: {e}")

    def _manage_vol_pcr_position(self, kc, spot_ltp, quote_data=None):
        t = self.vol_pcr_active_trade
        if not t:
            return
        
        side = t["side"]
        entry_spot = t["entry_spot"]
        target = t["spot_target"]
        sl = t["spot_sl"]
        is_call = "CALL" in side
        
        # Update option LTP for floating P&L display
        if self.vol_pcr_mode == "live" and t["tradingsymbol"]:
            try:
                opt_key = f"NFO:{t['tradingsymbol']}"
                if quote_data and opt_key in quote_data:
                    opt_q = quote_data[opt_key]
                else:
                    quote = kc.quote([opt_key])
                    opt_q = quote.get(opt_key)
                if opt_q:
                    t["current_premium"] = float(opt_q.get("last_price") or t["current_premium"])
            except Exception:
                pass
        else:
            # Paper premium tracking (delta = 0.50)
            spot_change = (spot_ltp - entry_spot) if is_call else (entry_spot - spot_ltp)
            t["current_premium"] = max(1.0, t["entry_premium"] + (spot_change * 0.50))
        
        # Max loss floor (50%)
        if t["current_premium"] <= 0.50 * t["entry_premium"]:
            self._log(f"[VOL PCR] Max Loss Trigger: Premium lost 50% value. Exiting.")
            self._exit_vol_pcr_position(kc, "LOSS", spot_ltp)
            return
        
        # Timeout 45m
        elapsed_mins = (time.time() - t["started_at"]) / 60.0
        if elapsed_mins >= 45.0:
            self._log(f"[VOL PCR] Time-Decay Timeout (45m). Exiting.")
            self._exit_vol_pcr_position(kc, "TIMEOUT", spot_ltp)
            return
            
        # Vol PCR trailing/hard exits (0.20 offset or opposite triggers)
        vol_pcr_val = self._oi_metrics.get("vol_pcr", 1.0) if getattr(self, "_oi_metrics", None) else 1.0
        if is_call:
            t["min_vol_pcr"] = min(t.get("min_vol_pcr", vol_pcr_val), vol_pcr_val)
            limit_pcr = t["min_vol_pcr"] + 0.20
            if vol_pcr_val >= limit_pcr or vol_pcr_val >= 1.10:
                self._log(f"[VOL PCR] Volume PCR Trailing/Hard Exit: Vol PCR EMA hit {vol_pcr_val:.2f} (Lowest PCR: {t['min_vol_pcr']:.2f} + 0.20 offset = {limit_pcr:.2f} | Hard Barrier: 1.10). Exiting early.")
                self._exit_vol_pcr_position(kc, "LOSS", spot_ltp)
                return
        else:
            t["max_vol_pcr"] = max(t.get("max_vol_pcr", vol_pcr_val), vol_pcr_val)
            limit_pcr = t["max_vol_pcr"] - 0.20
            if vol_pcr_val <= limit_pcr or vol_pcr_val <= 0.90:
                self._log(f"[VOL PCR] Volume PCR Trailing/Hard Exit: Vol PCR EMA hit {vol_pcr_val:.2f} (Highest PCR: {t['max_vol_pcr']:.2f} - 0.20 offset = {limit_pcr:.2f} | Hard Barrier: 0.90). Exiting early.")
                self._exit_vol_pcr_position(kc, "LOSS", spot_ltp)
                return
                
        # Check premium profit target of 3.0 points (Everywhere)
        if t["current_premium"] - t["entry_premium"] >= 3.0:
            self._log(f"[VOL PCR] Premium Target Reached (+3.0pts): Entry: Rs. {t['entry_premium']:.2f}, Current: Rs. {t['current_premium']:.2f}. Booking profits.")
            self._exit_vol_pcr_position(kc, "WIN", spot_ltp)
            return
            
        # SL/Target Spot Check
        exit_triggered = False
        verdict = "OPEN"
        exit_spot = spot_ltp
        
        if is_call:
            if spot_ltp <= sl:
                exit_triggered = True
                verdict = "LOSS"
                exit_spot = sl
            elif spot_ltp >= target:
                exit_triggered = True
                verdict = "WIN"
                exit_spot = target
        else:
            if spot_ltp >= sl:
                exit_triggered = True
                verdict = "LOSS"
                exit_spot = sl
            elif spot_ltp <= target:
                exit_triggered = True
                verdict = "WIN"
                exit_spot = target
                
        if exit_triggered:
            self._exit_vol_pcr_position(kc, verdict, exit_spot)

    def _exit_vol_pcr_position(self, kc, verdict, exit_spot):
        t = self.vol_pcr_active_trade
        if not t:
            return
            
        exit_premium = t["current_premium"]
        total_shares = t["lots"] * t["lot_size"]
        options_brokerage = 40.0
        options_slippage = 0.5
        
        if self.vol_pcr_mode == "live" and t["tradingsymbol"]:
            try:
                # Fetch current option quotes
                quote = kc.quote([f"NFO:{t['tradingsymbol']}"])
                opt_q = quote.get(f"NFO:{t['tradingsymbol']}")
                
                # Place Limit Sell at Best Ask (if winning) or Market (if losing/timeout)
                if verdict == "WIN":
                    sell_depth = opt_q.get("depth", {}).get("sell", []) if opt_q else []
                    best_ask = float(sell_depth[0]["price"]) if sell_depth else exit_premium
                    limit_price = round(best_ask, 1)
                    self._log(f"[LIVE VOL PCR] Exiting WIN: Placing LIMIT SELL order at Rs. {limit_price}...")
                    oid = kc.place_order(
                        variety          = kc.VARIETY_REGULAR,
                        exchange         = kc.EXCHANGE_NFO,
                        tradingsymbol    = t["tradingsymbol"],
                        transaction_type = kc.TRANSACTION_TYPE_SELL,
                        quantity         = t["lots"] * t["lot_size"],
                        product          = kc.PRODUCT_MIS,
                        order_type       = kc.ORDER_TYPE_LIMIT,
                        price            = limit_price
                    )
                else:
                    self._log(f"[LIVE VOL PCR] Exiting LOSS/TIMEOUT: Placing MARKET SELL order...")
                    oid = kc.place_order(
                        variety          = kc.VARIETY_REGULAR,
                        exchange         = kc.EXCHANGE_NFO,
                        tradingsymbol    = t["tradingsymbol"],
                        transaction_type = kc.TRANSACTION_TYPE_SELL,
                        quantity         = t["lots"] * t["lot_size"],
                        product          = kc.PRODUCT_MIS,
                        order_type       = kc.ORDER_TYPE_MARKET
                    )
            except Exception as e:
                self._log(f"[LIVE VOL PCR EXIT ERROR] Exit order placement failed: {e}")
            
        gross_pnl = (exit_premium - t["entry_premium"]) * total_shares
        net_pnl = gross_pnl - (t["lots"] * options_brokerage) - (options_slippage * total_shares)
        
        completed_trade = {
            "entry_time": t["entry_time"],
            "exit_time": self._ist().strftime("%H:%M:%S"),
            "duration": f"{int((time.time() - t['started_at']) / 60.0)}m",
            "symbol": t["symbol"],
            "side": t["side"],
            "lots": t["lots"],
            "entry_premium": t["entry_premium"],
            "exit_premium": exit_premium,
            "result": verdict,
            "pnl": round(net_pnl)
        }
        
        with self.lock:
            self.vol_pcr_completed_trades.append(completed_trade)
            self.vol_pcr_active_trade = None
        self._reset_scanning_state()
        self._save_state()
            
        self._log(f"[VOL PCR CLOSED] {completed_trade['side']} -> {verdict} | P&L: Rs. {completed_trade['pnl']:+}")

    def status(self):
        with self.lock:
            floating_pnl = 0
            if self.active_trade:
                t = self.active_trade
                total_shares = t["lots"] * t["lot_size"]
                options_brokerage = 40.0
                options_slippage = 0.5
                pnl_gross = (t["current_premium"] - t["entry_premium"]) * total_shares
                floating_pnl = pnl_gross - (t["lots"] * options_brokerage) - (options_slippage * total_shares)

            # Calculate dynamic targets for live display
            setup_info = {
                "l_stage": self.l_stage,
                "l_peak": round(self.l_peak, 2) if self.l_peak else None,
                "l_trough": round(self.l_trough, 2) if self.l_trough else None,
                "l_drop_target": round(self.l_peak - config.ATR_DROP_MULT * (self.l_peak_atr or 7.0), 2) if self.l_peak else None,
                "l_bounce_target": round(self.l_trough + config.ATR_BOUNCE_MULT * (self.l_peak_atr or 7.0), 2) if (self.l_stage == 3 and self.l_trough) else None,
                
                "s_stage": self.s_stage,
                "s_trough": round(self.s_trough, 2) if self.s_trough else None,
                "s_peak": round(self.s_peak, 2) if self.s_peak else None,
                "s_rally_target": round(self.s_trough + config.ATR_DROP_MULT * (self.s_trough_atr or 7.0), 2) if self.s_trough else None,
                "s_drop_target": round(self.s_peak - config.ATR_BOUNCE_MULT * (self.s_trough_atr or 7.0), 2) if (self.s_stage == 3 and self.s_peak) else None,
            }

            # Calculate Vol PCR strategy active trade metrics
            pt = self.vol_pcr_active_trade
            vol_pcr_floating_pnl = 0
            if pt:
                pt_shares = pt["lots"] * pt["lot_size"]
                opt_brokerage = 40.0
                opt_slippage = 0.5
                pt_pnl_gross = (pt["current_premium"] - pt["entry_premium"]) * pt_shares
                vol_pcr_floating_pnl = pt_pnl_gross - (pt["lots"] * opt_brokerage) - (opt_slippage * pt_shares)

            return {
                "vol_pcr_active_trade": {
                    "symbol": pt["symbol"] if self.vol_pcr_active_trade else "-",
                    "side": pt["side"] if self.vol_pcr_active_trade else "-",
                    "entry_time": pt["entry_time"] if self.vol_pcr_active_trade else "-",
                    "entry_spot": round(pt["entry_spot"], 2) if self.vol_pcr_active_trade else 0,
                    "spot_target": round(pt["spot_target"], 2) if self.vol_pcr_active_trade else 0,
                    "current_sl": round(pt["current_sl"], 2) if self.vol_pcr_active_trade else 0,
                    "entry_premium": round(pt["entry_premium"], 2) if self.vol_pcr_active_trade else 0,
                    "current_premium": round(pt["current_premium"], 2) if self.vol_pcr_active_trade else 0,
                    "lots": pt["lots"] if self.vol_pcr_active_trade else 0,
                    "pnl": round(vol_pcr_floating_pnl) if self.vol_pcr_active_trade else 0
                } if self.vol_pcr_active_trade else None,
                "vol_pcr_completed_trades": self.vol_pcr_completed_trades,
            "vol_pcr_mode": self.vol_pcr_mode,
                "vol_pcr_mode": self.vol_pcr_mode,
                "running": self.running,
                "mode": self.mode,
                "capital": self.capital,
                "state": self.state,
                "logs": self.logs[-30:],
                "active_trade": {
                    "symbol": t["symbol"] if self.active_trade else "-",
                    "side": t["side"] if self.active_trade else "-",
                    "entry_time": t["entry_time"] if self.active_trade else "-",
                    "entry_spot": round(t["entry_spot"], 2) if self.active_trade else 0,
                    "spot_target": round(t["spot_target"], 2) if self.active_trade else 0,
                    "current_sl": round(t["current_sl"], 2) if self.active_trade else 0,
                    "entry_premium": round(t["entry_premium"], 2) if self.active_trade else 0,
                    "current_premium": round(t["current_premium"], 2) if self.active_trade else 0,
                    "lots": t["lots"] if self.active_trade else 0,
                    "pnl": round(floating_pnl) if self.active_trade else 0
                } if self.active_trade else None,
                "completed_trades": self.completed_trades,
                "setup": setup_info,
                "oi_metrics": getattr(self, "_oi_metrics", {})
            }
