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
                "active_trade": self.active_trade
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

    def start(self, capital=40000.0, mode="paper", lot_size_mode="auto", fixed_lots=1):
        with self.lock:
            if self.running:
                return False
            self.running = True
            self.mode = mode.lower()
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
        today_trading_candles = [c for c in temp_candles if c["date"].date() == today_date and c["date"].strftime("%H:%M") >= "09:25"]
        
        self.l_stage = 1
        self.l_peak = None
        self.l_trough = None
        self.l_peak_atr = None
        self.s_stage = 1
        self.s_trough = None
        self.s_peak = None
        self.s_trough_atr = None
        
        if today_trading_candles:
            self._log(f"Replaying {len(today_trading_candles)} of today's candles to reconstruct active stage counters...")
            for c in today_trading_candles:
                high = float(c["high"])
                low = float(c["low"])
                atr = float(c["atr"])
                
                # LONG SETUP REPLAY
                if self.l_stage == 1:
                    if self.l_peak is None or high > self.l_peak:
                        self.l_peak = high
                        self.l_peak_atr = atr
                    else:
                        self.l_trough = low
                        self.l_stage = 2
                elif self.l_stage == 2:
                    if high > self.l_peak:
                        self.l_peak = high
                        self.l_peak_atr = atr
                        self.l_trough = low
                        self.l_stage = 1
                    else:
                        self.l_trough = min(self.l_trough, low)
                        drop_required = 2.5 * (self.l_peak_atr if self.l_peak_atr else atr)
                        if self.l_trough <= self.l_peak - drop_required:
                            self.l_stage = 3
                elif self.l_stage == 3:
                    if low < self.l_trough:
                        self.l_trough = low
                    bounce_required = 0.7 * atr
                    bounce_level = self.l_trough + bounce_required
                    if high >= bounce_level:
                        self.l_peak = None
                        self.l_trough = None
                        self.l_stage = 1
                        
                # SHORT SETUP REPLAY
                if self.s_stage == 1:
                    if self.s_trough is None or low < self.s_trough:
                        self.s_trough = low
                        self.s_trough_atr = atr
                    else:
                        self.s_peak = high
                        self.s_stage = 2
                elif self.s_stage == 2:
                    if low < self.s_trough:
                        self.s_trough = low
                        self.s_trough_atr = atr
                        self.s_peak = high
                        self.s_stage = 1
                    else:
                        self.s_peak = max(self.s_peak, high)
                        rally_required = 2.5 * (self.s_trough_atr if self.s_trough_atr else atr)
                        if self.s_peak >= self.s_trough + rally_required:
                            self.s_stage = 3
                elif self.s_stage == 3:
                    if high > self.s_peak:
                        self.s_peak = high
                    drop_required = 0.7 * atr
                    short_trigger_level = self.s_peak - drop_required
                    if low <= short_trigger_level:
                        self.s_trough = None
                        self.s_peak = None
                        self.s_stage = 1
            
            self._log(f"Replay complete. Reconstructed State: Long Stage {self.l_stage} (Peak: {self.l_peak}, Trough: {self.l_trough}), Short Stage {self.s_stage} (Trough: {self.s_trough}, Peak: {self.s_peak})")

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
                if self.nifty_open is None:
                    self.nifty_open = ltp
                    self._log(f"First Nifty Spot Tick observed. Session Open locked at Rs. {self.nifty_open}")

                ist = self._ist()
                time_str = ist.strftime("%H:%M")
                minute_key = ist.strftime("%Y-%m-%d %H:%M")

                # Real-Time Tick Trigger Checks when in Stage 3
                is_valid_time = "09:25" <= time_str < "15:30"
                if len(self.candles) > 0 and is_valid_time and not self.active_trade:
                    last_c = self.candles[-1]
                    atr_val = last_c.get("atr", 7.0)
                    ema_val = last_c.get("nifty_ema", ltp)
                    
                    is_nifty_green_today = ltp > self.nifty_open if self.nifty_open else True
                    is_nifty_red_today = ltp < self.nifty_open if self.nifty_open else True
                    is_nifty_above_ema = ltp > ema_val
                    is_nifty_below_ema = ltp < ema_val
                    
                    # LONG TRIGGER CHECK
                    if self.l_stage == 3:
                        bounce_required = 0.7 * atr_val
                        bounce_level = self.l_trough + bounce_required
                        if ltp >= bounce_level:
                             if is_nifty_above_ema and is_nifty_green_today and self.has_vol_conf:
                                # Trigger LONG CE Trade instantly!
                                self._enter_position(kc, "BUY CALL (CE)", bounce_level, atr_val, ema_val)
                                self.l_peak = None
                                self.l_trough = None
                                self.l_stage = 1
                                
                    # SHORT TRIGGER CHECK
                    if self.s_stage == 3 and not self.active_trade:
                        drop_required = 0.7 * atr_val
                        short_trigger_level = self.s_peak - drop_required
                        if ltp <= short_trigger_level:
                             if is_nifty_below_ema and is_nifty_red_today and self.has_vol_conf:
                                # Trigger SHORT PE Trade instantly!
                                self._enter_position(kc, "BUY PUT (PE)", short_trigger_level, atr_val, ema_val)
                                self.s_trough = None
                                self.s_peak = None
                                self.s_stage = 1

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

                        # Run state machine on completed candle
                        high = live_high
                        low = live_low
                        close = live_close
                        atr = atr_val
                        nifty_ema = ema_val

                        is_nifty_above_ema = close > nifty_ema
                        is_nifty_below_ema = close < nifty_ema
                        is_nifty_green_today = close > self.nifty_open
                        is_nifty_red_today = close < self.nifty_open
                        is_valid_time = "09:25" <= time_str < "15:30"

                        # LONG STATE MACHINE
                        if self.l_stage == 1:
                            if self.l_peak is None or high > self.l_peak:
                                self.l_peak = high
                                self.l_peak_atr = atr
                            else:
                                self.l_trough = low
                                self.l_stage = 2
                        elif self.l_stage == 2:
                            if high > self.l_peak:
                                self.l_peak = high
                                self.l_peak_atr = atr
                                self.l_trough = low
                                self.l_stage = 1
                            else:
                                self.l_trough = min(self.l_trough, low)
                                drop_required = 2.5 * (self.l_peak_atr if self.l_peak_atr else atr)
                                if self.l_trough <= self.l_peak - drop_required:
                                    self.l_stage = 3
                        elif self.l_stage == 3:
                            if low < self.l_trough:
                                self.l_trough = low

                        # SHORT STATE MACHINE
                        if not self.active_trade:
                            if self.s_stage == 1:
                                if self.s_trough is None or low < self.s_trough:
                                    self.s_trough = low
                                    self.s_trough_atr = atr
                                else:
                                    self.s_peak = high
                                    self.s_stage = 2
                            elif self.s_stage == 2:
                                if low < self.s_trough:
                                    self.s_trough = low
                                    self.s_trough_atr = atr
                                    self.s_peak = high
                                    self.s_stage = 1
                                else:
                                    self.s_peak = max(self.s_peak, high)
                                    rally_required = 2.5 * (self.s_trough_atr if self.s_trough_atr else atr)
                                    if self.s_peak >= self.s_trough + rally_required:
                                        self.s_stage = 3
                            elif self.s_stage == 3:
                                if high > self.s_peak:
                                    self.s_peak = high

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

                # If in position, manage exits at 1-second resolution
                if self.active_trade:
                    self._manage_active_position(kc, ltp, quote_data=q)

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
        sl_points = max(atr, 7.0)
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

        self._log(f"SIGNAL FIRED: {side} | Signal Spot: ₹{entry_spot:.2f} | Fill Spot: ₹{current_spot:.2f} | Target: ₹{spot_target:.2f} | SL: ₹{spot_sl:.2f} (ATR={atr:.2f})")
        self.state = "in-trade"

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

                # Place order at the best bid price to buy at a discount (saving spread)
                buy_depth = opt_q.get("depth", {}).get("buy", []) if opt_q else []
                best_bid = float(buy_depth[0]["price"]) if buy_depth else entry_premium
                limit_price = round(best_bid, 1)
                
                self._log(f"[LIVE] Placing DISCOUNT LIMIT BUY order (at Best Bid) at Rs. {limit_price} for {lots} lots of {tradingsymbol}...")
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
                self._log(f"[LIVE] Order placed successfully. ID: {oid}. Polling for fill (max 10 seconds)...")
                
                # Poll order status for fill
                filled = False
                for seconds_elapsed in range(10):
                    time.sleep(1)
                    orders = kc.orders()
                    o = next((x for x in orders if str(x["order_id"]) == str(oid)), None)
                    if o:
                        status = o["status"]
                        if status == "COMPLETE":
                            entry_premium = float(o["average_price"])
                            self._log(f"[LIVE] FILLED at premium Rs. {entry_premium:.2f} (Matched Bid!)")
                            filled = True
                            break
                        elif status in ["CANCELLED", "REJECTED"]:
                            raise ValueError(f"Order was {status}. Reason: {o.get('status_message', 'none')}")
                
                if not filled:
                    self._log(f"[LIVE] Limit order did not fill in 10 seconds. Cancelling order {oid} to avoid stale entry...")
                    try:
                        kc.cancel_order(variety=kc.VARIETY_REGULAR, order_id=oid)
                    except Exception as ce:
                        self._log(f"[LIVE] Cancel failed or already filled: {ce}")
                    
                    # Return out of _enter_position to scan again
                    self.active_trade = None
                    self.state = "scanning"
                    if opt_type == "CE":
                        self.l_stage = 3
                    else:
                        self.s_stage = 3
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
        options_slippage = 2.0

        if self.mode == "live" and t["tradingsymbol"]:
            try:
                # Fetch current option quotes
                quote = kc.quote([f"NFO:{t['tradingsymbol']}"])
                opt_q = quote.get(f"NFO:{t['tradingsymbol']}")
                
                if verdict == "TARGET":
                    # Place Limit Sell at Best Ask (demanding the seller's price!)
                    sell_depth = opt_q.get("depth", {}).get("sell", []) if opt_q else []
                    best_ask = float(sell_depth[0]["price"]) if sell_depth else exit_premium
                    limit_price = round(best_ask, 1)
                    
                    self._log(f"[LIVE] Exiting TARGET: Placing LIMIT SELL order (at Best Ask) at Rs. {limit_price} for {t['lots']} lots...")
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

        self._log(f"TRADE CLOSED: {completed_trade['side']} -> {verdict} | P&L: Rs. {completed_trade['pnl']:+}")

    def status(self):
        with self.lock:
            floating_pnl = 0
            if self.active_trade:
                t = self.active_trade
                total_shares = t["lots"] * t["lot_size"]
                options_brokerage = 40.0
                options_slippage = 2.0
                pnl_gross = (t["current_premium"] - t["entry_premium"]) * total_shares
                floating_pnl = pnl_gross - (t["lots"] * options_brokerage) - (options_slippage * total_shares)

            return {
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
                "completed_trades": self.completed_trades
            }
