# CCXT-Daily-Bot

A lightweight, local Python trading bot built on **CCXT**. It evaluates daily
candlesticks once per day against a 20-period Simple Moving Average and, on a
bullish signal, buys at market and immediately posts a `STOP_LOSS_LIMIT` and a
`LIMIT` take-profit straight onto the exchange order book.

Because the exit orders live on the exchange, you can power the PC off after the
run finishes — the exchange manages the trade from that point on.

---

## Features

* **Zero cloud costs.** Runs entirely on your machine. No server, no hosting bill.
* **Exchange-hosted exits.** Stop-loss and take-profit rest on the order book, so
  no 24/7 process is required.
* **Paper trading by default.** `PAPER_TRADING = True` blocks every write until you
  explicitly disable it.
* **Secrets never in git.** API keys live in a gitignored `.env`, not in source.
* **Never leaves a position naked.** If either exit leg fails to place, the
  successful leg is cancelled and the position is flattened at market.
* **No double entries.** A `state.json` file plus live order reconciliation
  prevents a second entry on the same day and sweeps up orphaned exit orders.
* **Structured logs.** Console output plus a dated file under `logs/`.

---

## Project Structure

```text
CCXT-Daily-Bot/
├── .env.example        # Template for your API keys (copy to .env)
├── .gitignore          # Ignores .env, state.json and logs/
├── config.py           # Strategy settings only — no secrets, safe to commit
├── daily_trade.py      # Signal evaluation and execution
├── requirements.txt    # Dependencies
├── README.md
│
├── .env                # (you create this) — gitignored
├── state.json          # (created at runtime) — gitignored
└── logs/               # (created at runtime) — gitignored
```

---

## Quick Start

### 1. Prerequisites

* Python 3.9 or newer
* An exchange account with API keys (Binance, Bybit or OKX spot)

### 2. Install dependencies

> **Use `python -m pip`, not bare `pip`.** If your `python` and `pip` point at
> different interpreters (common with pyenv, conda, or the Windows Store
> Python), bare `pip` will install into an environment your script never runs
> under, and the bot will fail with `ModuleNotFoundError: No module named 'ccxt'`.

```bash
python -m pip install -r requirements.txt
```

### 3. Create your `.env`

```bash
copy .env.example .env
```

Then open `.env` and fill in your keys. The variable prefix must match
`EXCHANGE_ID` in `config.py`, upper-cased (`binance` → `BINANCE_API_KEY`).

`.env` is gitignored. **Never** paste real keys into `config.py` or
`.env.example`.

### 4. Configure the strategy

Edit `config.py`:

```python
EXCHANGE_ID = "binance"       # binance | bybit | okx
SYMBOL = "BTC/USDT"
TIMEFRAME = "1d"
SMA_PERIOD = 20

TRADE_SIZE_USD = 20.0         # Notional per trade
STOP_LOSS_PCT = 0.02          # 2% stop  ─┐
TAKE_PROFIT_PCT = 0.04        # 4% target ─┘  2:1 reward:risk

PAPER_TRADING = True          # Flip to False only after validating paper output
```

### 5. Run it

```bash
python daily_trade.py
```

Read the output and confirm the sizing, stop and target levels are what you
expect. Only then consider setting `PAPER_TRADING = False`.

---

## Strategy Logic

1. **Data.** Fetch 30 daily candles via `fetch_ohlcv`.
2. **Drop the forming candle.** The last row returned is the *current, still-open*
   UTC day. Using it would compare a live intrabar tick against an SMA that
   already contains it, and the signal could flip mid-day. The decision is made
   on the last **closed** candle instead.
3. **Indicator.** 20-period SMA of closes.
4. **Signal.** Close above SMA → bullish. Otherwise no action.
5. **Sizing.** `TRADE_SIZE_USD / price`, truncated to the exchange's lot step,
   then validated against the venue's minimum amount and minimum notional.
6. **Execution (live only).** Market buy, then a `stop_loss_limit` sell 2% below
   and a `limit` sell 4% above, both for the *filled* quantity.
7. **Exit.** The script ends. The exchange takes it from there.

### What a run looks like

```text
======================================================================
CCXT-Daily-Bot | 2026-09-29 | binance/BTC/USDT
Mode: PAPER (no orders will be submitted)
======================================================================
Closed candle 2026-09-28
Close  : 83500.01000000 BTC/USDT
20 SMA: 80706.58300000
Signal : BULLISH (close above SMA)
--------------------------------------------------------------
Entry     : 83500.01000000 BTC/USDT (market buy)
Size      : 0.00023000 BTC/USDT  (19.21 USDT)
Stop-Loss : trigger 81830.01000000 / limit 81748.18000000
Take-Profit: 86840.01000000
Risk      : 0.3841 USDT   Reward: 0.7682 USDT   R:R = 2.00:1
--------------------------------------------------------------
PAPER TRADE: simulation complete. No orders submitted.
```

---

## Safety Design

These behaviours are deliberate and tested. Please do not remove them casually.

**No `reduceOnly`.** Binance spot forwards the parameter verbatim and rejects the
order with `-1104 Not all sent parameters were read`. On spot a stale
take-profit cannot fill without the base balance, and the orphan sweep cancels
stray orders before that can happen. (On futures `reduceOnly` *is* supported and
should be added.)

**Stop-limit price sits below the trigger.** Binance requires
`limit price > last price > stop price` for a sell and will otherwise reject with
`-1013` or `would immediately trigger`. The bot steps the limit price down by
whole ticks until the constraint holds.

**Exit quantity is the filled quantity**, not the requested quantity, so a
partially filled entry is never over-sold.

**Writes are never retried.** A timed-out order request may still have been
accepted by the exchange. The bot does not blindly resend it — it queries the
order book to discover what actually landed, then cancels and flattens.

**Position sizes are validated before submission.** An order below the venue's
minimum notional is rejected rather than submitted and bounced.

**`state.json` guards repeat runs.** Reconciling against the exchange each run
means a second same-day invocation declines to open another position, and any
open order the bot did not create is cancelled.

---

## Scheduling (optional)

To run once a day automatically, use Windows Task Scheduler with a daily trigger,
or cron on Linux/macOS:

```cron
17 9 * * * cd /path/to/CCXT-Daily-Bot && /path/to/python/python daily_trade.py
```

Output is written to `logs/daily_trade_YYYY-MM-DD.log` regardless of how it is
invoked.

---

## Disclaimer

This software is for educational purposes only. Cryptocurrency trading carries
substantial risk of loss. Test thoroughly in `PAPER_TRADING` mode, and never
commit real API keys to version control.
