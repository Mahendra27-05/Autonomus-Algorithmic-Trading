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
from scalper import get_atm_strike, get_live_vix, login_to_groww

# ==========================================
# 1. CREDENTIALS & CONFIGURATION (SENSEX / BSE)
# ==========================================
GROWW_API_KEY = "----"       # Groww API TOTP Token
GROWW_TOTP_SECRET = "----"   # Groww 32_STRING Access Token

TELEGRAM_BOT_TOKEN = "----"  # Telegram API Token
TELEGRAM_CHAT_ID = "----"    # Telegram Chat ID
PREMIUM_ACCESS_KEY = "----"  # Access Key for other or public to view 
ENABLE_TELEGRAM_ALERTS = False # Switch to True to resume telegram notifications

# --- TRADING MODE SWITCH ---
TRADING_MODE = "PAPER"  # "PAPER" or "LIVE"

# --- PAPER TRADING CONFIG ---
PAPER_STARTING_CAPITAL = 1000000.0      # ₹10 Lakhs Virtual Cash
PAPER_MAX_DAILY_LOSS_PCT = 5.0         # Stop trading if daily loss > 5%
PAPER_MAX_POSITION_PCT = 20.0          # Max 20% capital per trade (Margin)
PAPER_SLIPPAGE_PCT = 0.05              # 0.05% Slippage per leg
PAPER_BROKERAGE_PER_ORDER = 20.0       # ₹20 per executed order
PAPER_DATA_FILE = "paper_account_sensex.json"      # Separate file for Sensex
PAPER_TRADES_LOG = "paper_trades_log_sensex.csv"
PAPER_DAILY_PNL_FILE = "daily_pnl_sensex.csv"

# --- SENSEX SPECIFIC CONFIG ---
INDEX_PREFIX = "BSE-SENSEX"
UNDERLYING = "SENSEX"
EXCHANGE = "BSE"  # Logic uses groww.EXCHANGE_BSE
STRIKE_STEP = 100                     # Sensex Strike Intervalcls
LOT_SIZE = 10                         # Current Sensex Lot Size
QUANTITY = LOT_SIZE * 1               # Number of Lots (1 Lot = 10 Qty)
# FUTURES_SYMBOL will be dynamically determined
# Futures Expiry will be fetched dynamically, but fallback:
FUTURES_EXPIRY_FALLBACK = "30Jul26" 
# Futures Symbol Format: BSE-SENSEX-<EXPIRY>-FUT
FUTURES_SYMBOL = f"{INDEX_PREFIX}-{FUTURES_EXPIRY_FALLBACK}-FUT"

VIX_SL_MULTIPLIER = 1.2
TSL_ACTIVATION_PTS = 20              # Adjusted for Sensex Premium scale (approx 1.5x Nifty)
TSL_TRAIL_PTS = 15
PRODUCT_TYPE = "MIS"
ORDER_TYPE_ENTRY = "MARKET"
ORDER_TYPE_SL = "SL-M"
SQUARE_OFF_TIME = "15:20:00"

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
tg_session = requests.Session()

# ==========================================
# 2. VIP DATABASE & TELEGRAM
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
                            tg_session.post(reply_url, json={"chat_id": chat_id, "text": "✅ *Access Granted.*\nYou will now receive Ivan AlgoBot (SENSEX) signals.", "parse_mode": "Markdown"})
                            logging.info(f"New VIP verified: {chat_id}")
                        else:
                            tg_session.post(reply_url, json={"chat_id": chat_id, "text": "You are already subscribed.", "parse_mode": "Markdown"})
                    else:
                        tg_session.post(reply_url, json={"chat_id": chat_id, "text": "❌ *Access Denied.* Invalid Key.", "parse_mode": "Markdown"})
    except Exception: pass 
    except Exception as e: logging.error(f"Error checking for new subscribers: {e}")

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
# 3. PAPER TRADING ENGINE (UPDATED FOR SENSEX MARGINS)
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
        logging.info(f"📝 PAPER ACCOUNT LOADED (SENSEX) | Equity: ₹{self.get_equity():.2f} | Daily PnL: ₹{self.get_daily_pnl():.2f}")

    def get_equity(self) -> float: return self.cash + self.realized_pnl + self.unrealized_pnl
    def get_free_cash(self) -> float: return self.cash - self.used_margin
    def get_daily_pnl(self) -> float: return self.get_equity() - self.daily_start_equity

    def _check_new_day(self):
        today = date.today().isoformat()
        if today != self.last_save_date:
            logging.info(f"📅 NEW TRADING DAY DETECTED: {today}. Previous day was {self.last_save_date}")
            # Log Yesterday's PnL based on its daily_start_equity before resetting
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
            "sl_price": sl_price, "sl_order_id": sl_order_id,
            "entry_time": datetime.now().isoformat(), "highest_price": entry_price, "lowest_price": entry_price,
            "brokerage_paid": PAPER_BROKERAGE_PER_ORDER * 2, "margin_used": margin_used
        }
        self.used_margin += margin_used
        self.cash -= margin_used
        self.cash -= PAPER_BROKERAGE_PER_ORDER
        self.save_state()
        logging.info(f"📝 [PAPER] Position Opened: {symbol} {side} {qty} @ {entry_price} | Margin: ₹{margin_used:.0f}")

    def update_unrealized_pnl(self, ltp: float):
        if not self.current_position: self.unrealized_pnl = 0.0; return
        pos = self.current_position; side = pos["side"]; entry = pos["entry_price"]; qty = pos["qty"]
        if side == "BUY": pnl = (ltp - entry) * qty; pos["highest_price"] = max(pos["highest_price"], ltp)
        else: pnl = (entry - ltp) * qty; pos["lowest_price"] = min(pos["lowest_price"], ltp)
        self.unrealized_pnl = pnl
        dd = self.daily_start_equity - self.get_equity()
        if dd > self.max_drawdown_today: self.max_drawdown_today = dd

    def modify_sl(self, new_sl_price: float):
        if self.current_position: self.current_position["sl_price"] = new_sl_price; self.save_state()

    def close_position(self, exit_price: float, exit_reason: str = "SL HIT") -> Dict:
        if not self.current_position: return {}
        pos = self.current_position; side = pos["side"]; entry = pos["entry_price"]; qty = pos["qty"]
        if side == "BUY": gross_pnl = (exit_price - entry) * qty
        else: gross_pnl = (entry - exit_price) * qty
        total_brokerage = pos["brokerage_paid"] + PAPER_BROKERAGE_PER_ORDER
        slippage_cost = (entry + exit_price) * qty * (PAPER_SLIPPAGE_PCT / 100)
        net_pnl = gross_pnl - total_brokerage - slippage_cost
        self.realized_pnl += net_pnl
        self.cash += pos["margin_used"] + net_pnl
        self.used_margin -= pos["margin_used"]
        self.trade_count += 1
        if net_pnl > 0: self.wins += 1
        else: self.losses += 1
        trade_log = {"Date": date.today().isoformat(), "EntryTime": pos["entry_time"], "ExitTime": datetime.now().isoformat(),
            "Symbol": pos["symbol"], "Side": side, "Qty": qty, "Entry": round(entry, 2), "Exit": round(exit_price, 2),
            "GrossPnL": round(gross_pnl, 2), "Brokerage": round(total_brokerage, 2), "Slippage": round(slippage_cost, 2),
            "NetPnL": round(net_pnl, 2), "Reason": exit_reason, "EquityAfter": round(self.get_equity(), 2)}
        self.append_trade_log(trade_log)
        self.current_position = None; self.unrealized_pnl = 0.0; self.save_state()
        logging.info(f"📝 [PAPER] Closed: {pos['symbol']} | Net: ₹{net_pnl:.2f} | Reason: {exit_reason} | Equity: ₹{self.get_equity():.2f}")
        return trade_log

    def force_square_off(self, ltp: float):
        if self.current_position: logging.warning("📝 [PAPER] EOD FORCE SQUARE OFF"); self.close_position(ltp, "EOD SQUARE OFF")

    def save_state(self):
        state = {"cash": self.cash, "realized_pnl": self.realized_pnl, "unrealized_pnl": self.unrealized_pnl,
            "used_margin": self.used_margin, "daily_start_equity": self.daily_start_equity,
            "max_drawdown_today": self.max_drawdown_today, "trade_count": self.trade_count,
            "wins": self.wins, "losses": self.losses, "last_save_date": self.last_save_date,
            "current_position": self.current_position}
        with open(PAPER_DATA_FILE, "w") as f: json.dump(state, f, default=str)

    def load_state(self):
        if os.path.exists(PAPER_DATA_FILE):
            try:
                with open(PAPER_DATA_FILE, "r") as f: state = json.load(f)
                self.cash = state.get("cash", self.starting_capital); self.realized_pnl = state.get("realized_pnl", 0.0)
                self.unrealized_pnl = state.get("unrealized_pnl", 0.0); self.used_margin = state.get("used_margin", 0.0)
                self.daily_start_equity = state.get("daily_start_equity", self.starting_capital)
                self.max_drawdown_today = state.get("max_drawdown_today", 0.0); self.trade_count = state.get("trade_count", 0)
                self.wins = state.get("wins", 0); self.losses = state.get("losses", 0)
                self.last_save_date = state.get("last_save_date", date.today().isoformat())
                self.current_position = state.get("current_position"); logging.info("📝 Paper State Loaded.")
            except Exception as e: logging.error(f"Paper Load Failed: {e}")

    def append_trade_log(self, trade: Dict):
        df = pd.DataFrame([trade]); header = not os.path.exists(PAPER_TRADES_LOG)
        df.to_csv(PAPER_TRADES_LOG, mode='a', header=header, index=False)

    def log_daily_pnl(self, day: str, pnl: float):
        equity = self.daily_start_equity + pnl
        row = {"Date": day, "StartEquity": round(self.daily_start_equity, 2), "EndEquity": round(equity, 2), 
               "DailyPnL": round(pnl, 2), "Trades": self.trade_count, "Wins": self.wins, "Losses": self.losses}
        df = pd.DataFrame([row]); header = not os.path.exists(PAPER_DAILY_PNL_FILE)
        df.to_csv(PAPER_DAILY_PNL_FILE, mode='a', header=header, index=False)

    def get_status_message(self) -> str:
        eq = self.get_equity(); daily_pnl = self.get_daily_pnl()
        daily_pct = (daily_pnl / self.daily_start_equity) * 100 if self.daily_start_equity else 0
        win_rate = (self.wins / self.trade_count * 100) if self.trade_count > 0 else 0
        return (f"📊 *PAPER SENSEX STATUS*\n💰 Equity: ₹{eq:,.2f}\n📈 Daily PnL: ₹{daily_pnl:,.2f} ({daily_pct:+.2f}%)\n"
                f"💵 Free: ₹{self.get_free_cash():,.2f} | Margin: ₹{self.used_margin:,.2f}\n📝 Trades: {self.trade_count} | WR: {win_rate:.1f}%")

class PaperEngine(ExecutionEngine):
    def __init__(self, groww_client):
        self.groww = groww_client; self.account = PaperAccount(); self.order_counter = 0
        logging.info("📝 PAPER ENGINE INITIALIZED (BSE SENSEX)")

    def _gen_id(self): self.order_counter += 1; return f"PAPER_BSE_{int(time.time())}_{self.order_counter}"

    def _estimate_margin(self, price: float, qty: int) -> float:
        # Sensex MIS Margin approx 15-18% of Notional
        return price * qty * 0.18 

    def _fetch_ltp_internal(self, data_symbol: str) -> Optional[float]:
        # Helper to fetch 1m candle close for Paper LTP
        try:
            opt_data = self.groww.get_historical_candles(
                exchange=self.groww.EXCHANGE_BSE, segment=self.groww.SEGMENT_FNO, groww_symbol=data_symbol,
                start_time=(datetime.now() - timedelta(minutes=2)).strftime("%Y-%m-%d %H:%M:%S"),
                end_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                candle_interval=self.groww.CANDLE_INTERVAL_MIN_1
            )
            return opt_data['candles'][-1][4]
        except Exception as e:
            logging.error(f"Paper LTP Fetch Failed {data_symbol}: {e}"); return None

    def place_entry_order(self, data_symbol: str, qty: int, side: str) -> Tuple[bool, float, str]:
        ltp = self._fetch_ltp_internal(data_symbol)
        if ltp is None: return False, 0.0, ""
        slip = ltp * (PAPER_SLIPPAGE_PCT / 100) # 0.05% slippage
        fill_price = round(ltp + slip if side == "BUY" else ltp - slip, 1) # Tick 0.05 -> 0.1 round
        est_margin = self._estimate_margin(fill_price, qty)
        can, reason = self.account.can_trade(est_margin)
        if not can:
            send_telegram_alert(f"📝 *PAPER ENTRY BLOCKED*\n`{data_symbol}`\n{reason}"); return False, 0.0, ""
        oid = self._gen_id()
        logging.info(f"📝 [PAPER] ENTRY FILLED {side} {qty} {data_symbol} @ ₹{fill_price} (LTP: ₹{ltp})")
        send_telegram_alert(f"🟢 *PAPER ENTRY FILLED (SENSEX)*\n`{data_symbol}`\nQty: {qty} | Side: {side}\nPrice: ₹{fill_price}")
        return True, fill_price, oid

    def place_sl_order(self, data_symbol: str, qty: int, trigger_price: float, side: str) -> Tuple[bool, str]:
        trigger_price = round(trigger_price * 20) / 20 # Tick 0.05
        sl_id = self._gen_id()
        logging.info(f"📝 [PAPER] SL PLACED {data_symbol} Trigger: ₹{trigger_price} | ID: {sl_id}")
        send_telegram_alert(f"🛡️ *PAPER SL PLACED*\n`{data_symbol}`\nTrigger: ₹{trigger_price}")
        return True, sl_id

    def modify_sl_order(self, order_id: str, new_trigger_price: float) -> bool:
        new_trigger_price = round(new_trigger_price * 20) / 20
        if self.account.current_position: self.account.modify_sl(new_trigger_price)
        logging.info(f"📝 [PAPER] SL MODIFIED {order_id} -> ₹{new_trigger_price}")
        send_telegram_alert(f"🔄 *PAPER TSL UPDATED*\nNew Trigger: ₹{new_trigger_price}"); return True

    def cancel_order(self, order_id: str) -> bool: logging.info(f"📝 [PAPER] CANCEL {order_id}"); return True

    def check_position_closed(self, data_symbol: str) -> bool:
        if not self.account.current_position: return True
        pos = self.account.current_position
        if pos['symbol'] != data_symbol: return True
        ltp = self._fetch_ltp_internal(data_symbol)
        if ltp is None: return False
        self.account.update_unrealized_pnl(ltp)
        sl = pos['sl_price']; side = pos['side']
        hit = (side == "BUY" and ltp <= sl) or (side == "SELL" and ltp >= sl)
        if hit: logging.warning(f"📝 [PAPER] SL HIT! LTP: {ltp} vs SL: {sl}")
        return hit

    def get_ltp(self, data_symbol: str) -> Optional[float]: return self._fetch_ltp_internal(data_symbol)

    def square_off_all(self):
        if self.account.current_position:
            pos = self.account.current_position; ltp = self.get_ltp(pos['symbol'])
            if ltp: self.account.force_square_off(ltp)
            else: logging.error("Paper Square Off Failed: No LTP")


# ==========================================
# 4. LIVE ENGINE (BSE SENSEX)
# ==========================================
class LiveEngine(ExecutionEngine):
    def __init__(self, groww):
        self.groww = groww; self.active_sl_order_id = None; self.active_data_symbol = None; self.active_side = None
        logging.info("🔴 LIVE ENGINE INITIALIZED (BSE SENSEX) - REAL MONEY")

    def _data_to_trading_symbol(self, data_symbol: str) -> str:
        """
        Converts Groww Data Symbol -> Trading Symbol for Order Placement.
        Data: 'BSE-SENSEX-27Jun26-75000-CE'
        Trading: 'SENSEX26JUN2775000CE' (Format: SENSEX<YY><MON><DD><STRIKE><CE/PE>)
        """
        try:
            parts = data_symbol.split('-')
            # Parts: ['BSE', 'SENSEX', '27Jun26', '75000', 'CE']
            if len(parts) == 5:
                expiry_str = parts[2]      # '27Jun26'
                strike = parts[3]          # '75000'
                opt_type = parts[4]        # 'CE'/'PE'
                
                # Parse Expiry '27Jun26' -> Day=27, Mon=Jun, Year=26
                # Target Format: YYMONDD (e.g., 26JUN27)
                dt = datetime.strptime(expiry_str, "%d%b%y")
                expiry_fmt = dt.strftime("%y%b%d").upper() # 26JUN27
                
                return f"SENSEX{expiry_fmt}{strike}{opt_type}"
            return data_symbol.replace("-", "").replace("BSE", "").replace("SENSEX", "")
        except Exception as e:
            logging.error(f"Symbol Conversion Failed: {data_symbol} | {e}")
            return data_symbol

    def place_entry_order(self, data_symbol: str, qty: int, side: str) -> Tuple[bool, float, str]:
        try:
            ts = self._data_to_trading_symbol(data_symbol)
            logging.info(f"🔴 [LIVE] ENTRY: {side} {qty} {ts} (MKT)")
            resp = self.groww.place_order(
                trading_symbol=ts, exchange=self.groww.EXCHANGE_BSE, segment=self.groww.SEGMENT_FNO, 
                side=side, quantity=qty, order_type=self.groww.ORDER_TYPE_MARKET, 
                product_type=self.groww.PRODUCT_TYPE_MIS, validity=self.groww.VALIDITY_DAY
            )
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
                logging.info(f"🔴 [LIVE] FILLED: {ts} @ ₹{avg_price} | ID: {oid}")
                send_telegram_alert(f"🟢 *LIVE ENTRY FILLED (SENSEX)*\n`{ts}`\nQty: {qty} | Avg: ₹{avg_price:.2f}")
                return True, avg_price, oid
            else:
                err = resp.get("message", "Unknown"); logging.error(f"🔴 ENTRY FAIL: {err}"); send_telegram_alert(f"🔴 *LIVE ENTRY FAILED*\n`{ts}`\n{err}"); return False, 0.0, ""
        except Exception as e: logging.error(f"🔴 ENTRY EXC: {e}"); send_telegram_alert(f"🔴 *LIVE EXC ENTRY*\n{e}"); return False, 0.0, ""

    def place_sl_order(self, data_symbol: str, qty: int, trigger_price: float, side: str) -> Tuple[bool, str]:
        try:
            ts = self._data_to_trading_symbol(data_symbol)
            sl_side = self.groww.TRANSACTION_TYPE_SELL if side == "BUY" else self.groww.TRANSACTION_TYPE_BUY
            trigger_price = round(trigger_price * 20) / 20
            logging.info(f"🔴 [LIVE] SL-M: {sl_side} {qty} {ts} @ Trig ₹{trigger_price}")
            resp = self.groww.place_order(
                trading_symbol=ts, exchange=self.groww.EXCHANGE_BSE, segment=self.groww.SEGMENT_FNO, 
                side=sl_side, quantity=qty, order_type=self.groww.ORDER_TYPE_SL_M, 
                product_type=self.groww.PRODUCT_TYPE_MIS, validity=self.groww.VALIDITY_DAY, 
                trigger_price=trigger_price
            )
            if resp.get("success") and resp.get("order_id"):
                oid = resp["order_id"]; self.active_sl_order_id = oid; self.active_data_symbol = data_symbol; self.active_side = side
                logging.info(f"🔴 [LIVE] SL PLACED: {oid}"); send_telegram_alert(f"🛡️ *LIVE SL PLACED*\n`{ts}`\nTrig: ₹{trigger_price}\nID: `{oid}`"); return True, oid
            else: err = resp.get("message", "Fail"); logging.error(f"🔴 SL FAIL: {err}"); send_telegram_alert(f"🔴 *LIVE SL FAILED*\n`{ts}`\n{err}"); return False, ""
        except Exception as e: logging.error(f"🔴 SL EXC: {e}"); return False, ""

    def modify_sl_order(self, order_id: str, new_trigger_price: float) -> bool:
        try:
            new_trigger_price = round(new_trigger_price * 20) / 20
            logging.info(f"🔴 [LIVE] MOD SL {order_id} -> ₹{new_trigger_price}")
            resp = self.groww.modify_order(order_id=order_id, trigger_price=new_trigger_price, order_type=self.groww.ORDER_TYPE_SL_M)
            if resp.get("success"): logging.info("🔴 [LIVE] MOD OK"); send_telegram_alert(f"🔄 *LIVE TSL UPDATED*\nNew Trig: ₹{new_trigger_price}"); return True
            else: err = resp.get("message", "Fail"); logging.error(f"🔴 MOD FAIL: {err}"); send_telegram_alert(f"⚠️ *LIVE TSL FAILED*\n`{order_id}`\n{err}\n**MANUAL CHECK**"); return False
        except Exception as e: logging.error(f"🔴 MOD EXC: {e}"); return False

    def cancel_order(self, order_id: str) -> bool:
        try: return self.groww.cancel_order(order_id=order_id).get("success", False)
        except: return False

    def check_position_closed(self, data_symbol: str) -> bool:
        try:
            positions = self.groww.get_positions()
            if positions and positions.get("data"):
                ts = self._data_to_trading_symbol(data_symbol)
                for pos in positions["data"]:
                    if pos.get("trading_symbol") == ts and pos.get("net_quantity", 0) != 0: return False
            return True
        except: return False

    def get_ltp(self, data_symbol: str) -> Optional[float]: return None # Not used in Live management loop

    def square_off_all(self):
        if self.active_data_symbol:
            logging.info(f"🔴 [LIVE] EOD SQUARE OFF: {self.active_data_symbol}"); send_telegram_alert(f"🏁 *LIVE EOD SQUARE OFF* `{self.active_data_symbol}`")
            if self.active_sl_order_id: self.cancel_order(self.active_sl_order_id); time.sleep(0.2)
            try:
                ts = self._data_to_trading_symbol(self.active_data_symbol)
                positions = self.groww.get_positions()
                if positions and positions.get("data"):
                    for pos in positions["data"]:
                        if pos.get("trading_symbol") == ts:
                            qty = abs(pos.get("net_quantity", 0))
                            if qty > 0:
                                side = self.groww.TRANSACTION_TYPE_SELL if pos.get("net_quantity") > 0 else self.groww.TRANSACTION_TYPE_BUY
                                self.groww.place_order(trading_symbol=ts, exchange=self.groww.EXCHANGE_BSE, segment=self.groww.SEGMENT_FNO, side=side, quantity=qty, order_type=self.groww.ORDER_TYPE_MARKET, product_type=self.groww.PRODUCT_TYPE_MIS, validity=self.groww.VALIDITY_DAY)
            except Exception as e: logging.error(f"EOD Error: {e}"); send_telegram_alert(f"🔴 *EOD ERROR*\n{e}")
            self.active_sl_order_id = None; self.active_data_symbol = None; self.active_side = None

# ==========================================
# 5. HELPER & DATA FUNCTIONS (BSE SENSEX) 
# ==========================================

def get_nearest_options_expiry(groww, underlying=UNDERLYING):
    """
    Fetches expiries for Current Month AND Next Month to find the ACTIVE expiry.
    Returns format: 'DDMMMYY' (e.g., '30Jul26')
    """
    now = datetime.now()
    all_expiries = []
    
    # Check current month and next month (handle Dec->Jan rollover)
    months_to_check = []
    months_to_check.append((now.year, now.month))
    next_month = now.month + 1
    next_year = now.year
    if next_month > 12:
        next_month = 1
        next_year += 1
    months_to_check.append((next_year, next_month))

    for y, m in months_to_check:
        try:
            exp_data = groww.get_expiries(exchange=groww.EXCHANGE_BSE, underlying_symbol=underlying, year=y, month=m)
            expiry_list = exp_data.get("expiries", [])
            # API returns 'YYYY-MM-DD'
            for exp_str in expiry_list:
                try:
                    exp_date = datetime.strptime(exp_str, "%Y-%m-%d").date()
                    if exp_date >= now.date(): # Only future or today
                        all_expiries.append(exp_date)
                except: continue
        except Exception as e:
            logging.warning(f"Expiry fetch failed for {m}/{y}: {e}")

    if not all_expiries:
        logging.error("NO VALID FUTURE EXPIRIES FOUND. Using Fallback.")
        return FUTURES_EXPIRY_FALLBACK

    nearest = min(all_expiries)
    # Format for Data Symbols: DDMMMYY (e.g., 30Jul26)
    return nearest.strftime("%d%b%y")

def get_current_futures_symbol(groww):
    """Dynamically constructs the ACTIVE Monthly Futures Symbol for Sensex"""
    expiry = get_nearest_options_expiry(groww) 
    # Format: BSE-SENSEX-30Jul26-FUT
    symbol = f"{INDEX_PREFIX}-{expiry}-FUT"
    logging.info(f"Active Futures Symbol Resolved: {symbol}")
    return symbol
def fetch_dataframe(groww, symbol, interval, days_back):
    end_time = datetime.now()
    start_time = end_time - timedelta(days=days_back)
    
    try:
        data = groww.get_historical_candles(
            exchange=groww.EXCHANGE_BSE, 
            segment=groww.SEGMENT_FNO, 
            groww_symbol=symbol,
            start_time=start_time.strftime("%Y-%m-%d %H:%M:%S"),
            end_time=end_time.strftime("%Y-%m-%d %H:%M:%S"),
            candle_interval=interval
        )
        
        # --- DEBUG: Log Raw Response if Empty ---
        if not data or "candles" not in data or not data.get("candles"):
            logging.warning(f"API Empty/Invalid Response for {symbol}: {data}")
            return pd.DataFrame()

        raw_candles = data.get("candles", [])
        # Ensure we only take first 6 columns (ts, o, h, l, c, v)
        clean_candles = [row[:6] for row in raw_candles if len(row) >= 6]
        
        if not clean_candles:
            logging.warning(f"No valid candle rows for {symbol}")
            return pd.DataFrame()

        df = pd.DataFrame(clean_candles, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        df.set_index("timestamp", inplace=True)
        # Ensure numeric types
        for col in ['open', 'high', 'low', 'close', 'volume']:
            df[col] = pd.to_numeric(df[col], errors='coerce')
        df.dropna(inplace=True)
        return df

    except Exception as e:
        logging.error(f"Exception in fetch_dataframe for {symbol}: {e}")
        return pd.DataFrame()

# ==========================================
# 6. STRATEGY & TRADE MANAGEMENT
# ==========================================

def manage_dynamic_trade(engine: ExecutionEngine, groww, data_symbol: str, entry_price: float, current_vix: float, side: str):
    initial_sl_points = round(current_vix * VIX_SL_MULTIPLIER, 1)
    if side == "BUY": current_sl = round(entry_price - initial_sl_points, 1)
    else: current_sl = round(entry_price + initial_sl_points, 1)

    highest_price = entry_price; tsl_active = False; sl_order_id = None

    # 1. Place Initial SL
    success, sl_order_id = engine.place_sl_order(data_symbol, QUANTITY, current_sl, side)
    if not success:
        logging.error("CRITICAL: Initial SL Failed."); send_telegram_alert("🔴 *CRITICAL: SL FAILED*")
        engine.square_off_all(); return

    # 2. Register Paper Position
    if isinstance(engine, PaperEngine):
        est_margin = engine._estimate_margin(entry_price, QUANTITY)
        engine.account.open_position(data_symbol, side, entry_price, QUANTITY, current_sl, sl_order_id, est_margin)

    send_telegram_alert(f"🛡️ *ACTIVE SENSEX ({TRADING_MODE})*\nEntry: ₹{entry_price}\nVIX SL: {initial_sl_points} pts\nSL: ₹{current_sl}\nID: `{sl_order_id}`")

    # 3. Monitor Loop
    # NOTE FOR LIVE TRADING: The current implementation uses groww.get_historical_candles
    # for live_premium, which provides 1-minute candle closes, not true real-time LTP.
    # For precise live TSL, a faster LTP source (e.g., groww.get_market_quotes or websockets)
    # and an update to LiveEngine.get_ltp would be required.
    while True:
        now = datetime.now(); current_time = now.time()
        if current_time >= datetime.strptime(SQUARE_OFF_TIME, "%H:%M:%S").time():
            logging.info("EOD Reached."); send_telegram_alert("🏁 *EOD SQUARE OFF*")
            engine.square_off_all(); break

        try:
            opt_data = groww.get_historical_candles(
                exchange=groww.EXCHANGE_BSE, segment=groww.SEGMENT_FNO, groww_symbol=data_symbol,
                start_time=(now - timedelta(minutes=3)).strftime("%Y-%m-%d %H:%M:%S"),
                end_time=now.strftime("%Y-%m-%d %H:%M:%S"),
                candle_interval=groww.CANDLE_INTERVAL_MIN_1
            )
            live_premium = opt_data['candles'][-1][4]

            if isinstance(engine, PaperEngine):
                engine.account.update_unrealized_pnl(live_premium)
                if int(time.time()) % 30 == 0:
                    pos = engine.account.current_position
                    if pos: logging.info(f"Tracking {data_symbol} | LTP: ₹{live_premium} | SL: ₹{current_sl} | uPnL: ₹{engine.account.unrealized_pnl:.2f}")

            if engine.check_position_closed(data_symbol):
                msg = f"🛑 *POSITION CLOSED*\n`{data_symbol}`\nExit ~₹{live_premium}\nHigh: ₹{highest_price}"
                logging.info(msg); send_telegram_alert(msg)
                if isinstance(engine, PaperEngine):
                    log = engine.account.close_position(live_premium, "SL HIT")
                    if log:
                        emoji = "🟢" if log['NetPnL'] > 0 else "🔴"
                        send_telegram_alert(f"{emoji} *TRADE CLOSED*\nNet: ₹{log['NetPnL']:.2f}\n{engine.account.get_status_message()}")
                break

            # TSL Logic
            if side == "BUY":
                if live_premium > highest_price:
                    highest_price = live_premium
                    if highest_price >= (entry_price + TSL_ACTIVATION_PTS):
                        if not tsl_active: send_telegram_alert("🔥 *TSL ACTIVATED*"); tsl_active = True
                        new_sl = round(highest_price - TSL_TRAIL_PTS, 1)
                        if new_sl > current_sl: current_sl = new_sl; engine.modify_sl_order(sl_order_id, current_sl)
            else: # SELL
                if live_premium < highest_price:
                    highest_price = live_premium
                    if (entry_price - highest_price) >= TSL_ACTIVATION_PTS:
                        if not tsl_active: send_telegram_alert("🔥 *TSL ACTIVATED (SHORT)*"); tsl_active = True
                        new_sl = round(highest_price + TSL_TRAIL_PTS, 1)
                        if new_sl < current_sl: current_sl = new_sl; engine.modify_sl_order(sl_order_id, current_sl)

        except Exception as e: logging.error(f"Monitor Err: {e}")
        time.sleep(0.5)

def execute_strategy(groww, engine: ExecutionEngine):
    logging.info("Scanning Sensex 1m Trend...")
    try:
        # 1. Get Dynamic Futures Symbol (Now handles July expiry correctly)
        fut_symbol = get_current_futures_symbol(groww) # This function already returns the full symbol string
        logging.info(f"Using Futures Symbol: {fut_symbol}")

        df_1m = fetch_dataframe(groww, fut_symbol, interval=groww.CANDLE_INTERVAL_MIN_1, days_back=2)
        
        # --- CRITICAL CHECK ---
        if df_1m.empty or len(df_1m) < 20:
            logging.error(f"Insufficient Data for {fut_symbol} (Rows: {len(df_1m)}). Skipping cycle.")
            # Optional: Try fetching Spot/Cash Index 'BSE-SENSEX' as fallback for VWAP/RSI if Futures fail
            # But for now, just skip to avoid errors.
            return False
            
        df_1m.ta.vwap(append=True); df_1m.ta.rsi(length=14, append=True)
        
        # Use .iloc[-2] (Closed Candle) safely
        if len(df_1m) < 2: return False
        latest = df_1m.iloc[-2]
        
        close_price = latest['close']; current_rsi = latest['RSI_14']; current_vwap = latest['VWAP_D']; current_vix = get_live_vix(groww)
        logging.info(f"Sensex Fut: {close_price} | VWAP: {current_vwap:.2f} | RSI: {current_rsi:.2f} | VIX: {current_vix:.2f}")

        vix_safe = 11 < current_vix < 25
        bull = {"Price > VWAP": close_price > current_vwap, "RSI > 60": current_rsi > 60, "VIX Safe": vix_safe}
        bear = {"Price < VWAP": close_price < current_vwap, "RSI < 40": current_rsi < 40, "VIX Safe": vix_safe}

        trade_type = ""; option_suffix = ""; entry_side = ""
        if all(bull.values()): trade_type = "BULLISH BREAKOUT"; option_suffix = "CE"; entry_side = "BUY"
        elif all(bear.values()): trade_type = "BEARISH BREAKDOWN"; option_suffix = "PE"; entry_side = "SELL"

        if trade_type:
            atm_strike = get_atm_strike(close_price)
            expiry = get_nearest_options_expiry(groww)
            target_symbol = f"{INDEX_PREFIX}-{expiry}-{atm_strike}-{option_suffix}"
            
            alert = f"🚀 *{trade_type} (SENSEX 1M)*\nMode: **{TRADING_MODE}**\nFut: ₹{close_price}\nVWAP/RSI Aligned | VIX: {current_vix:.2f}\nTarget: `{target_symbol}`"
            logging.info(alert); send_telegram_alert(alert)
            
            # PaperEngine.place_entry_order should return a valid fill_price.
            success, fill_price, oid = engine.place_entry_order(target_symbol, QUANTITY, entry_side)
            
            if TRADING_MODE == "PAPER" and fill_price == 0.0:
                opt_df = fetch_dataframe(groww, target_symbol, groww.CANDLE_INTERVAL_MIN_1, 1)
                if not opt_df.empty: fill_price = opt_df.iloc[-1]['close']

            manage_dynamic_trade(engine, groww, target_symbol, fill_price, current_vix, entry_side)
            return True
        return False
    except Exception as e: 
        logging.error(f"Exec Error: {e}"); 
        send_telegram_alert(f"⚠️ *STRATEGY ERROR*\n`{e}`"); 
        return False
# ==========================================
# 7. MAIN LOOP
# ==========================================
if __name__ == "__main__":
    groww_client = login_to_groww()
    if groww_client:
        if TRADING_MODE == "LIVE":
            engine = LiveEngine(groww_client)
            send_telegram_alert(f"🤖 *Ivan AlgoBot ONLINE (LIVE SENSEX)*\n⚠️ **REAL MONEY**\nScanning 1m...")
            logging.warning("!!! LIVE TRADING ACTIVE !!!")
        else:
            engine = PaperEngine(groww_client)
            send_telegram_alert(f"🤖 *Ivan AlgoBot ONLINE (PAPER SENSEX)*\n💰 Capital: ₹{PAPER_STARTING_CAPITAL:,.0f}\n📊 Daily Loss Limit: {PAPER_MAX_DAILY_LOSS_PCT}%\n✅ Sim Active.")
            logging.info("Mode: PAPER (SENSEX)")

        tg_thread = threading.Thread(target=telegram_background_worker, daemon=True); tg_thread.start()
        logging.info("Telegram Worker Started.")
        
        # Initialize EOD/Weekend flags in the main scope
        eod_printed = False
        weekend_printed = False
        
        while True:
            now = datetime.now(); current_time = now.time(); current_day = now.weekday()
            market_open = datetime.strptime("09:15:00", "%H:%M:%S").time()
            market_close = datetime.strptime("15:30:00", "%H:%M:%S").time()
            
            # Reset daily flags at the start of a new trading day
            if current_time >= market_open and current_time < market_close and (eod_printed or weekend_printed):
                eod_printed = False
                weekend_printed = False

            if (0 <= current_day <= 4) and (market_open <= current_time <= market_close):
                # Check Daily Loss Limit before scanning (Paper Only)
                if isinstance(engine, PaperEngine):
                    can, reason = engine.account.can_trade(0)
                    if not can:
                        logging.warning(reason); time.sleep(60); continue

                trade_executed = execute_strategy(groww_client, engine)
                if trade_executed:
                    logging.info("Trade Done. Pausing till EOD.")
                    send_telegram_alert("🛑 *DAILY TRADE COMPLETE (SENSEX)*")
                    while datetime.now().time() < market_close: time.sleep(30) # Sleep longer if trade executed
            else:
                if current_time > market_close and current_day < 5:
                    if not eod_printed:
                        summary = engine.account.get_status_message() if isinstance(engine, PaperEngine) else "Live Mode"
                        send_telegram_alert(f"🏁 *MARKET CLOSED (SENSEX)*\n{summary}")
                        eod_printed = True
                elif current_day >= 5:
                    if not weekend_printed:
                        summary = engine.account.get_status_message() if isinstance(engine, PaperEngine) else "Live Mode"
                        send_telegram_alert(f"📅 *WEEKEND (SENSEX)*\n{summary}")
                        weekend_printed = True
                logging.info("Market Closed. Resting...")
            
            time.sleep(20)
    else: logging.error("Startup Failed: Auth Issue.")
