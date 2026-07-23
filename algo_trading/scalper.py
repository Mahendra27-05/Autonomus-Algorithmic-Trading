# ==========================================
# IVAN ALGOBOT - INSTITUTIONAL SCALPING EDITION
# ==========================================
# Mode: PAPER / LIVE | Strategy: Mean Reversion Scalping (BB Reclaim + Vol Spike)
# Risk: 0.5% Equity/Trade | Kelly Sizing | 5s Heartbeat | Max 15 Trades/Day
# ==========================================

import json
import os
import time
import math
import logging
import threading
from datetime import datetime, timedelta, date
from abc import ABC, abstractmethod
from typing import Dict, Optional, Tuple, List
from dataclasses import dataclass, field
from enum import Enum
from collections import deque

import numpy as np
import pandas as pd
import pandas_ta as ta
import pyotp
import requests
from growwapi import GrowwAPI

# ==========================================
# 1. CREDENTIALS & CONFIGURATION
# ==========================================
GROWW_API_KEY = "----"       # Groww API TOTP Token
GROWW_TOTP_SECRET = "----"   # Groww 32_STRING Access Token

TELEGRAM_BOT_TOKEN = "----"  # Telegram API Token
TELEGRAM_CHAT_ID = "----"    # Telegram Chat ID
PREMIUM_ACCESS_KEY = "----"  # Access Key for other or public to view 

# --- TRADING MODE SWITCH ---
TRADING_MODE = "PAPER"  # "PAPER" or "LIVE"

# --- PAPER TRADING CONFIG ---
PAPER_STARTING_CAPITAL = 1000000.0
PAPER_MAX_DAILY_LOSS_PCT = 5.0
PAPER_MAX_POSITION_PCT = 20.0
PAPER_SLIPPAGE_PCT = 0.05
PAPER_BROKERAGE_PER_ORDER = 20.0
PAPER_DATA_FILE = "paper_account.json"
PAPER_TRADES_LOG = "paper_trades_log.csv"
PAPER_DAILY_PNL_FILE = "daily_pnl.csv"

# --- TELEGRAM & STRATEGY CONFIG ---
ENABLE_TELEGRAM_ALERTS = False  # Switch to True to resume telegram notifications
INDEX_PREFIX = "NSE-NIFTY"
FUTURES_EXPIRY = "28Jul26"
FUTURES_SYMBOL = f"{INDEX_PREFIX}-{FUTURES_EXPIRY}-FUT"
QUANTITY = 65  # Base Lot Size (1 Lot = 65 Qty)
PRODUCT_TYPE = "MIS"
ORDER_TYPE_ENTRY = "MARKET"
ORDER_TYPE_SL = "SL-M"
SQUARE_OFF_TIME = "15:20:00"

# ==========================================
# 2. INSTITUTIONAL SCALPING PARAMETERS
# ==========================================
SCALP_LOOKBACK = 100
SCALP_RISK_PER_TRADE_PCT = 0.5      # 0.5% Equity Risk Per Trade
SCALP_MAX_DAILY_TRADES = 15         # Overtrading Circuit Breaker
SCALP_COOLDOWN_SECS = 120           # 2 Min Cooldown
SCALP_MAX_HOLD_SECS = 300           # 5 Min Max Hold (Time Stop)
SCALP_TARGET_R_MULT = 1.5           # Target 1.5R
SCALP_BE_TRIGGER_R = 0.8            # Breakeven at 0.8R
SCALP_TSL_TRIGGER_R = 1.2           # Trailing Start at 1.2R
SCALP_TSL_STEP_R = 0.3              # Trail Step 0.3R
SCALP_SPREAD_THRESHOLD_PCT = 0.15   # Max Spread % (Liquidity Filter)
SCALP_VOL_SPIKE_MULT = 2.0          # Volume > 2x Avg
SCALP_ADX_TREND_THRESHOLD = 25      # Block if ADX > 25 (Trending)
SCALP_ATR_PERIOD = 14
SCALP_BB_PERIOD = 20
SCALP_BB_STD = 2.0
SCALP_RSI_PERIOD = 7
SCALP_RSI_OB = 70
SCALP_RSI_OS = 30

# Pandas TA Column Name Constants
BB_PERIOD = SCALP_BB_PERIOD
BB_STD = SCALP_BB_STD
ATR_COL = f'ATRR_{SCALP_ATR_PERIOD}'
ADX_COL = 'ADX_14'
RSI_COL = f'RSI_{SCALP_RSI_PERIOD}'
BB_U_COL = f'BBU_{BB_PERIOD}_{BB_STD}'
BB_L_COL = f'BBL_{BB_PERIOD}_{BB_STD}'
BB_M_COL = f'BBM_{BB_PERIOD}_{BB_STD}'

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
tg_session = requests.Session()

# ==========================================
# 3. VIP DATABASE & TELEGRAM
# ==========================================
SUBSCRIBER_FILE = "subscribers.json"
LAST_UPDATE_ID_FILE = "last_update_id.txt"

def get_subscribers():
    if not os.path.exists(SUBSCRIBER_FILE): return [TELEGRAM_CHAT_ID]
    with open(SUBSCRIBER_FILE, "r") as f:
        try: return json.load(f)
        except: return [TELEGRAM_CHAT_ID]

def save_subscriber(chat_id):
    chat_id = str(chat_id)
    subs = get_subscribers()
    if chat_id not in subs:
        subs.append(chat_id)
        with open(SUBSCRIBER_FILE, "w") as f: json.dump(subs, f)
        return True
    return False

def check_for_new_subscribers():
    offset = None
    if os.path.exists(LAST_UPDATE_ID_FILE):
        with open(LAST_UPDATE_ID_FILE, "r") as f: offset = int(f.read().strip())
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    if offset: url += f"?offset={offset + 1}"
    try:
        response = tg_session.get(url, timeout=5).json()
        if response.get("ok") and response.get("result"):
            for result in response["result"]:
                update_id = result["update_id"]
                with open(LAST_UPDATE_ID_FILE, "w") as f: f.write(str(update_id))
                message = result.get("message", {})
                chat_id = str(message.get("chat", {}).get("id"))
                text = message.get("text", "")
                if text.startswith("/start") and chat_id:
                    parts = text.strip().split(" ")
                    reply_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
                    if len(parts) == 2 and parts[1] == PREMIUM_ACCESS_KEY:
                        if save_subscriber(chat_id):
                            tg_session.post(reply_url, json={"chat_id": chat_id, "text": "✅ *Access Granted.*\nYou will now receive Ivan AlgoBot signals.", "parse_mode": "Markdown"})
                            logging.info(f"New VIP verified: {chat_id}")
                        else:
                            tg_session.post(reply_url, json={"chat_id": chat_id, "text": "You are already subscribed.", "parse_mode": "Markdown"})
                    else:
                        tg_session.post(reply_url, json={"chat_id": chat_id, "text": "❌ *Access Denied.* Invalid Key.", "parse_mode": "Markdown"})
    except Exception: pass 

def send_telegram_alert(message):
    if not ENABLE_TELEGRAM_ALERTS or not TELEGRAM_BOT_TOKEN: return
    subs = get_subscribers()
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    for chat_id in subs:
        try: tg_session.post(url, json={"chat_id": chat_id, "text": message, "parse_mode": "Markdown"}, timeout=2)
        except Exception as e: logging.error(f"Failed to send to {chat_id}: {e}")

def telegram_background_worker():
    while True:
        check_for_new_subscribers()
        time.sleep(1) 

# ==========================================
# 4. PAPER TRADING ENGINE (FULLY FEATURED)
# ==========================================

class ExecutionEngine(ABC):
    @abstractmethod
    def place_entry_order(self, trading_symbol: str, qty: int, side: str) -> Tuple[bool, float, str]: pass
    @abstractmethod
    def place_sl_order(self, trading_symbol: str, qty: int, trigger_price: float, side: str) -> Tuple[bool, str]: pass
    @abstractmethod
    def modify_sl_order(self, order_id: str, new_trigger_price: float) -> bool: pass
    @abstractmethod
    def cancel_order(self, order_id: str) -> bool: pass
    @abstractmethod
    def check_position_closed(self, trading_symbol: str) -> bool: pass
    @abstractmethod
    def get_ltp(self, trading_symbol: str) -> Optional[float]: pass
    @abstractmethod
    def square_off_all(self): pass

class PaperAccount:
    def __init__(self):
        self.starting_capital = PAPER_STARTING_CAPITAL
        self.cash = PAPER_STARTING_CAPITAL
        self.realized_pnl = 0.0
        self.unrealized_pnl = 0.0
        self.used_margin = 0.0
        self.daily_start_equity = PAPER_STARTING_CAPITAL
        self.max_drawdown_today = 0.0
        self.trade_count = 0
        self.wins = 0
        self.losses = 0
        self.current_position = None
        self.last_save_date = date.today().isoformat()
        self.load_state()
        self._check_new_day()
        logging.info(f"📝 PAPER ACCOUNT LOADED | Equity: ₹{self.get_equity():.2f} | Cash: ₹{self.cash:.2f} | Daily PnL: ₹{self.get_daily_pnl():.2f}")

    def get_equity(self) -> float: return self.cash + self.realized_pnl + self.unrealized_pnl
    def get_free_cash(self) -> float: return self.cash - self.used_margin
    def get_daily_pnl(self) -> float: return self.get_equity() - self.daily_start_equity

    def _check_new_day(self):
        today = date.today().isoformat()
        if today != self.last_save_date:
            logging.info(f"📅 NEW TRADING DAY: {today}")
            self.log_daily_pnl(self.last_save_date, self.get_daily_pnl())
            self.daily_start_equity = self.get_equity()
            self.max_drawdown_today = 0.0
            self.last_save_date = today
            self.save_state()

    def can_trade(self, required_margin: float) -> Tuple[bool, str]:
        self._check_new_day()
        daily_pnl_pct = (self.get_daily_pnl() / self.daily_start_equity) * 100
        if daily_pnl_pct <= -PAPER_MAX_DAILY_LOSS_PCT:
            return False, f"🛑 DAILY LOSS LIMIT HIT ({daily_pnl_pct:.2f}%)"
        if required_margin > (self.get_equity() * PAPER_MAX_POSITION_PCT / 100):
            return False, f"🛑 POSITION SIZE LIMIT. Req Margin ₹{required_margin:.0f}"
        if required_margin > self.get_free_cash():
            return False, f"🛑 INSUFFICIENT MARGIN. Free: ₹{self.get_free_cash():.0f}"
        return True, "OK"

    def open_position(self, symbol: str, side: str, entry_price: float, qty: int, sl_price: float, sl_order_id: str, margin_used: float):
        self.current_position = {
            "symbol": symbol, "side": side, "entry_price": entry_price, "qty": qty,
            "sl_price": sl_price, "sl_order_id": sl_order_id, "entry_time": datetime.now().isoformat(),
            "highest_price": entry_price, "lowest_price": entry_price,
            "brokerage_paid": PAPER_BROKERAGE_PER_ORDER * 2, "margin_used": margin_used
        }
        self.used_margin += margin_used
        self.cash -= margin_used
        self.cash -= PAPER_BROKERAGE_PER_ORDER
        self.save_state()
        logging.info(f"📝 [PAPER] Position Opened: {symbol} {side} {qty} @ {entry_price} | Margin: ₹{margin_used:.0f}")

    def update_unrealized_pnl(self, ltp: float):
        if not self.current_position: 
            self.unrealized_pnl = 0.0; return
        pos = self.current_position
        side, entry, qty = pos["side"], pos["entry_price"], pos["qty"]
        if side == "BUY":
            pnl = (ltp - entry) * qty
            if ltp > pos["highest_price"]: pos["highest_price"] = ltp
        else:
            pnl = (entry - ltp) * qty
            if ltp < pos["lowest_price"]: pos["lowest_price"] = ltp
        self.unrealized_pnl = pnl
        dd = self.daily_start_equity - self.get_equity()
        if dd > self.max_drawdown_today: self.max_drawdown_today = dd

    def modify_sl(self, new_sl_price: float):
        if self.current_position:
            self.current_position["sl_price"] = new_sl_price
            self.save_state()

    def close_position(self, exit_price: float, exit_reason: str = "SL HIT") -> Dict:
        if not self.current_position: return {}
        pos = self.current_position
        side, entry, qty = pos["side"], pos["entry_price"], pos["qty"]
        if side == "BUY": gross_pnl = (exit_price - entry) * qty
        else: gross_pnl = (entry - exit_price) * qty
        
        total_brokerage = pos["brokerage_paid"] + PAPER_BROKERAGE_PER_ORDER
        slippage_cost = (entry + exit_price) * qty * (PAPER_SLIPPAGE_PCT / 100)
        net_pnl = gross_pnl - total_brokerage - slippage_cost
        
        self.realized_pnl += net_pnl
        self.cash += pos["margin_used"]
        self.cash += net_pnl
        self.used_margin -= pos["margin_used"]
        self.trade_count += 1
        if net_pnl > 0: self.wins += 1
        else: self.losses += 1
        
        trade_log = {
            "Date": date.today().isoformat(), "EntryTime": pos["entry_time"], "ExitTime": datetime.now().isoformat(),
            "Symbol": pos["symbol"], "Side": side, "Qty": qty, "Entry": round(entry, 2), "Exit": round(exit_price, 2),
            "GrossPnL": round(gross_pnl, 2), "Brokerage": round(total_brokerage, 2),
            "Slippage": round(slippage_cost, 2), "NetPnL": round(net_pnl, 2),
            "Reason": exit_reason, "EquityAfter": round(self.get_equity(), 2)
        }
        self.append_trade_log(trade_log)
        self.current_position = None
        self.unrealized_pnl = 0.0
        self.save_state()
        logging.info(f"📝 [PAPER] Closed: {pos['symbol']} | Net: ₹{net_pnl:.2f} | Reason: {exit_reason} | Equity: ₹{self.get_equity():.2f}")
        return trade_log

    def force_square_off(self, ltp: float):
        if self.current_position:
            logging.warning("📝 [PAPER] EOD FORCE SQUARE OFF")
            self.close_position(ltp, "EOD SQUARE OFF")

    def save_state(self):
        state = { "cash": self.cash, "realized_pnl": self.realized_pnl, "unrealized_pnl": self.unrealized_pnl,
            "used_margin": self.used_margin, "daily_start_equity": self.daily_start_equity,
            "max_drawdown_today": self.max_drawdown_today, "trade_count": self.trade_count,
            "wins": self.wins, "losses": self.losses, "last_save_date": self.last_save_date,
            "current_position": self.current_position }
        with open(PAPER_DATA_FILE, "w") as f: json.dump(state, f, default=str)

    def load_state(self):
        if os.path.exists(PAPER_DATA_FILE):
            try:
                with open(PAPER_DATA_FILE, "r") as f: state = json.load(f)
                self.cash = state.get("cash", self.starting_capital)
                self.realized_pnl = state.get("realized_pnl", 0.0)
                self.unrealized_pnl = state.get("unrealized_pnl", 0.0)
                self.used_margin = state.get("used_margin", 0.0)
                self.daily_start_equity = state.get("daily_start_equity", self.starting_capital)
                self.max_drawdown_today = state.get("max_drawdown_today", 0.0)
                self.trade_count = state.get("trade_count", 0)
                self.wins = state.get("wins", 0)
                self.losses = state.get("losses", 0)
                self.last_save_date = state.get("last_save_date", date.today().isoformat())
                self.current_position = state.get("current_position")
                logging.info("📝 Paper State Loaded Successfully.")
            except Exception as e: logging.error(f"Paper State Load Failed: {e}. Starting Fresh.")

    def append_trade_log(self, trade: Dict):
        df = pd.DataFrame([trade])
        header = not os.path.exists(PAPER_TRADES_LOG)
        df.to_csv(PAPER_TRADES_LOG, mode='a', header=header, index=False)

    def log_daily_pnl(self, day: str, pnl: float):
        equity = self.daily_start_equity + pnl
        row = {"Date": day, "StartEquity": round(self.daily_start_equity, 2), "EndEquity": round(equity, 2), 
               "DailyPnL": round(pnl, 2), "Trades": self.trade_count, "Wins": self.wins, "Losses": self.losses}
        df = pd.DataFrame([row])
        header = not os.path.exists(PAPER_DAILY_PNL_FILE)
        df.to_csv(PAPER_DAILY_PNL_FILE, mode='a', header=header, index=False)

    def get_status_message(self) -> str:
        eq = self.get_equity()
        daily_pnl = self.get_daily_pnl()
        daily_pct = (daily_pnl / self.daily_start_equity) * 100 if self.daily_start_equity else 0
        win_rate = (self.wins / self.trade_count * 100) if self.trade_count > 0 else 0
        return (f"📊 *PAPER ACCOUNT STATUS*\n"
                f"💰 Equity: ₹{eq:,.2f} (Start: ₹{self.starting_capital:,.0f})\n"
                f"📈 Daily PnL: ₹{daily_pnl:,.2f} ({daily_pct:+.2f}%)\n"
                f"💵 Free Cash: ₹{self.get_free_cash():,.2f} | Margin Used: ₹{self.used_margin:,.2f}\n"
                f"📝 Trades: {self.trade_count} | Win Rate: {win_rate:.1f}% (W:{self.wins}/L:{self.losses})")

class PaperEngine(ExecutionEngine):
    def __init__(self, groww_client):
        self.groww = groww_client
        self.account = PaperAccount()
        self.order_counter = 0
        logging.info("📝 PROFESSIONAL PAPER TRADING ENGINE INITIALIZED")

    def _gen_id(self): 
        self.order_counter += 1
        return f"PAPER_{int(time.time())}_{self.order_counter}"

    def _estimate_margin(self, price: float, qty: int) -> float:
        return price * qty * 0.15 

    def place_entry_order(self, trading_symbol: str, qty: int, side: str) -> Tuple[bool, float, str]:
        try:
            opt_data = self.groww.get_historical_candles(
                exchange=self.groww.EXCHANGE_NSE, segment=self.groww.SEGMENT_FNO, groww_symbol=trading_symbol,
                start_time=(datetime.now() - timedelta(minutes=2)).strftime("%Y-%m-%d %H:%M:%S"),
                end_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                candle_interval=self.groww.CANDLE_INTERVAL_MIN_1
            )
            ltp = opt_data['candles'][-1][4]
        except Exception as e:
            logging.error(f"Paper LTP Fetch Failed: {e}"); return False, 0.0, ""

        slip = ltp * (PAPER_SLIPPAGE_PCT / 100)
        fill_price = ltp + slip if side == "BUY" else ltp - slip
        fill_price = round(fill_price * 20) / 20 # Tick size 0.05

        est_margin = self._estimate_margin(fill_price, qty)
        can, reason = self.account.can_trade(est_margin)
        if not can:
            send_telegram_alert(f"📝 *PAPER ENTRY BLOCKED*\n`{trading_symbol}`\nReason: {reason}")
            return False, 0.0, ""

        oid = self._gen_id()
        logging.info(f"📝 [PAPER] ENTRY FILLED {side} {qty} {trading_symbol} @ ₹{fill_price} (LTP: ₹{ltp})")
        send_telegram_alert(f"🟢 *PAPER ENTRY FILLED*\n`{trading_symbol}`\nQty: {qty} | Side: {side}\nPrice: ₹{fill_price}")
        return True, fill_price, oid

    def place_sl_order(self, trading_symbol: str, qty: int, trigger_price: float, side: str) -> Tuple[bool, str]:
        trigger_price = round(trigger_price * 20) / 20
        sl_id = self._gen_id()
        logging.info(f"📝 [PAPER] SL PLACED {trading_symbol} Trigger: ₹{trigger_price} | ID: {sl_id}")
        send_telegram_alert(f"🛡️ *PAPER SL PLACED*\n`{trading_symbol}`\nTrigger: ₹{trigger_price}")
        return True, sl_id

    def modify_sl_order(self, order_id: str, new_trigger_price: float) -> bool:
        new_trigger_price = round(new_trigger_price * 20) / 20
        if self.account.current_position: self.account.modify_sl(new_trigger_price)
        logging.info(f"📝 [PAPER] SL MODIFIED {order_id} -> ₹{new_trigger_price}")
        send_telegram_alert(f"🔄 *PAPER TSL UPDATED*\nNew Trigger: ₹{new_trigger_price}")
        return True

    def cancel_order(self, order_id: str) -> bool:
        logging.info(f"📝 [PAPER] ORDER CANCELLED {order_id}"); return True

    def check_position_closed(self, trading_symbol: str) -> bool:
        if not self.account.current_position: return True
        pos = self.account.current_position
        if pos['symbol'] != trading_symbol: return True
        try:
            opt_data = self.groww.get_historical_candles(
                exchange=self.groww.EXCHANGE_NSE, segment=self.groww.SEGMENT_FNO, groww_symbol=trading_symbol,
                start_time=(datetime.now() - timedelta(minutes=2)).strftime("%Y-%m-%d %H:%M:%S"),
                end_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                candle_interval=self.groww.CANDLE_INTERVAL_MIN_1
            )
            ltp = opt_data['candles'][-1][4]
            self.account.update_unrealized_pnl(ltp)
            sl = pos['sl_price']; side = pos['side']
            hit = (side == "BUY" and ltp <= sl) or (side == "SELL" and ltp >= sl)
            if hit: logging.warning(f"📝 [PAPER] SL TRIGGERED! LTP: {ltp} <= SL: {sl}")
            return hit
        except Exception as e: logging.error(f"Paper SL Check Error: {e}"); return False

    def get_ltp(self, trading_symbol: str) -> Optional[float]:
        try:
            opt_data = self.groww.get_historical_candles(
                exchange=self.groww.EXCHANGE_NSE, segment=self.groww.SEGMENT_FNO, groww_symbol=trading_symbol,
                start_time=(datetime.now() - timedelta(minutes=2)).strftime("%Y-%m-%d %H:%M:%S"),
                end_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                candle_interval=self.groww.CANDLE_INTERVAL_MIN_1
            )
            return opt_data['candles'][-1][4]
        except: return None

    def square_off_all(self):
        if self.account.current_position:
            pos = self.account.current_position
            ltp = self.get_ltp(pos['symbol'])
            if ltp: self.account.force_square_off(ltp)
            else: logging.error("Paper Square Off Failed: Could not fetch LTP")

# ==========================================
# 5. LIVE ENGINE
# ==========================================
class LiveEngine(ExecutionEngine):
    def __init__(self, groww):
        self.groww = groww
        self.active_sl_order_id = None
        self.active_symbol = None
        self.active_side = None
        logging.info("🔴 LIVE TRADING ENGINE INITIALIZED - REAL MONEY AT RISK")

    def _data_to_trading_symbol(self, data_symbol: str) -> str:
        try:
            parts = data_symbol.split('-')
            if len(parts) == 5: return f"NIFTY{parts[2].upper()}{parts[3]}{parts[4]}"
            return data_symbol.replace("-", "").replace("NSE", "").replace("FUT", "")
        except: return data_symbol

    def place_entry_order(self, trading_symbol: str, qty: int, side: str) -> Tuple[bool, float, str]:
        try:
            ts = self._data_to_trading_symbol(trading_symbol)
            logging.info(f"🔴 [LIVE] PLACING ENTRY: {side} {qty} {ts} (MKT)")
            resp = self.groww.place_order(trading_symbol=ts, exchange=self.groww.EXCHANGE_NSE, segment=self.groww.SEGMENT_FNO, side=side, quantity=qty, order_type=self.groww.ORDER_TYPE_MARKET, product_type=self.groww.PRODUCT_TYPE_MIS, validity=self.groww.VALIDITY_DAY)
            if resp.get("success") and resp.get("order_id"):
                oid = resp["order_id"]; time.sleep(0.5)
                trades = self.groww.get_order_history(order_id=oid)
                avg_price = 0.0
                if trades and trades.get("data"):
                    fills = [t for t in trades["data"] if t.get("status") == "complete"]
                    if fills:
                        total_qty = sum(f["filled_quantity"] for f in fills)
                        total_val = sum(f["filled_quantity"] * f["average_price"] for f in fills)
                        avg_price = total_val / total_qty if total_qty > 0 else 0
                logging.info(f"🔴 [LIVE] ENTRY FILLED: {ts} @ Avg ₹{avg_price} | OrderID: {oid}")
                send_telegram_alert(f"🟢 *LIVE ENTRY FILLED*\n`{ts}`\nQty: {qty} | Avg: ₹{avg_price:.2f}")
                return True, avg_price, oid
            else:
                err = resp.get("message", "Unknown Error")
                logging.error(f"🔴 [LIVE] ENTRY FAILED: {err}")
                send_telegram_alert(f"🔴 *LIVE ENTRY FAILED*\n`{ts}`\nReason: {err}")
                return False, 0.0, ""
        except Exception as e:
            logging.error(f"🔴 [LIVE] ENTRY EXCEPTION: {e}"); send_telegram_alert(f"🔴 *LIVE EXCEPTION ENTRY*\n{e}"); return False, 0.0, ""

    def place_sl_order(self, trading_symbol: str, qty: int, trigger_price: float, side: str) -> Tuple[bool, str]:
        try:
            ts = self._data_to_trading_symbol(trading_symbol)
            sl_side = self.groww.TRANSACTION_TYPE_SELL if side == "BUY" else self.groww.TRANSACTION_TYPE_BUY
            trigger_price = round(trigger_price * 20) / 20
            logging.info(f"🔴 [LIVE] PLACING SL-M: {sl_side} {qty} {ts} @ Trigger ₹{trigger_price}")
            resp = self.groww.place_order(trading_symbol=ts, exchange=self.groww.EXCHANGE_NSE, segment=self.groww.SEGMENT_FNO, side=sl_side, quantity=qty, order_type=self.groww.ORDER_TYPE_SL_M, product_type=self.groww.PRODUCT_TYPE_MIS, validity=self.groww.VALIDITY_DAY, trigger_price=trigger_price)
            if resp.get("success") and resp.get("order_id"):
                oid = resp["order_id"]; self.active_sl_order_id = oid; self.active_symbol = trading_symbol; self.active_side = side
                logging.info(f"🔴 [LIVE] SL PLACED SUCCESS: OrderID {oid}")
                send_telegram_alert(f"🛡️ *LIVE SL PLACED*\n`{ts}`\nTrigger: ₹{trigger_price}\nOrderID: `{oid}`")
                return True, oid
            else:
                err = resp.get("message", "Unknown Error"); logging.error(f"🔴 [LIVE] SL PLACE FAILED: {err}"); send_telegram_alert(f"🔴 *LIVE SL FAILED*\n`{ts}`\n{err}"); return False, ""
        except Exception as e: logging.error(f"🔴 [LIVE] SL EXCEPTION: {e}"); return False, ""

    def modify_sl_order(self, order_id: str, new_trigger_price: float) -> bool:
        try:
            new_trigger_price = round(new_trigger_price * 20) / 20
            logging.info(f"🔴 [LIVE] MODIFYING SL {order_id} -> Trigger ₹{new_trigger_price}")
            resp = self.groww.modify_order(order_id=order_id, trigger_price=new_trigger_price, order_type=self.groww.ORDER_TYPE_SL_M)
            if resp.get("success"):
                logging.info(f"🔴 [LIVE] SL MODIFIED SUCCESS"); send_telegram_alert(f"🔄 *LIVE TSL UPDATED*\nNew Trigger: ₹{new_trigger_price}"); return True
            else:
                err = resp.get("message", "Modify Failed"); logging.error(f"🔴 [LIVE] SL MODIFY FAILED: {err}"); send_telegram_alert(f"⚠️ *LIVE TSL MODIFY FAILED*\nOrder: `{order_id}`\nError: {err}\n**MANUAL CHECK REQUIRED**"); return False
        except Exception as e: logging.error(f"🔴 [LIVE] SL MODIFY EXCEPTION: {e}"); return False

    def cancel_order(self, order_id: str) -> bool:
        try: return self.groww.cancel_order(order_id=order_id).get("success", False)
        except: return False

    def check_position_closed(self, trading_symbol: str) -> bool:
        try:
            positions = self.groww.get_positions()
            if positions and positions.get("data"):
                ts = self._data_to_trading_symbol(trading_symbol)
                for pos in positions["data"]:
                    if pos.get("trading_symbol") == ts and pos.get("net_quantity", 0) != 0: return False
            return True
        except: return True
    def get_ltp(self, trading_symbol: str) -> Optional[float]: return None

    def square_off_all(self):
        if self.active_symbol:
            logging.info(f"🔴 [LIVE] EOD SQUARE OFF: {self.active_symbol}"); send_telegram_alert(f"🏁 *LIVE EOD SQUARE OFF* `{self.active_symbol}`")
            if self.active_sl_order_id: self.cancel_order(self.active_sl_order_id); time.sleep(0.2)
            try:
                ts = self._data_to_trading_symbol(self.active_symbol)
                positions = self.groww.get_positions()
                if positions and positions.get("data"):
                    for pos in positions["data"]:
                        if pos.get("trading_symbol") == ts:
                            qty = abs(pos.get("net_quantity", 0))
                            if qty > 0:
                                side = self.groww.TRANSACTION_TYPE_SELL if pos.get("net_quantity") > 0 else self.groww.TRANSACTION_TYPE_BUY
                                self.groww.place_order(trading_symbol=ts, exchange=self.groww.EXCHANGE_NSE, segment=self.groww.SEGMENT_FNO, side=side, quantity=qty, order_type=self.groww.ORDER_TYPE_MARKET, product_type=self.groww.PRODUCT_TYPE_MIS, validity=self.groww.VALIDITY_DAY)
            except Exception as e: logging.error(f"EOD Square Off Error: {e}"); send_telegram_alert(f"🔴 *EOD SQUARE OFF ERROR*\n{e}")
            self.active_sl_order_id = None; self.active_symbol = None; self.active_side = None

# ==========================================
# 6. HELPER & DATA FUNCTIONS
# ==========================================
def login_to_groww():
    try:
        logging.info("Authenticating via TOTP..."); totp = pyotp.TOTP(GROWW_TOTP_SECRET).now()
        access_token = GrowwAPI.get_access_token(api_key=GROWW_API_KEY, totp=totp)
        groww = GrowwAPI(access_token); logging.info("System Online."); return groww
    except Exception as e: logging.error(f"Authentication Failure: {e}"); return None

def get_atm_strike(spot_price): return int(math.floor(spot_price / 50.0) * 50)

def get_nearest_options_expiry(groww, underlying="NIFTY"):
    now = datetime.now()
    try:
        exp_data = groww.get_expiries(exchange=groww.EXCHANGE_NSE, underlying_symbol=underlying, year=now.year, month=now.month)
        expiry_list = exp_data.get("expiries", [])
        upcoming_dates = [datetime.strptime(exp, "%Y-%m-%d").date() for exp in expiry_list if datetime.strptime(exp, "%Y-%m-%d").date() >= now.date()]
        if upcoming_dates: return min(upcoming_dates).strftime("%d%b%y")
    except Exception as e: logging.error(f"Failed to auto-fetch expiry: {e}")
    return "30Jun26"

def fetch_dataframe(groww, symbol, interval, days_back):
    end_time = datetime.now(); start_time = end_time - timedelta(days=days_back)
    data = groww.get_historical_candles(exchange=groww.EXCHANGE_NSE, segment=groww.SEGMENT_FNO, groww_symbol=symbol, start_time=start_time.strftime("%Y-%m-%d %H:%M:%S"), end_time=end_time.strftime("%Y-%m-%d %H:%M:%S"), candle_interval=interval)
    if not data or "candles" not in data or not data["candles"]: return pd.DataFrame()
    raw_candles = data.get("candles", []); clean_candles = [row[:6] for row in raw_candles]
    df = pd.DataFrame(clean_candles, columns=["timestamp", "open", "high", "low", "close", "volume"])
    if not df.empty: df["timestamp"] = pd.to_datetime(df["timestamp"]); df.set_index("timestamp", inplace=True)
    return df

def get_live_vix(groww):
    try:
        data = groww.get_historical_candles(exchange=groww.EXCHANGE_NSE, segment="CASH", groww_symbol="NSE-INDIAVIX", start_time=(datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S"), end_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"), candle_interval=groww.CANDLE_INTERVAL_DAY_1)
        return data.get("candles", [])[-1][4]
    except Exception: return 15.0

# ==========================================
# 7. INSTITUTIONAL SCALPING STRATEGY CORE
# ==========================================

class SignalType(Enum):
    LONG = "LONG"; SHORT = "SHORT"; NONE = "NONE"

@dataclass
class ScalpSignal:
    direction: SignalType; entry_price: float; sl_price: float; tp_price: float
    size: int; risk_per_lot: float; atr_val: float; regime: str; symbol: str
    metadata: dict = field(default_factory=dict)

@dataclass
class ActiveScalpPosition:
    signal: ScalpSignal; symbol: str; entry_time: datetime
    entry_order_id: str; sl_order_id: str; current_sl: float
    highest_r: float = 0.0; be_triggered: bool = False; tsl_active: bool = False

class ScalpingStrategy:
    def __init__(self, groww_client, engine: ExecutionEngine):
        self.groww = groww_client; self.engine = engine
        self.active_position: Optional[ActiveScalpPosition] = None
        self.last_trade_time = datetime.min; self.daily_trade_count = 0
        self._last_reset_date = date.today()
        self.wins = 0; self.losses = 0; self.total_r_multiple = 0.0

    def _reset_daily_counters(self):
        today = date.today()
        if self._last_reset_date != today:
            self.daily_trade_count = 0; self.last_trade_time = datetime.min
            self._last_reset_date = today; self.wins = 0; self.losses = 0; self.total_r_multiple = 0.0
            logging.info(f"🔄 [SCALPER] Daily Counters Reset for {today}")

    def _analyze_market_state(self, df: pd.DataFrame, symbol: str) -> Tuple[bool, str, Dict]:
        meta = {}
        try:
            if len(df) < 50: return False, "Insufficient Data", meta
            last = df.iloc[-1]
            spread_pct = (last['high'] - last['low']) / last['close'] * 100 if last['close'] > 0 else 99
            meta['spread_pct'] = spread_pct
            if spread_pct > SCALP_SPREAD_THRESHOLD_PCT: return False, f"Spread Wide: {spread_pct:.2f}%", meta

            df.ta.atr(length=SCALP_ATR_PERIOD, append=True)
            df.ta.adx(length=14, append=True)
            df.ta.bbands(length=SCALP_BB_PERIOD, std=SCALP_BB_STD, append=True)
            df.ta.rsi(length=SCALP_RSI_PERIOD, append=True)
            df['vol_sma'] = df['volume'].rolling(20).mean()

            last = df.iloc[-1]; prev = df.iloc[-2]
            atr = last.get(ATR_COL); adx = last.get(ADX_COL); rsi = last.get(RSI_COL)
            bb_upper = last.get(BB_U_COL); bb_lower = last.get(BB_L_COL); bb_mid = last.get(BB_M_COL)
            vol_sma = last.get('vol_sma')
            vol_ratio = (last['volume'] / vol_sma) if vol_sma and vol_sma > 0 else 0
            
            meta.update({'atr': atr, 'adx': adx, 'rsi': rsi, 'vol_ratio': vol_ratio,
                         'bb_upper': bb_upper, 'bb_lower': bb_lower, 'bb_mid': bb_mid, 'close': last['close']})

            if any(v is None or np.isnan(v) for v in [atr, adx, rsi, bb_upper, bb_lower]): return False, "NaN Indicators", meta
            if adx > SCALP_ADX_TREND_THRESHOLD: return False, f"Trending (ADX:{adx:.1f})", meta
            if vol_ratio < 0.5: return False, f"Low Vol ({vol_ratio:.1f}x)", meta

            regime = "RANGE_HIGH_VOL" if atr > df[ATR_COL].rolling(50).mean().iloc[-1] else "RANGE_LOW_VOL"
            return True, regime, meta
        except Exception as e: logging.error(f"Market State Error [{symbol}]: {e}"); return False, "Analysis Error", meta

    def _generate_signal(self, df: pd.DataFrame, meta: Dict, symbol: str) -> Optional[ScalpSignal]:
        last = df.iloc[-1]; prev = df.iloc[-2]
        close = last['close']; atr = meta['atr']
        bb_upper_curr = meta['bb_upper']; bb_lower_curr = meta['bb_lower']
        bb_upper_prev = prev.get(BB_U_COL); bb_lower_prev = prev.get(BB_L_COL)
        rsi = meta['rsi']; vol_ratio = meta['vol_ratio']

        if any(v is None for v in [bb_upper_prev, bb_lower_prev]): return None

        sl_dist = atr * 1.0; tp_dist = atr * SCALP_TARGET_R_MULT
        direction = SignalType.NONE; entry_px = sl_px = tp_px = 0.0

        # LONG: Lower Band Pierce & Reclaim
        long_cond = (prev['close'] < bb_lower_prev and last['close'] > bb_lower_curr and last['close'] > prev['high'] and rsi < SCALP_RSI_OB and vol_ratio > SCALP_VOL_SPIKE_MULT)
        # SHORT: Upper Band Pierce & Reject
        short_cond = (prev['close'] > bb_upper_prev and last['close'] < bb_upper_curr and last['close'] < prev['low'] and rsi > SCALP_RSI_OS and vol_ratio > SCALP_VOL_SPIKE_MULT)

        if long_cond:
            direction = SignalType.LONG; entry_px = close; sl_px = round(entry_px - sl_dist, 1); tp_px = round(entry_px + tp_dist, 1)
        elif short_cond:
            direction = SignalType.SHORT; entry_px = close; sl_px = round(entry_px + sl_dist, 1); tp_px = round(entry_px - tp_dist, 1)
        else: return None

        # Kelly / Vol Sizing
        if isinstance(self.engine, PaperEngine): equity = self.engine.account.get_equity()
        else: equity = 1000000.0
        risk_capital = equity * (SCALP_RISK_PER_TRADE_PCT / 100.0)
        risk_per_lot = abs(entry_px - sl_px) * QUANTITY
        if risk_per_lot <= 0: return None
        lots = max(1, int(risk_capital / risk_per_lot)); lots = min(lots, 10)
        final_qty = lots * QUANTITY

        logging.info(f"🎯 [SCALPER] SIGNAL: {direction.value} {symbol} | Entry: {entry_px} SL: {sl_px} TP: {tp_px} | Qty: {final_qty} ({lots}L) | Risk: ₹{abs(entry_px - sl_px) * final_qty:.0f}")
        return ScalpSignal(direction=direction, entry_price=entry_px, sl_price=sl_px, tp_price=tp_px,
            size=final_qty, risk_per_lot=abs(entry_px - sl_px), atr_val=atr, regime=meta.get('regime','UNKNOWN'), symbol=symbol,
            metadata={'rsi': rsi, 'vol_ratio': vol_ratio, 'adx': meta['adx']})

    def run_scan_cycle(self):
        self._reset_daily_counters(); now = datetime.now()
        if self.active_position: self._manage_active_position(now); return
        if self.daily_trade_count >= SCALP_MAX_DAILY_TRADES: return
        if (now - self.last_trade_time).total_seconds() < SCALP_COOLDOWN_SECS: return
        if now.time() > datetime.strptime("15:15:00", "%H:%M:%S").time(): return

        try:
            df_fut = fetch_dataframe(self.groww, FUTURES_SYMBOL, self.groww.CANDLE_INTERVAL_MIN_1, days_back=1)
            if df_fut.empty or len(df_fut) < SCALP_LOOKBACK: return
            spot_price = df_fut.iloc[-1]['close']
            ok, regime, meta_fut = self._analyze_market_state(df_fut, FUTURES_SYMBOL)
            if not ok: return

            atm_strike = get_atm_strike(spot_price)
            expiry = get_nearest_options_expiry(self.groww)
            ce_sym = f"{INDEX_PREFIX}-{expiry}-{atm_strike}-CE"
            pe_sym = f"{INDEX_PREFIX}-{expiry}-{atm_strike}-PE"

            df_ce = fetch_dataframe(self.groww, ce_sym, self.groww.CANDLE_INTERVAL_MIN_1, days_back=1)
            df_pe = fetch_dataframe(self.groww, pe_sym, self.groww.CANDLE_INTERVAL_MIN_1, days_back=1)

            if not df_ce.empty:
                ok_ce, _, meta_ce = self._analyze_market_state(df_ce, ce_sym)
                if ok_ce:
                    sig = self._generate_signal(df_ce, meta_ce, ce_sym)
                    if sig and sig.direction == SignalType.LONG: self._execute_entry(sig); return

            if not df_pe.empty:
                ok_pe, _, meta_pe = self._analyze_market_state(df_pe, pe_sym)
                if ok_pe:
                    sig = self._generate_signal(df_pe, meta_pe, pe_sym)
                    if sig and sig.direction == SignalType.SHORT: self._execute_entry(sig); return
        except Exception as e: logging.error(f"Scalper Scan Error: {e}")

    def _execute_entry(self, signal: ScalpSignal):
        side = "BUY" if signal.direction == SignalType.LONG else "SELL"
        success, fill_price, entry_oid = self.engine.place_entry_order(signal.symbol, signal.size, side)
        if not success: return
        success, sl_oid = self.engine.place_sl_order(signal.symbol, signal.size, signal.sl_price, side)
        if not success: self.engine.square_off_all(); return

        self.active_position = ActiveScalpPosition(signal=signal, symbol=signal.symbol, entry_time=datetime.now(),
            entry_order_id=entry_oid, sl_order_id=sl_oid, current_sl=signal.sl_price)
        self.daily_trade_count += 1
        if isinstance(self.engine, PaperEngine):
            est_margin = self.engine._estimate_margin(fill_price, signal.size)
            self.engine.account.open_position(signal.symbol, side, fill_price, signal.size, signal.sl_price, sl_oid, est_margin)

        send_telegram_alert(f"⚡ *SCALP ENTRY ({signal.regime})*\n`{signal.symbol}` | {side} x{signal.size}\nEntry: ₹{fill_price:.2f} | SL: ₹{signal.sl_price:.2f} (1.0 ATR)\nTP: ₹{signal.tp_price:.2f} ({SCALP_TARGET_R_MULT}R)\nRisk: ₹{signal.risk_per_lot * signal.size:.0f} | ADX: {signal.metadata.get('adx',0):.1f}")

    def _manage_active_position(self, now: datetime):
        pos = self.active_position; sig = pos.signal
        ltp = self.engine.get_ltp(pos.symbol)
        if ltp is None:
            try:
                opt_data = self.groww.get_historical_candles(exchange=self.groww.EXCHANGE_NSE, segment=self.groww.SEGMENT_FNO, groww_symbol=pos.symbol,
                    start_time=(now - timedelta(minutes=2)).strftime("%Y-%m-%d %H:%M:%S"), end_time=now.strftime("%Y-%m-%d %H:%M:%S"), candle_interval=self.groww.CANDLE_INTERVAL_MIN_1)
                ltp = opt_data['candles'][-1][4]
            except: return
        if ltp is None: return

        if isinstance(self.engine, PaperEngine): self.engine.account.update_unrealized_pnl(ltp)

        entry = sig.entry_price; risk_unit = sig.risk_per_lot
        if risk_unit <= 0: return
        if sig.direction == SignalType.LONG:
            current_r = (ltp - entry) / risk_unit
            if ltp <= pos.current_sl: self._on_exit(pos, ltp, "SL HIT"); return
        else:
            current_r = (entry - ltp) / risk_unit
            if ltp >= pos.current_sl: self._on_exit(pos, ltp, "SL HIT"); return
        pos.highest_r = max(pos.highest_r, current_r)

        # 1. TIME STOP
        if (now - pos.entry_time).total_seconds() > SCALP_MAX_HOLD_SECS: self._exit_position(pos, ltp, "TIME STOP (5m)"); return
        # 2. FULL TARGET
        if current_r >= SCALP_TARGET_R_MULT: self._exit_position(pos, ltp, f"TP {SCALP_TARGET_R_MULT}R"); return
        # 3. BREAKEVEN
        if not pos.be_triggered and current_r >= SCALP_BE_TRIGGER_R:
            new_sl = round(entry, 1)
            if (sig.direction == SignalType.LONG and new_sl > pos.current_sl) or (sig.direction == SignalType.SHORT and new_sl < pos.current_sl):
                self.engine.modify_sl_order(pos.sl_order_id, new_sl); pos.current_sl = new_sl; pos.be_triggered = True
                send_telegram_alert(f"🔒 *BREAKEVEN* `{pos.symbol}` SL -> ₹{new_sl}")
        # 4. TRAILING STOP
        if not pos.tsl_active and current_r >= SCALP_TSL_TRIGGER_R:
            pos.tsl_active = True; send_telegram_alert(f"📈 *TSL ACTIVE* `{pos.symbol}` @ {current_r:.1f}R")
        if pos.tsl_active:
            trail_dist = risk_unit * SCALP_TSL_STEP_R
            if sig.direction == SignalType.LONG:
                highest_px = entry + pos.highest_r * risk_unit; new_sl = round(highest_px - trail_dist, 1)
                if new_sl > pos.current_sl: self.engine.modify_sl_order(pos.sl_order_id, new_sl); pos.current_sl = new_sl
            else:
                lowest_px = entry - pos.highest_r * risk_unit; new_sl = round(lowest_px + trail_dist, 1)
                if new_sl < pos.current_sl: self.engine.modify_sl_order(pos.sl_order_id, new_sl); pos.current_sl = new_sl

    def _exit_position(self, pos: ActiveScalpPosition, exit_price: float, reason: str):
        logging.info(f"🚀 [SCALPER] EXIT {pos.symbol} @ {exit_price} | Reason: {reason}")
        send_telegram_alert(f"🏁 *SCALP EXIT* `{pos.symbol}`\nExit: ₹{exit_price:.2f} | Reason: {reason}")
        self.engine.cancel_order(pos.sl_order_id)
        side = "SELL" if pos.signal.direction == SignalType.LONG else "BUY"
        self.engine.place_entry_order(pos.symbol, pos.signal.size, side)
        if isinstance(self.engine, PaperEngine):
            trade_log = self.engine.account.close_position(exit_price, reason)
            if trade_log:
                r_mult = trade_log['NetPnL'] / (pos.signal.risk_per_lot * pos.signal.size) if pos.signal.risk_per_lot > 0 else 0
                self.total_r_multiple += r_mult
                if trade_log['NetPnL'] > 0: self.wins += 1
                else: self.losses += 1
                send_telegram_alert(f"{'🟢' if trade_log['NetPnL']>0 else '🔴'} *SCALP CLOSED*\nNet: ₹{trade_log['NetPnL']:.0f} ({r_mult:.2f}R)\nSession R: {self.total_r_multiple:.2f} | W:{self.wins} L:{self.losses}")
        self.active_position = None; self.last_trade_time = datetime.now()

    def _on_exit(self, pos, price, reason): self._exit_position(pos, price, reason)

    def force_eod_close(self):
        if self.active_position:
            ltp = self.engine.get_ltp(self.active_position.symbol)
            if ltp: self._exit_position(self.active_position, ltp, "EOD FORCE CLOSE")
            else:
                self.engine.cancel_order(self.active_position.sl_order_id)
                side = "SELL" if self.active_position.signal.direction == SignalType.LONG else "BUY"
                self.engine.place_entry_order(self.active_position.symbol, self.active_position.signal.size, side)
                self.active_position = None

# ==========================================
# 8. MAIN EXECUTION LOOP
# ==========================================
if __name__ == "__main__":
    groww_client = login_to_groww()
    if not groww_client: logging.error("Startup failed: Authentication Issue."); exit()

    if TRADING_MODE == "LIVE":
        engine = LiveEngine(groww_client)
        send_telegram_alert(f"🤖 *Ivan AlgoBot ONLINE (LIVE SCALPING)*\n⚠️ **REAL MONEY**\nRegime: Mean Reversion | Risk: 0.5%/Trade | Max 15 Trades/Day")
        logging.warning("!!! LIVE SCALPING MODE ACTIVE !!!")
    else:
        engine = PaperEngine(groww_client)
        send_telegram_alert(f"🤖 *Ivan AlgoBot ONLINE (PAPER SCALPING)*\n💰 Capital: ₹{PAPER_STARTING_CAPITAL:,.0f}\n📊 Daily Loss Limit: {PAPER_MAX_DAILY_LOSS_PCT}%\n⚡ Scalping Engine: ACTIVE (5s Heartbeat)")
        logging.info("Trading Mode: PAPER SCALPING")

    scalper = ScalpingStrategy(groww_client, engine)
    tg_thread = threading.Thread(target=telegram_background_worker, daemon=True); tg_thread.start()
    logging.info("Background Telegram worker activated.")
    
    while True:
        now = datetime.now(); current_time = now.time(); current_day = now.weekday()
        market_open = datetime.strptime("09:15:00", "%H:%M:%S").time()
        market_close = datetime.strptime("15:30:00", "%H:%M:%S").time()
        eod_cutoff = datetime.strptime("15:20:00", "%H:%M:%S").time()
        is_market_hours = (0 <= current_day <= 4) and (market_open <= current_time <= market_close)
        is_eod = current_time >= eod_cutoff

        if is_market_hours:
            if isinstance(engine, PaperEngine):
                can, reason = engine.account.can_trade(0)
                if not can:
                    logging.warning(f"Risk Halt: {reason}")
                    if not getattr(engine, '_halt_alerted', False):
                        send_telegram_alert(f"🛑 *RISK HALT*\n{reason}"); engine._halt_alerted = True
                    time.sleep(30); continue
                else: engine._halt_alerted = False

            scalper.run_scan_cycle()

            if is_eod:
                logging.info("EOD Cutoff Reached. Flattening...")
                scalper.force_eod_close()
                while datetime.now().time() < market_close: time.sleep(1)
                if isinstance(engine, PaperEngine) and not getattr(engine, '_eod_printed', False):
                    send_telegram_alert(f"🏁 *MARKET CLOSED - EOD SUMMARY*\n{engine.account.get_status_message()}"); engine._eod_printed = True
        else:
            if current_day >= 5 and not getattr(engine, '_weekend_printed', False):
                msg = f"📅 *WEEKEND*"
                if isinstance(engine, PaperEngine): msg += f"\n{engine.account.get_status_message()}"
                send_telegram_alert(msg); engine._weekend_printed = True
            if current_time < datetime.strptime("08:00:00", "%H:%M:%S").time():
                for attr in ['_eod_printed', '_weekend_printed', '_halt_alerted']:
                    if hasattr(engine, attr): delattr(engine, attr)
            logging.info("Market Closed. Resting 30s..."); time.sleep(30); continue

        time.sleep(5) # 5 Second Institutional Heartbeat
