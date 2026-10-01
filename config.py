"""Strategy settings and API credential resolution for CCXT-Daily-Bot.

This module is safe to commit. It contains no secrets -- API credentials are
read at runtime from a gitignored ``.env`` file (see ``.env.example``).

.. warning::

   This strategy has no proven edge and is not profitable. The out-of-sample
   expectancy bootstrap CI is [-0.150, +0.345] -- it includes zero -- and across
   32 configurations x 119 symbols not one stayed profitable on a majority of
   symbols out-of-sample. See the README section "Is it profitable?".

   The settings below are engineering that holds up under scrutiny. The risk
   limits bound losses; they do not create an edge. Do not read any constant in
   this file as approval to trade it with real money.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

# --- Exchange -----------------------------------------------------------------
# Binance, Bybit and OKX spot are all supported. The credential variable names
# are derived from this value, so switching exchanges needs no other edits.
EXCHANGE_ID = "binance"

# --- Strategy -----------------------------------------------------------------
SYMBOL = "BTC/USDT"
TIMEFRAME = "1d"
SMA_PERIOD = 20

# Number of candles to pull. Must cover SMA_PERIOD and ATR_PERIOD plus a small
# margin, since the stop distance is derived from ATR.
CANDLE_LIMIT = 40

# --- Risk ---------------------------------------------------------------------
# Sizing mode.
#   "vol_target" -- size each trade from the asset's own volatility, so the USD
#                   amount at risk is constant and the stop is a fixed multiple
#                   of ATR.
#   "fixed"      -- spend a constant notional behind a constant percentage stop.
#
# Use "vol_target". The evidence is in the README: a fixed 2% stop is tighter
# than daily noise on these assets, so it is hit constantly and the bot
# re-enters into the same downtrend bleeding fees. On the identical entry rule
# this change moved expectancy from -0.384R to +0.098R per trade.
#
# To be clear about what this does and does not do: it makes the strategy
# materially better, it does not make it profitable. Walk-forward validation
# found no edge that survives out of sample. Do not read this constant as
# approval to trade it with real money.
SIZING_MODE = "vol_target"

# Share of account equity put at risk on a single trade. 1% is conventional.
RISK_PCT = 0.01

# A stop 2x ATR away is roughly equidistant in volatility terms across assets
# and regimes, which a fixed percentage is not.
ATR_PERIOD = 14
ATR_STOP_MULT = 2.0

# Take-profit distance as a multiple of the stop distance. 2.0 reproduces the
# original 2:1 reward:risk ratio.
TARGET_RATIO = 2.0

# Guards on the ATR-derived stop distance. ATR collapses in a quiet market and
# explodes in a crash; without these, a flat-volatility reading would size a
# position far larger than intended and a crisis reading would size one to dust.
MIN_STOP_PCT = 0.01
MAX_STOP_PCT = 0.15

# Hard ceiling on a single position, whatever the risk arithmetic implies.
MAX_NOTIONAL_USD = 200.0

# Account equity used for sizing when the live balance is unavailable, which
# includes paper mode. Set this to the size of the account you actually intend
# to trade, because it determines position size.
ACCOUNT_EQUITY_USD = 200.0

# --- Layer 3 risk limits -----------------------------------------------------
# These are the deterministic guardrails in risk_engine.RiskExecutionEngine. They
# are distinct from RISK_PCT above on purpose: RISK_PCT sizes a position,
# MAX_TRADE_RISK_PCT caps what an already-sized position may lose. Sizing inside
# the cap means the cap is almost never hit, so it stays an independent backstop
# against a bad ATR reading or an upstream signal that ignored sizing entirely.
MAX_TRADE_RISK_PCT = 0.01

# Daily equity loss that halts new exposure. Exits stay available once tripped --
# blocking them would strand an open position precisely when it needs closing.
MAX_DAILY_DRAWDOWN_PCT = 0.03

# Gross (absolute) exposure ceiling as a multiple of equity.
#
# This is deliberately set to 1.0, not the conventional 2.0x, because this bot
# trades unlevered spot: there is no borrowing, so exposure cannot exceed equity
# and a 2.0x cap is unreachable in normal operation -- the cash check already
# binds first. What the ceiling is really guarding against is several brackets
# being live at once and every one of them gapping through its stop, which costs
# more than any single position could. 1.0 forbids adding to a position that
# already holds the whole account. Raise it only if leverage is introduced, and
# then re-check it against the cash-balance ordering in risk_engine.
MAX_GROSS_EXPOSURE_RATIO = 1.0

# Conviction an upstream signal must claim before the engine will look at it.
MIN_CONVICTION = 0.70

# Set True to make the engine reject every signal without consulting the rest of
# the pipeline. This is the kill switch: it fails closed, so an operator can halt
# trading without editing thresholds and without the engine needing a network or
# an exchange connection to obey.
TRADING_HALTED = False

# Only read when SIZING_MODE == "fixed", which is kept so the original bracket
# can still be reproduced for comparison.
TRADE_SIZE_USD = 20.0  # Quote-currency notional per trade ($20 is the floor on most venues)
STOP_LOSS_PCT = 0.02  # 2% below entry
TAKE_PROFIT_PCT = 0.04  # 4% above entry  ->  2:1 reward:risk

# Binance rejects a stop-limit whose limit price is not strictly worse than its
# trigger price (-1013), and one that sits at the market price. This buffer
# places the stop-limit a hair below the stop trigger so the pair is always valid.
STOP_LOSS_LIMIT_BUFFER_PCT = 0.001

# --- Execution ----------------------------------------------------------------
# Keep True until the paper-trading output has been sanity-checked by hand.
# Flipping this to False submits real orders with real funds.
PAPER_TRADING = True

MAX_RETRIES = 3  # Retries for transient exchange/network errors
RETRY_BACKOFF_SECONDS = 2.0

# --- Monitoring ---------------------------------------------------------------
# A daily job that fails silently is worse than one that crashes loudly. Set
# ALERT_WEBHOOK_URL to any endpoint accepting a JSON {"text": "..."} POST (Slack,
# Discord, ntfy, a local relay). Left empty, alerts are written to the log only.
# This is configuration, not a credential, so it is safe to commit.
ALERT_WEBHOOK_URL = os.getenv("ALERT_WEBHOOK_URL", "")
ALERT_TIMEOUT_SECONDS = 10.0

# --- Fees ---------------------------------------------------------------------
# Binance spot charges 0.1% taker on both legs. At TRADE_SIZE_USD = 20 that is
# ~0.04 USDT of round-trip cost against ~0.39 USDT of risk -- material, so the
# backtest charges it rather than pretending fills are free.
TAKER_FEE_PCT = 0.001

# --- Backtest -----------------------------------------------------------------
# ~5000 daily candles is roughly thirteen years of BTC history, which is what the
# sweep needs to leave a credible out-of-sample stretch after the 70/30 split.
BACKTEST_CANDLES = 5000

# When one daily candle touches both the stop and the take-profit, daily bars
# cannot say which came first. "pessimistic" assumes the stop was hit, which is
# the honest default; "optimistic" assumes the target and is reported only to
# bracket the true outcome.
AMBIGUOUS_CANDLE_POLICY = "pessimistic"

# --- Runtime paths ------------------------------------------------------------
STATE_FILE = BASE_DIR / "state.json"
LOG_DIR = BASE_DIR / "logs"
CACHE_DIR = BASE_DIR / "cache"  # OHLCV cache for research scripts (gitignored)


def api_credentials(exchange_id: str = EXCHANGE_ID) -> dict[str, str]:
    """Resolve the API key/secret for ``exchange_id`` from the environment.

    Returns an empty dict in paper-trading mode, so the bot can validate its
    strategy logic without any credentials present at all.
    """
    if PAPER_TRADING:
        return {}

    prefix = exchange_id.upper()
    api_key = os.getenv(f"{prefix}_API_KEY", "").strip()
    api_secret = os.getenv(f"{prefix}_API_SECRET", "").strip()

    missing = [
        name
        for name, value in (
            (f"{prefix}_API_KEY", api_key),
            (f"{prefix}_API_SECRET", api_secret),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(
            f"PAPER_TRADING is False but these environment variables are "
            f"missing or empty: {', '.join(missing)}. Copy .env.example to "
            f".env and fill them in."
        )

    return {"apiKey": api_key, "secret": api_secret}
