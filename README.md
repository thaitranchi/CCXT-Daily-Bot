# CCXT-Daily-Bot

A lightweight, local Python trading bot built on top of the **CCXT** library. It evaluates daily candlestick data (1D timeframe) once per day against a technical indicator (20-day Simple Moving Average) and submits **Take-Profit (TP)** and **Stop-Loss (SL)** orders directly to the exchange's order book.

Because execution orders sit natively on the exchange, you can safely turn off your local PC after running the script once per day.

---

## Features

* **Zero Cloud Costs:** Runs entirely on your local machine using standard Python libraries.
* **Exchange Offloading:** Places limit stop-loss and limit take-profit orders directly on the exchange order book, eliminating the need for a 24/7 server.
* **Paper Trading Mode:** Built-in simulation flag (`PAPER_TRADING = True`) to safely test signals before connecting live exchange funds.
* **Modular Configuration:** Keeps API secrets, risk management parameters, and trading symbols in a separate configuration file.

---

## Directory Structure

```text
CCXT-Daily-Bot/
├── config.py          # API keys, trade sizing, and strategy settings
├── daily_trade.py     # Main signal evaluation and execution script
└── requirements.txt   # Required Python dependencies

```

---

## Quick Start

### 1. Prerequisites

* Python 3.9+ installed on your PC.
* An active crypto exchange account (e.g., Binance, Bybit, OKX) with API keys generated.

### 2. Installation

Clone or create the project folder on your local machine, open your terminal inside the directory, and install the required dependencies:

```bash
pip install -r requirements.txt

```

### 3. Setup Configuration

Open `config.py` and update your exchange API keys and trading rules:

```python
# API Credentials (Keep private)
API_KEY = "YOUR_EXCHANGE_API_KEY"
SECRET_KEY = "YOUR_EXCHANGE_SECRET_KEY"

# Strategy Settings
SYMBOL = "BTC/USDT"
TIMEFRAME = "1d"
TRADE_SIZE_USD = 20.0     # Fixed trade amount in USDT ($20 minimum per exchange rules)
STOP_LOSS_PCT = 0.02      # 2% Stop-Loss below entry
TAKE_PROFIT_PCT = 0.04    # 4% Take-Profit above entry
PAPER_TRADING = True      # Set to False when ready for live order execution

```

### 4. Running the Bot

Run the strategy manually once per day (or set it as a daily Windows Task Scheduler / Cron job):

```bash
python daily_trade.py

```

---

## Strategy Logic Overview

1. **Data Ingestion:** Fetches the last 30 daily candles (`1d`) via CCXT.
2. **Indicator Calculation:** Computes the 20-period Simple Moving Average (SMA).
3. **Signal Check:**
* **Bullish Signal:** If the latest closing price is above the 20 SMA, the bot enters a market buy position.
* **Exit Orders:** Immediately attaches a `STOP_LOSS_LIMIT` and a `LIMIT` Take-Profit order directly to the exchange order book.
* **No Signal:** If price is below the 20 SMA, no action is taken.


4. **Graceful Exit:** The script terminates after submitting orders, letting the exchange handle trade closures automatically.

---

## Disclaimer

This software is for educational purposes only. Cryptocurrency trading carries substantial risk. Test thoroughly in `PAPER_TRADING` mode before committing real funds.
