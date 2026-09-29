"""Strategy settings and API credential resolution for CCXT-Daily-Bot.

This module is safe to commit. It contains no secrets -- API credentials are
read at runtime from a gitignored ``.env`` file (see ``.env.example``).
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

# Number of candles to pull. SMA_PERIOD + 1 is the strict minimum; the extra
# candles are spare in case the exchange returns a short series.
CANDLE_LIMIT = 30

# --- Risk ---------------------------------------------------------------------
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

# --- Runtime paths ------------------------------------------------------------
STATE_FILE = BASE_DIR / "state.json"
LOG_DIR = BASE_DIR / "logs"


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
