# Autonomus-Algorithmic-Trading
Professional Python-based algorithmic trading bot for Groww with Paper Trading, Live Trading, Risk Management, Telegram Alerts, VWAP + RSI Strategy, and Dynamic Trailing Stop Loss.


# Ivan AlgoBot

A professional algorithmic trading bot built in Python for the Groww API.

Supports both Paper Trading and Live Trading with advanced risk management.

---

## Features

✔ Paper Trading Engine

✔ Live Trading Engine

✔ Groww API Integration

✔ Telegram Signal Alerts

✔ Dynamic Trailing Stop Loss

✔ Virtual Portfolio Management

✔ Daily P&L Tracking

✔ Risk Management

✔ Auto Square Off

✔ VWAP + RSI Strategy

✔ Position Sizing

✔ Trade Logging

✔ Market Session Detection

---

## Strategy

The bot enters trades using:

- VWAP
- RSI
- India VIX Filter

Trade Conditions

Bullish

- Price > VWAP
- RSI > 60
- VIX Safe

Bearish

- Price < VWAP
- RSI < 40
- VIX Safe

---

## Technologies

- Python
- Pandas
- pandas_ta
- Requests
- Groww API
- Telegram Bot API
- TOTP Authentication

---

## Back Testing

- It consists of 30 logs of back testing data
- Daily PnO data
- Groww Instruments data
- A sample scalper.py code for scalping

---

## Trading Modes

Paper Trading

- Virtual Capital
- Safe Testing
- Performance Tracking

Live Trading

- Real Orders
- Real-Time Execution
- Automatic SL
- Dynamic Trailing SL

---

## Risk Management

- Daily Loss Limit
- Position Size Limit
- Margin Control
- Auto Square Off
- Dynamic Stop Loss
- Slippage Simulation

---

## Disclaimer

v1,v2,v3,....represents a version or a modified code

This project is for educational purposes only.

Trading involves financial risk. Use Live Mode at your own risk and responsibility.
