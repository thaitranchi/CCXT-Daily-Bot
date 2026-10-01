# CCXT-Daily-Bot

> [!CAUTION]
> **This strategy has no proven edge and is not profitable.** A 6-month block
> bootstrap on the out-of-sample expectancy gives a 95% CI of
> `[-0.150, +0.345]` — it includes zero. Across 32 configurations × 119 symbols,
> not one stayed profitable on a majority of symbols out-of-sample. Do not run
> this with money you cannot lose. See [Is it profitable?](#is-it-profitable).
> Everything here is engineering that survives contact with reality; none of it
> is evidence that the entry rule makes money.

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
* **Missed-run detection.** A daily job that fails silently is worse than one that
  crashes loudly. The bot compares the current date against the last recorded run
  and reports how many daily candles were never evaluated, and it can POST a
  webhook to Slack/Discord/ntfy when that happens or when a run fails.
* **Research tooling.** Walk-forward validation, a survivorship-neutral symbol
  universe, and a fee-aware backtest engine — all read-only, no orders.
* **Deterministic risk gate (`risk_engine.py`).** Every trade plan is validated
  against account-level limits — hard stop required, per-trade risk cap, daily
  drawdown circuit breaker, gross exposure cap — before anything is submitted.
  It fails closed, never raises on a bad signal, and always returns a reason.
* **Local control dashboard (`dashboard.py`).** Loopback-only, read-only web UI
  showing account state, the Layer 1-3 signal flow, guardrail status and an order
  audit log. Adds `?demo=1` for reviewing the UI without credentials.

---

## Project Structure

```text
CCXT-Daily-Bot/
├── .env.example        # Template for your API keys (copy to .env)
├── .gitignore          # Ignores .env, state.json, logs/ and cache/
├── config.py           # Strategy + risk settings only — no secrets, safe to commit
├── daily_trade.py      # Signal evaluation and execution, gated by risk_engine
├── risk_engine.py      # Layer 3 deterministic risk gatekeeper
├── dashboard.py        # Local read-only control dashboard (stdlib HTTP server)
├── static/             # Dashboard assets (index.html, styles.css, app.js)
├── backtest.py         # Fee-aware backtest engine + OHLCV cache
├── signals.py          # Pluggable entry signals
├── universe.py         # Survivorship-neutral symbol universe (live + delisted)
├── sweep.py            # Parameter sweep
├── validate.py         # Multi-signal validation with statistical tests
├── walkforward.py      # Rolling train/test walk-forward (the honest number)
├── requirements.txt    # Dependencies
├── README.md
│
├── .env                # (you create this) — gitignored
├── state.json          # (created at runtime) — gitignored
├── cache/              # (created at runtime) — gitignored
└── logs/               # (created at runtime) — gitignored
```

### Research scripts

All of these are **read-only** — they never place orders and never read your API
keys. They fetch public candles and cache them under `cache/`, so repeated runs
are fast and do not hammer the exchange.

| Script | What it answers |
| --- | --- |
| `python backtest.py` | How does the current `config.py` strategy do on its own symbol? |
| `python sweep.py` | Why does a stop distance behave as it does? (**BTC only — survivor-biased**, see below) |
| `python validate.py` | Do the alternative signals beat the baseline across the whole universe? |
| `python walkforward.py` | **If I had run this search live, rolling forward, what would I have made?** |

`walkforward.py` is the one to believe. It picks a configuration on a training
window, trades it unedited over the next test window, and never looks back. It
includes delisted pairs, and it bootstraps the result so the headline number
carries an error bar.

`validate.py` and `walkforward.py` use the survivorship-neutral universe. `backtest.py`
and `sweep.py` are single-symbol, so they inherit the survivor bias that motivated
building `universe.py` in the first place — fine for diagnosing mechanics, not for
claiming an edge.

---

## Is it profitable?

No. This is the most important thing in this README, so it is also the first
thing and repeated at the top.

Run on the full Binance USDT universe (120 directional pairs, 81 still live and
39 delisted, so the coin graveyard is included rather than quietly filtered out),
daily candles, 0.1% taker fee on both legs:

| Configuration | Out-of-sample expectancy | Verdict |
| --- | --- | --- |
| Fixed $20 notional, 2% stop / 4% target | **-0.384 R/trade** | Catastrophic. Re-enters into every collapse; 23.9% win rate against a 36.7% bar. |
| Volatility-targeted, 1% risk, 2xATR stops, 4 signal families searched | **-0.006 R/trade** | No edge. Searching across signal families actively destroys it. |
| Volatility-targeted, 1% risk, 2xATR stops, single signal family | +0.086 R/trade | Looked promising until it met the block bootstrap. |

`validate.py` reaches the same conclusion from the other direction. Across 32
configurations x 119 symbols, **not one** was profitable on a majority of symbols
either in-sample or out-of-sample, and all 32 had negative pooled out-of-sample R.
The live rule managed 6 of 119 symbols out-of-sample, against a threshold of 71.

Three conclusions worth stating plainly:

**1. Sizing and stops mattered far more than signal choice.** Switching from fixed
notional with fixed 2% stops to volatility-targeted sizing with ATR stops moved
expectancy from -0.384R to +0.098R on the same underlying entry rule. The fixed
2% stop was simply too tight: ordinary daily noise triggered it, the bot re-entered
into the same downtrend, and it bled fees on the way down. This is the single
biggest improvement in the project.

`python sweep.py` finds the same thing by a completely independent route. Its
280 configurations are all fixed-percentage, and the survivors are consistently
the wide-stop ones — 4%/12%, 5%/15%, 7%/14% — while the live 2%/4% bracket is down
**-43.3R in-sample and -15.9R out-of-sample** on BTC alone. Two unrelated methods
converging on "the stops are too tight" is the most robust finding here.

**2. There is still no proven edge.** A 6-month block bootstrap over time — the
honest test, because all 100 coins share one market history — gives
`95% CI [-0.150, +0.345]`, which **includes zero**. The per-year breakdown shows
why: +76R (2020), +299R (2021), -116R (2022), +253R (2023), +87R (2024), -223R
(2025), -99R (2026). The gains arrive in bull markets and the losses arrive in
bears. It also costs a 301R drawdown to earn 277R, so even the positive number
would have been unpleasant to sit through.

**3. Any number from `sweep.py` that looks good is partly a selection artifact.**
It reports 9 of its top 10 in-sample candidates surviving out-of-sample, which
looks encouraging until you notice it searched 280 configurations and split the
history once. The walk-forward exists precisely because a single split is not
enough, and it is the only number here that never saw its test data before
choosing a configuration.

**4. Neither the risk engine nor the dashboard changes this.** `risk_engine.py`
and `dashboard.py` bound losses and make the absence of edge visible. They add no
edge whatsoever. A guardrail that reliably limits a loss on a strategy with no
edge produces a strategy with no edge and smaller losses — which is worth having,
and is not the same thing as being profitable.

**The live config has deliberately not been changed.** The volatility-targeting
upgrade is well-evidenced enough to be worth adopting, but adopting it does not
make the strategy profitable, and the parameters that *look* profitable were
chosen by looking at the data that produced them. Anyone who wants to see the
final, post-research configuration is welcome to switch `sizing` in `config.py`
themselves, with the numbers above as the context for what to expect.

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

# Risk. SIZING_MODE = "vol_target" derives the stop from ATR and sizes the
# position so a constant share of equity is at risk. See "What the research
# actually found" for why this replaced the old fixed 2%/4% bracket.
SIZING_MODE = "vol_target"
RISK_PCT = 0.01               # Share of equity at risk per trade
ATR_PERIOD = 14
ATR_STOP_MULT = 2.0           # Stop sits 2x ATR below entry
TARGET_RATIO = 2.0            # Target sits 2x the stop distance away
MAX_NOTIONAL_USD = 200.0      # Hard ceiling per position
ACCOUNT_EQUITY_USD = 200.0    # Sizing basis when the live balance is unreadable

PAPER_TRADING = True          # Flip to False only after validating paper output
```

> `ACCOUNT_EQUITY_USD` determines position size in paper mode. Set it to the size
> of the account you actually intend to trade — in live mode the real balance is
> read instead and this value is only a fallback.

### 5. Run it

```bash
python daily_trade.py
```

Read the output and confirm the sizing, stop and target levels are what you
expect. Only then consider setting `PAPER_TRADING = False`.

---

## Strategy Logic

1. **Data.** Fetch 40 daily candles via `fetch_ohlcv`.
2. **Drop the forming candle.** The last row returned is the *current, still-open*
   UTC day. Using it would compare a live intrabar tick against an SMA that
   already contains it, and the signal could flip mid-day. The decision is made
   on the last **closed** candle instead.
3. **Indicator.** 20-period SMA of closes, plus 14-period ATR.
4. **Signal.** Close above SMA → bullish. Otherwise no action.
5. **Stop distance.** `2 x ATR / price`, clamped to 1%–15%. The clamp is a safety
   rail: a flat-volatility reading would otherwise size a position far too large,
   and a crisis reading would size one to nothing.
6. **Sizing.** `equity x RISK_PCT / stop_fraction`, capped at `MAX_NOTIONAL_USD`,
   truncated to the exchange's lot step, then validated against the venue's minimum
   amount and minimum notional. Because the stop is in ATR units, the **USD at risk
   stays constant** while the position size shrinks as volatility rises.
7. **Execution (live only).** Market buy, then a `stop_loss_limit` sell at the
   computed stop and a `limit` sell at twice the stop distance above, both for the
   *filled* quantity.
8. **Exit.** The script ends. The exchange takes it from there.

### What a run looks like

```text
======================================================================
CCXT-Daily-Bot | 2026-09-30 | binance/BTC/USDT
Mode: PAPER (no orders will be submitted)
======================================================================
Closed candle 2026-09-29
Close  : 83663.66000000 BTC/USDT
20 SMA: 80974.44450000
14 ATR: 2303.63428571  (2.75% of price)
Signal : BULLISH (close above SMA)
--------------------------------------------------------------
Sizing    : vol_target (1% of 200.00 USDT equity at risk)
Entry     : 83663.66000000 BTC/USDT (market buy)
Size      : 0.00043000 BTC/USDT  (35.98 USDT)
Stop-Loss : trigger 79056.39000000 / limit 78977.33000000  (-5.51%)
Take-Profit: 92878.20000000  (+11.01%)
Risk      : 1.9811 USDT (0.99% of equity)   Reward: 3.9623 USDT   R:R = 2.00:1
--------------------------------------------------------------
PAPER TRADE: simulation complete. No orders submitted.
```

Note what the ATR line is doing there: BTC's 14-day ATR is 2.75% of price, so a
fixed 2% stop sits *inside* ordinary daily noise. The old bracket was
structurally guaranteed to be hit by nothing more than a normal day. At 2x ATR
the stop moves out to 5.51% and the position shrinks to hold the same 1% of
equity at risk.

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
