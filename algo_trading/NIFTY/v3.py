import json
import os
import time
import math
import logging
import threading
from datetime import datetime, timedelta, date
from abc import ABC, abstractmethod
from typing import Dict, Optional, Tuple, List
import pandas as pd
import pandas_ta
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

# --- STRATEGY PARAMETERS ---
ENABLE_TELEGRAM_ALERTS = False  # Switch to True to resume telegram notifications
INDEX_PREFIX = "NSE-NIFTY"
FUTURES_EXPIRY = "28Jul26" # Used only for Futures Data Feed Symbol
FUTURES_SYMBOL = f"{INDEX_PREFIX}-{FUTURES_EXPIRY}-FUT"
QUANTITY = 65
VIX_SL_MULTIPLIER = 1.2
TSL_ACTIVATION_PTS = 15
TSL_TRAIL_PTS = 10
PRODUCT_TYPE = "MIS"
ORDER_TYPE_ENTRY = "MARKET"
ORDER_TYPE_SL = "SL-M"
SQUARE_OFF_TIME = "15:20:00"

# --- CONSTANTS ---
OPTION_TICK_SIZE = 0.05

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
tg_session = requests.Session()

# ==========================================
# 2. TELEGRAM & VIP DATABASE
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
# 3. UTILITY HELPERS
# ==========================================
def round_to_tick(price: float, tick: float = OPTION_TICK_SIZE) -> float:
    """Rounds price to nearest valid exchange tick size (0.05 for NSE Options)."""
    return round(round(price / tick) * tick, 2)

# ==========================================
# 4. PAPER TRADING ENGINE (FULLY FEATURED)
# ==========================================

class PaperAccount:
    """Manages Virtual Capital, Margin, PnL, and Persistence"""
    
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

    def get_equity(self) -> float:
        return self.cash + self.realized_pnl + self.unrealized_pnl

    def get_free_cash(self) -> float:
        return self.cash - self.used_margin

    def get_daily_pnl(self) -> float:
        return self.get_equity() - self.daily_start_equity

    def _check_new_day(self):
        today = date.today().isoformat()
        if today != self.last_save_date:
            logging.info(f"📅 NEW TRADING DAY DETECTED: {today}")
            self.log_daily_pnl(self.last_save_date, self.get_daily_pnl())
            self.daily_start_equity = self.get_equity()
            self.max_drawdown_today = 0.0
            self.last_save_date = today
            self.save_state()

    def can_trade(self, required_margin: float) -> Tuple[bool, str]:
        self._check_new_day()
        daily_pnl_pct = (self.get_daily_pnl() / self.daily_start_equity) * 100
        if daily_pnl_pct <= -PAPER_MAX_DAILY_LOSS_PCT:
            return False, f"🛑 DAILY LOSS LIMIT HIT ({daily_pnl_pct:.2f}%). Max: {PAPER_MAX_DAILY_LOSS_PCT}%"
        if required_margin > (self.get_equity() * PAPER_MAX_POSITION_PCT / 100):
            return False, f"🛑 POSITION SIZE LIMIT. Required Margin ₹{required_margin:.0f} > {PAPER_MAX_POSITION_PCT}% Equity"
        if required_margin > self.get_free_cash():
            return False, f"🛑 INSUFFICIENT MARGIN. Free: ₹{self.get_free_cash():.0f}, Req: ₹{required_margin:.0f}"
        return True, "OK"

    def open_position(self, symbol: str, side: str, entry_price: float, qty: int, sl_price: float, sl_order_id: str, margin_used: float):
        self.current_position = {
            "symbol": symbol, "side": side, "entry_price": entry_price, 
            "qty": qty, "sl_price": sl_price, "sl_order_id": sl_order_id,
            "entry_time": datetime.now().isoformat(),
            "highest_price": entry_price, "lowest_price": entry_price,
            "brokerage_paid": PAPER_BROKERAGE_PER_ORDER * 2,
            "margin_used": margin_used
        }
        self.used_margin += margin_used
        self.cash -= margin_used
        self.cash -= PAPER_BROKERAGE_PER_ORDER
        self.save_state()
        logging.info(f"📝 [PAPER] Position Opened: {symbol} {side} {qty} @ {entry_price} | Margin Blocked: ₹{margin_used:.0f}")

    def update_unrealized_pnl(self, ltp: float):
        if not self.current_position: 
            self.unrealized_pnl = 0.0
            return
        pos = self.current_position
        side = pos["side"]; entry = pos["entry_price"]; qty = pos["qty"]
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
        side = pos["side"]; entry = pos["entry_price"]; qty = pos["qty"]
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
            "Date": date.today().isoformat(),
            "EntryTime": pos["entry_time"],
            "ExitTime": datetime.now().isoformat(),
            "Symbol": pos["symbol"], "Side": side, "Qty": qty,
            "Entry": round(entry, 2), "Exit": round(exit_price, 2),
            "GrossPnL": round(gross_pnl, 2), "Brokerage": round(total_brokerage, 2),
            "Slippage": round(slippage_cost, 2), "NetPnL": round(net_pnl, 2),
            "Reason": exit_reason, "EquityAfter": round(self.get_equity(), 2)
        }
        self.append_trade_log(trade_log)
        self.current_position = None
        self.unrealized_pnl = 0.0
        self.save_state()
        logging.info(f"📝 [PAPER] Position Closed: {pos['symbol']} | Net PnL: ₹{net_pnl:.2f} | Reason: {exit_reason} | Equity: ₹{self.get_equity():.2f}")
        return trade_log

    def force_square_off(self, ltp: float):
        if self.current_position:
            logging.warning("📝 [PAPER] EOD FORCE SQUARE OFF")
            self.close_position(ltp, "EOD SQUARE OFF")

    def save_state(self):
        state = {
            "cash": self.cash, "realized_pnl": self.realized_pnl, "unrealized_pnl": self.unrealized_pnl,
            "used_margin": self.used_margin, "daily_start_equity": self.daily_start_equity,
            "max_drawdown_today": self.max_drawdown_today, "trade_count": self.trade_count,
            "wins": self.wins, "losses": self.losses, "last_save_date": self.last_save_date,
            "current_position": self.current_position
        }
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
            except Exception as e:
                logging.error(f"Paper State Load Failed: {e}. Starting Fresh.")

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


# ==========================================
# 5. EXECUTION ENGINES (ABSTRACT & IMPLEMENTATIONS)
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
            logging.error(f"Paper LTP Fetch Failed: {e}")
            return False, 0.0, ""

        slip = ltp * (PAPER_SLIPPAGE_PCT / 100)
        fill_price = ltp + slip if side == "BUY" else ltp - slip
        fill_price = round_to_tick(fill_price)

        est_margin = self._estimate_margin(fill_price, qty)
        can, reason = self.account.can_trade(est_margin)
        if not can:
            send_telegram_alert(f"📝 *PAPER ENTRY BLOCKED*\n`{trading_symbol}`\nReason: {reason}")
            return False, 0.0, ""

        oid = self._gen_id()
        logging.info(f"📝 [PAPER] ENTRY FILLED {side} {qty} {trading_symbol} @ ₹{fill_price} (LTP: ₹{ltp}, Slip: ₹{slip:.2f})")
        send_telegram_alert(f"🟢 *PAPER ENTRY FILLED*\n`{trading_symbol}`\nQty: {qty} | Side: {side}\nPrice: ₹{fill_price} (Slippage Applied)")
        return True, fill_price, oid

    def place_sl_order(self, trading_symbol: str, qty: int, trigger_price: float, side: str) -> Tuple[bool, str]:
        trigger_price = round_to_tick(trigger_price)
        sl_id = self._gen_id()
        logging.info(f"📝 [PAPER] SL ORDER PLACED {trading_symbol} Trigger: ₹{trigger_price} | ID: {sl_id}")
        send_telegram_alert(f"🛡️ *PAPER SL PLACED*\n`{trading_symbol}`\nTrigger: ₹{trigger_price}")
        return True, sl_id

    def modify_sl_order(self, order_id: str, new_trigger_price: float) -> bool:
        new_trigger_price = round_to_tick(new_trigger_price)
        if self.account.current_position and self.account.current_position.get("sl_order_id") == order_id:
            self.account.modify_sl(new_trigger_price)
            logging.info(f"📝 [PAPER] SL MODIFIED {order_id} -> ₹{new_trigger_price}")
            return True
        logging.error(f"📝 [PAPER] Modify Failed: Order ID Mismatch ({order_id})")
        return False

    def cancel_order(self, order_id: str) -> bool:
        logging.info(f"📝 [PAPER] ORDER CANCELLED {order_id}")
        return True

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
            
            sl = pos['sl_price']
            side = pos['side']
            hit = (side == "BUY" and ltp <= sl) or (side == "SELL" and ltp >= sl)
            
            if hit:
                logging.warning(f"📝 [PAPER] SL TRIGGERED! LTP: {ltp} <= SL: {sl}")
                return True
        except Exception as e:
            logging.error(f"Paper SL Check Error: {e}")
        return False

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
            if ltp:
                self.account.force_square_off(ltp)
            else:
                logging.error("Paper Square Off Failed: Could not fetch LTP")


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
            if len(parts) == 5:
                return f"NIFTY{parts[2].upper()}{parts[3]}{parts[4]}"
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
            trigger_price = round_to_tick(trigger_price)
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
            new_trigger_price = round_to_tick(new_trigger_price)
            logging.info(f"🔴 [LIVE] MODIFYING SL {order_id} -> Trigger ₹{new_trigger_price}")
            resp = self.groww.modify_order(order_id=order_id, trigger_price=new_trigger_price, order_type=self.groww.ORDER_TYPE_SL_M)
            if resp.get("success"):
                logging.info(f"🔴 [LIVE] SL MODIFIED SUCCESS"); return True
            else:
                err = resp.get("message", "Modify Failed"); logging.error(f"🔴 [LIVE] SL MODIFY FAILED: {err}"); return False
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
        except: return False

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
# 6. AUTOMATIC TRAILING STOP MANAGER (CORE LOGIC)
# ==========================================

class AutoTrailingStopManager:
    """
    Professional Automatic SL/TSL Engine.
    - Calculates Initial SL (VIX Based)
    - Places SL-M Order Automatically (with Retry)
    - Activates TSL after 'activation_pts' profit
    - Trails SL by 'trail_pts' dynamically (with Retry)
    - Handles Tick Rounding, State Persistence (Paper)
    """
    def __init__(self, engine: ExecutionEngine, groww_client, paper_account: Optional[PaperAccount] = None):
        self.engine = engine
        self.groww = groww_client
        self.paper_account = paper_account
        
        self.vix_multiplier = VIX_SL_MULTIPLIER
        self.activation_pts = TSL_ACTIVATION_PTS
        self.trail_pts = TSL_TRAIL_PTS
        self.qty = QUANTITY
        self.square_off_time = datetime.strptime(SQUARE_OFF_TIME, "%H:%M:%S").time()
        
        self.symbol = None
        self.side = None
        self.entry_price = 0.0
        self.initial_sl = 0.0
        self.current_sl = 0.0
        self.extreme_price = 0.0
        self.tsl_active = False
        self.sl_order_id = None
        self.vix_at_entry = 0.0
        self._running = False

    def start(self, symbol: str, side: str, entry_price: float, vix: float):
        self.symbol = symbol
        self.side = side.upper()
        self.entry_price = entry_price
        self.vix_at_entry = vix
        self.extreme_price = entry_price
        self.tsl_active = False
        self._running = True
        
        sl_dist = round(vix * self.vix_multiplier, 1)
        self.initial_sl = self._calc_sl_price(entry_price, sl_dist, self.side)
        self.current_sl = self.initial_sl
        
        logging.info(f"🛡️ [AUTO-TSL] Initialized {symbol} {side} @ {entry_price} | VIX: {vix} | SL Dist: {sl_dist} | SL: {self.current_sl}")
        
        if not self._place_sl_order_with_retry():
            raise ConnectionError("Failed to place Initial Stop Loss Order. Aborting Trade.")
        
        if self.paper_account:
            est_margin = self.engine._estimate_margin(entry_price, self.qty) if hasattr(self.engine, '_estimate_margin') else (entry_price * self.qty * 0.15)
            self.paper_account.open_position(symbol, side, entry_price, self.qty, self.current_sl, self.sl_order_id, est_margin)
            self._restore_paper_state()

        self._monitor_loop()

    def _calc_sl_price(self, ref_price: float, distance_pts: float, side: str) -> float:
        if side == "BUY": return round_to_tick(ref_price - distance_pts)
        else: return round_to_tick(ref_price + distance_pts)

    def calculate_trigger_price(self, ltp: float) -> Optional[float]:
        """CORE AUTOMATIC TRIGGER LOGIC: Returns new trigger price if update needed, else None."""
        if self.side == "BUY":
            if ltp > self.extreme_price: self.extreme_price = ltp
            
            if not self.tsl_active:
                if (self.extreme_price - self.entry_price) >= self.activation_pts:
                    self.tsl_active = True
                    logging.info(f"🔥 [AUTO-TSL] TSL ACTIVATED (Long) | Profit: {self.extreme_price - self.entry_price:.2f}pts")
                    return self._calc_sl_price(self.extreme_price, self.trail_pts, "BUY")
                return None
            
            new_sl = self._calc_sl_price(self.extreme_price, self.trail_pts, "BUY")
            if new_sl > self.current_sl: return new_sl
            return None

        else: # SELL
            if ltp < self.extreme_price: self.extreme_price = ltp
            
            if not self.tsl_active:
                if (self.entry_price - self.extreme_price) >= self.activation_pts:
                    self.tsl_active = True
                    logging.info(f"🔥 [AUTO-TSL] TSL ACTIVATED (Short) | Profit: {self.entry_price - self.extreme_price:.2f}pts")
                    return self._calc_sl_price(self.extreme_price, self.trail_pts, "SELL")
                return None
            
            new_sl = self._calc_sl_price(self.extreme_price, self.trail_pts, "SELL")
            if new_sl < self.current_sl: return new_sl
            return None

    def _place_sl_order_with_retry(self, max_retries=3) -> bool:
        for attempt in range(max_retries):
            sl_side = "SELL" if self.side == "BUY" else "BUY"
            success, oid = self.engine.place_sl_order(self.symbol, self.qty, self.current_sl, sl_side)
            if success:
                self.sl_order_id = oid
                send_telegram_alert(f"🛡️ *AUTO-SL PLACED*\n`{self.symbol}`\nTrigger: ₹{self.current_sl}\nOrderID: `{oid}`")
                return True
            logging.warning(f"SL Place Retry {attempt+1}/{max_retries} failed. Waiting 1s...")
            time.sleep(1)
        return False

    def _modify_sl_with_retry(self, new_trigger: float, max_retries=3) -> bool:
        for attempt in range(max_retries):
            if self.engine.modify_sl_order(self.sl_order_id, new_trigger):
                self.current_sl = new_trigger
                if self.paper_account: self.paper_account.modify_sl(new_trigger)
                send_telegram_alert(f"🔄 *AUTO-TSL UPDATED*\n`{self.symbol}`\nNew Trigger: ₹{new_trigger}")
                return True
            logging.warning(f"SL Modify Retry {attempt+1}/{max_retries} failed for {new_trigger}. Waiting 0.5s...")
            time.sleep(0.5)
        send_telegram_alert(f"🔴 *CRITICAL: TSL MODIFY FAILED*\nSymbol: `{self.symbol}`\nTarget Trigger: ₹{new_trigger}\n**MANUAL INTERVENTION REQUIRED**")
        return False

    def _monitor_loop(self):
        logging.info(f"👁️ [AUTO-TSL] Monitoring Started for {self.symbol}")
        
        while self._running:
            now = datetime.now(); current_time = now.time()

            if current_time >= self.square_off_time:
                logging.info("🏁 [AUTO-TSL] EOD Square Off Time Reached.")
                send_telegram_alert("🏁 *EOD SQUARE OFF TRIGGERED BY AUTO-TSL*")
                self.engine.square_off_all()
                self._running = False
                break

            try:
                opt_data = self.groww.get_historical_candles(
                    exchange=self.groww.EXCHANGE_NSE, segment=self.groww.SEGMENT_FNO, 
                    groww_symbol=self.symbol,
                    start_time=(now - timedelta(minutes=2)).strftime("%Y-%m-%d %H:%M:%S"),
                    end_time=now.strftime("%Y-%m-%d %H:%M:%S"),
                    candle_interval=self.groww.CANDLE_INTERVAL_MIN_1
                )
                ltp = opt_data['candles'][-1][4]
            except Exception as e:
                logging.error(f"[AUTO-TSL] LTP Fetch Error: {e}")
                time.sleep(1)
                continue

            if self.paper_account: self.paper_account.update_unrealized_pnl(ltp)

            if self.engine.check_position_closed(self.symbol):
                logging.info(f"🛑 [AUTO-TSL] Position Closed Detected for {self.symbol} @ ~{ltp}")
                send_telegram_alert(f"🛑 *POSITION CLOSED*\n`{self.symbol}`\nExit ~₹{ltp}\nExtreme: ₹{self.extreme_price}")
                
                if self.paper_account:
                    trade_log = self.paper_account.close_position(ltp, "SL HIT")
                    if trade_log:
                        emoji = "🟢" if trade_log['NetPnL'] > 0 else "🔴"
                        send_telegram_alert(f"{emoji} *TRADE RESULT*\nNet PnL: ₹{trade_log['NetPnL']:.2f}\n{self.paper_account.get_status_message()}")
                self._running = False
                break

            new_trigger = self.calculate_trigger_price(ltp)
            if new_trigger is not None:
                logging.info(f"[AUTO-TSL] Trigger Update: {self.current_sl} -> {new_trigger}")
                self._modify_sl_with_retry(new_trigger)

            if int(time.time()) % 30 == 0:
                upnl = self.paper_account.unrealized_pnl if self.paper_account else "N/A"
                logging.info(f"Tracking {self.symbol} | LTP: {ltp} | SL: {self.current_sl} | Extreme: {self.extreme_price} | TSL: {self.tsl_active} | uPnL: {upnl}")

            time.sleep(0.5)

    def _restore_paper_state(self):
        if self.paper_account and self.paper_account.current_position:
            pos = self.paper_account.current_position
            if self.side == "BUY": self.extreme_price = pos.get("highest_price", self.entry_price)
            else: self.extreme_price = pos.get("lowest_price", self.entry_price)
            
            self.current_sl = pos.get("sl_price", self.initial_sl)
            self.sl_order_id = pos.get("sl_order_id")
            
            if self.side == "BUY" and (self.extreme_price - self.entry_price) >= self.activation_pts: self.tsl_active = True
            elif self.side == "SELL" and (self.entry_price - self.extreme_price) >= self.activation_pts: self.tsl_active = True
            logging.info(f"🔄 [AUTO-TSL] State Restored: Extreme={self.extreme_price}, SL={self.current_sl}, Active={self.tsl_active}")

# ==========================================
# 7. DATA & STRATEGY HELPERS
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
    return "07Jul26"

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

def execute_strategy(groww, engine: ExecutionEngine):
    logging.info("Scanning 1m trend...")
    try:
        df_1m = fetch_dataframe(groww, FUTURES_SYMBOL, interval=groww.CANDLE_INTERVAL_MIN_1, days_back=2)
        if df_1m.empty: return False
        
        df_1m.ta.vwap(append=True)
        df_1m.ta.rsi(length=14, append=True)
        
        rsi_cols = [c for c in df_1m.columns if 'RSI' in c.upper()]
        vwap_cols = [c for c in df_1m.columns if 'VWAP' in c.upper()]
        if not rsi_cols or not vwap_cols: return False
        rsi_col, vwap_col = rsi_cols[0], vwap_cols[0]

        latest = df_1m.iloc[-2]
        close_price = latest['close']
        current_rsi = latest[rsi_col]
        current_vwap = latest[vwap_col]
        current_vix = get_live_vix(groww)
        
        logging.info(f"Spot: {close_price} | VWAP: {current_vwap:.2f} | RSI: {current_rsi:.2f} | VIX: {current_vix:.2f}")
        
        vix_safe = 11 < current_vix < 25
        bull = {"Price > VWAP": close_price > current_vwap, "RSI > 60": current_rsi > 60, "VIX Safe": vix_safe}
        bear = {"Price < VWAP": close_price < current_vwap, "RSI < 40": current_rsi < 40, "VIX Safe": vix_safe}
        
        trade_type = ""; option_suffix = ""; entry_side = ""
        if all(bull.values()): trade_type = "BULLISH BREAKOUT"; option_suffix = "CE"; entry_side = "BUY"
        elif all(bear.values()): trade_type = "BEARISH BREAKDOWN"; option_suffix = "PE"; entry_side = "SELL"
        
        if trade_type:
            atm_strike = get_atm_strike(close_price)
            dynamic_expiry = get_nearest_options_expiry(groww, underlying="NIFTY")
            target_symbol = f"{INDEX_PREFIX}-{dynamic_expiry}-{atm_strike}-{option_suffix}"
            
            alert_msg = f"🚀 *{trade_type} DETECTED*\nMode: **{TRADING_MODE}**\nVIX: {current_vix:.2f}\nExecuting `{target_symbol}`."
            logging.info(alert_msg); send_telegram_alert(alert_msg)
            
            success, fill_price, entry_oid = engine.place_entry_order(target_symbol, QUANTITY, entry_side)
            if not success: return False
            
            paper_acc = engine.account if isinstance(engine, PaperEngine) else None
            tsl_manager = AutoTrailingStopManager(engine, groww, paper_account=paper_acc)
            
            # BLOCKING CALL: Manages entire trade lifecycle
            tsl_manager.start(target_symbol, entry_side, fill_price, current_vix)
            
            return True
        return False
    except Exception as e: 
        logging.error(f"Execution Error: {e}"); send_telegram_alert(f"⚠️ *STRATEGY ERROR*\n`{e}`"); return False

# ==========================================
# 8. MAIN LOOP
# ==========================================
if __name__ == "__main__":
    groww_client = login_to_groww()
    if groww_client:
        if TRADING_MODE == "LIVE":
            engine = LiveEngine(groww_client)
            send_telegram_alert(f"🤖 *Ivan AlgoBot ONLINE (LIVE MODE)*\n⚠️ **REAL MONEY**\nScanning Every 20s...")
            logging.warning("!!! LIVE TRADING MODE ACTIVE !!!")
        else:
            engine = PaperEngine(groww_client)
            send_telegram_alert(f"🤖 *Ivan AlgoBot ONLINE (PAPER MODE)*\n💰 Capital: ₹{PAPER_STARTING_CAPITAL:,.0f}\n📊 Daily Loss Limit: {PAPER_MAX_DAILY_LOSS_PCT}%\n✅ Simulation Active.")
            logging.info("Trading Mode: PAPER (Virtual Money)")

        tg_thread = threading.Thread(target=telegram_background_worker, daemon=True); tg_thread.start()
        logging.info("Background Telegram worker activated.")
        
        # Flags to prevent spamming "Market Closed" messages
        eod_summary_sent = False
        weekend_msg_sent = False
        
        while True:
            now = datetime.now(); current_time = now.time(); current_day = now.weekday()
            market_open = datetime.strptime("09:15:00", "%H:%M:%S").time()
            market_close = datetime.strptime("15:30:00", "%H:%M:%S").time()
            
            is_trading_day = (0 <= current_day <= 4)
            is_market_hours = (market_open <= current_time <= market_close)
            
            if is_trading_day and is_market_hours:
                # Reset daily flags on new day
                eod_summary_sent = False
                weekend_msg_sent = False
                
                # Paper Daily Loss Limit Check
                if isinstance(engine, PaperEngine):
                    can, reason = engine.account.can_trade(0)
                    if not can:
                        logging.warning(reason)
                        time.sleep(60)
                        continue

                trade_executed = execute_strategy(groww_client, engine)
                if trade_executed:
                    logging.info("Trade cycle complete. Pausing strategy for the day.")
                    send_telegram_alert("🛑 *DAILY TRADE COMPLETE*\nNo new entries until next session.")
                    # Wait for market close
                    while datetime.now().time() < market_close: time.sleep(60)
            
            elif current_time > market_close and is_trading_day:
                if not eod_summary_sent:
                    if isinstance(engine, PaperEngine):
                        send_telegram_alert(f"🏁 *MARKET CLOSED - EOD SUMMARY*\n{engine.account.get_status_message()}")
                    else:
                        send_telegram_alert("🏁 *MARKET CLOSED*")
                    eod_summary_sent = True
                logging.info("Market Closed. Resting...")
            
            elif not is_trading_day:
                if not weekend_msg_sent:
                    if isinstance(engine, PaperEngine):
                        send_telegram_alert(f"📅 *WEEKEND* | {engine.account.get_status_message()}")
                    else:
                        send_telegram_alert("📅 *WEEKEND* | Market Closed")
                    weekend_msg_sent = True
                logging.info("Weekend. Resting...")

            time.sleep(20)
    else: 
        logging.error("Startup failed: Authentication Issue.")
