"""CCXT-Daily-Bot -- daily swing-trading bot with exchange-hosted exits.

Run once per day. It evaluates a 20-period SMA against the last *closed* daily
candle and, on a bullish signal, buys at market then immediately posts a
STOP_LOSS_LIMIT and a LIMIT take-profit onto the exchange order book. Once
those orders are live the local machine can be powered off -- the exchange
manages the exit from that point on.

Safety properties this module is built around:

* ``PAPER_TRADING`` defaults to True in config.py and blocks every write.
* Secrets live in a gitignored ``.env``; nothing here ever logs a credential.
* The signal uses the last closed candle, not the live intrabar tick.
* Sizing is rounded to the exchange's lot step and validated against its
  minimum-amount and minimum-notional limits before anything is submitted.
* A position is never left unprotected: if either exit leg fails to place, the
  successful leg is cancelled and the position is flattened at market.
* A ``state.json`` file plus live order reconciliation prevents a second entry
  on the same day and sweeps up any orphaned exit orders.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable, TypeVar

import ccxt
import pandas as pd

import config

T = TypeVar("T")

LOGGER = logging.getLogger("daily_trade")

# Transient conditions worth retrying. Deterministic failures (InsufficientFunds,
# InvalidOrder, AuthenticationError) are deliberately excluded -- retrying them
# only wastes time and, on a write, risks duplicating an order.
RETRYABLE_ERRORS = (
    ccxt.NetworkError,
    ccxt.ExchangeNotAvailable,
    ccxt.RequestTimeout,
    ccxt.RateLimitExceeded,
)


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #
def setup_logging() -> None:
    """Log to the console and to a dated file under ``logs/``."""
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    log_path = config.LOG_DIR / f"daily_trade_{stamp}.log"

    LOGGER.setLevel(logging.INFO)
    LOGGER.handlers.clear()

    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname)-8s %(message)s"))

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-8s %(message)s")
    )

    LOGGER.addHandler(console)
    LOGGER.addHandler(file_handler)
    LOGGER.info("Log file: %s", log_path)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def utc_today() -> str:
    """Today's date in UTC, matching the exchange's daily candle boundary."""
    return datetime.now(timezone.utc).date().isoformat()


def retry_call(description: str, func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Call a read-only exchange endpoint, retrying transient failures.

    Only ever use this for *idempotent reads* (fetch_ohlcv, fetch_balance,
    fetch_order, ...). Retrying a write is unsafe: a network timeout can hide a
    request the exchange already accepted, and a retry would then double it.
    """
    attempts = max(1, config.MAX_RETRIES)
    last_error: Exception | None = None

    for attempt in range(1, attempts + 1):
        try:
            return func(*args, **kwargs)
        except RETRYABLE_ERRORS as exc:
            last_error = exc
            if attempt == attempts:
                break
            delay = config.RETRY_BACKOFF_SECONDS * attempt
            LOGGER.warning(
                "%s failed (attempt %d/%d): %s -- retrying in %.1fs",
                description,
                attempt,
                attempts,
                exc,
                delay,
            )
            time.sleep(delay)

    raise RuntimeError(
        f"{description} failed after {attempts} attempts: {last_error}"
    ) from last_error


def build_exchange() -> ccxt.Exchange:
    """Instantiate the configured exchange with rate limiting and retries."""
    if not hasattr(ccxt, config.EXCHANGE_ID):
        raise RuntimeError(f"CCXT has no exchange named {config.EXCHANGE_ID!r}")

    exchange_cls = getattr(ccxt, config.EXCHANGE_ID)
    return exchange_cls(
        {
            "apiKey": config.api_credentials().get("apiKey", ""),
            "secret": config.api_credentials().get("secret", ""),
            "enableRateLimit": True,
            "maxRetries": config.MAX_RETRIES,
        }
    )


def load_state() -> dict[str, Any]:
    """Read persisted bot state, tolerating a missing or corrupt file."""
    path = config.STATE_FILE
    if not path.exists():
        return {"symbol": config.SYMBOL, "entry": None}

    try:
        # utf-8-sig transparently accepts both BOM and BOM-less files. Windows
        # editors (Notepad, PowerShell) add a BOM on save, and losing the state
        # file to a decode error would defeat the double-entry guard.
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        LOGGER.warning("State file unreadable (%s); starting fresh.", exc)
        return {"symbol": config.SYMBOL, "entry": None}


def save_state(state: dict[str, Any]) -> None:
    """Atomically persist bot state so a crash cannot leave a truncated file."""
    path = config.STATE_FILE
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(path)


# --------------------------------------------------------------------------- #
# Operational monitoring
# --------------------------------------------------------------------------- #
def notify(message: str, *, level: str = "error") -> None:
    """Emit an operational alert.

    Always logs, because a failure that only exists in a webhook that nobody
    watches is not an alert. Additionally POSTs to ``ALERT_WEBHOOK_URL`` when one
    is configured, using the stdlib so the bot keeps its dependency footprint.
    Notification failure must never abort a run, so every error here is swallowed
    after logging.
    """
    LOGGER.log(
        logging.ERROR if level == "error" else logging.WARNING,
        "ALERT: %s",
        message,
    )
    url = config.ALERT_WEBHOOK_URL.strip()
    if not url:
        return
    try:
        payload = json.dumps(
            {"text": f"CCXT-Daily-Bot [{config.SYMBOL}] {message}"}
        ).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=config.ALERT_TIMEOUT_SECONDS):
            pass
    except Exception as exc:  # noqa: BLE001 - alerting must not break trading
        LOGGER.warning("Alert delivery failed: %s", exc)


def check_run_continuity(state: dict[str, Any]) -> None:
    """Alert when evaluation days were skipped while the bot was not running.

    The bot evaluates one closed candle per run, so a machine that is off for
    three days silently drops three signals. Nothing downstream can detect that,
    because the skipped days simply never appear anywhere. Recording the last
    evaluation date in state is the only way to notice.
    """
    last_run = state.get("last_run_date")
    if not last_run:
        return

    try:
        gap = (
            datetime.fromisoformat(utc_today()).date()
            - datetime.fromisoformat(last_run).date()
        ).days
    except ValueError:
        LOGGER.warning("Unparseable last_run_date %r in state.", last_run)
        return

    if gap > 1:
        notify(
            f"{gap - 1} evaluation day(s) were missed between {last_run} and "
            f"{utc_today()}. Those daily candles were never evaluated.",
            level="warning",
        )


def record_run(state: dict[str, Any]) -> None:
    """Stamp the evaluation date so the next run can detect gaps."""
    state["last_run_date"] = utc_today()


# --------------------------------------------------------------------------- #
# Stage 1 -- strategy signal
# --------------------------------------------------------------------------- #
def average_true_range(frame: pd.DataFrame, period: int) -> pd.Series:
    """Average true range in price units, as a simple rolling mean.

    Deliberately identical to ``backtest._atr`` -- Wilder smoothing instead would
    read slightly lower here than the research engine used, which means sizing the
    live bot on a different stop than the one that was validated.
    """
    prev_close = frame["close"].shift(1)
    true_range = pd.concat(
        [
            frame["high"] - frame["low"],
            (frame["high"] - prev_close).abs(),
            (frame["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return true_range.rolling(window=period).mean()


def evaluate_signal(exchange: ccxt.Exchange) -> tuple[bool, dict[str, float]]:
    """Return ``(is_bullish, context)`` from the last closed daily candle.

    ``fetch_ohlcv`` returns the still-forming candle for the current UTC day as
    its final element. Comparing that live partial bar against an SMA that
    already includes it produces a signal that can flip intraday, so the last
    row is dropped and the decision is made on a settled close.
    """
    candles = retry_call(
        "fetch_ohlcv",
        exchange.fetch_ohlcv,
        config.SYMBOL,
        config.TIMEFRAME,
        limit=config.CANDLE_LIMIT,
    )
    if not candles:
        raise RuntimeError("Exchange returned no OHLCV data.")

    frame = pd.DataFrame(
        candles, columns=["timestamp", "open", "high", "low", "close", "volume"]
    )
    frame["datetime"] = pd.to_datetime(frame["timestamp"], unit="ms", utc=True)
    frame["sma"] = frame["close"].rolling(window=config.SMA_PERIOD).mean()
    frame["atr"] = average_true_range(frame, config.ATR_PERIOD)

    # Drop the in-progress candle.
    closed = frame.iloc[:-1]
    needed = max(config.SMA_PERIOD, config.ATR_PERIOD)
    if len(closed) < needed:
        raise RuntimeError(
            f"Need at least {needed} closed candles to compute the SMA and ATR; "
            f"got {len(closed)}."
        )

    latest = closed.iloc[-1]
    if pd.isna(latest["sma"]):
        raise RuntimeError("SMA is NaN on the latest closed candle.")

    context = {
        "close": float(latest["close"]),
        "sma": float(latest["sma"]),
        "candle_date": latest["datetime"].date().isoformat(),
        "candle_count": float(len(closed)),
    }

    atr = float(latest["atr"]) if not pd.isna(latest["atr"]) else float("nan")
    if config.SIZING_MODE == "vol_target" and (atr != atr or atr <= 0):
        raise RuntimeError(
            f"ATR is unusable ({atr!r}) on the latest closed candle; refusing to "
            f"size a position from it."
        )
    context["atr"] = atr

    LOGGER.info("Closed candle %s", context["candle_date"])
    LOGGER.info("Close  : %.8f %s", context["close"], config.SYMBOL)
    LOGGER.info(
        "%d SMA: %.8f", config.SMA_PERIOD, context["sma"]
    )
    if atr == atr:
        LOGGER.info(
            "%d ATR: %.8f  (%.2f%% of price)", config.ATR_PERIOD, atr, atr / context["close"] * 100
        )
    LOGGER.info(
        "Signal : %s",
        "BULLISH (close above SMA)" if context["close"] > context["sma"] else "NO TRADE",
    )

    return context["close"] > context["sma"], context


# --------------------------------------------------------------------------- #
# Stage 2 -- reconciliation
# --------------------------------------------------------------------------- #
def _describe_outcome(entry: dict[str, Any], orders: dict[str, dict[str, Any]]) -> str:
    """Summarise which exit leg triggered, for the log and the state file."""
    stop = orders.get("stop", {})
    take_profit = orders.get("take_profit", {})

    if stop.get("status") == "closed" and stop.get("filled"):
        return f"STOP-LOSS filled at {float(stop['average'] or stop['price']):.8f}"
    if take_profit.get("status") == "closed" and take_profit.get("filled"):
        return f"TAKE-PROFIT filled at {float(take_profit['average'] or take_profit['price']):.8f}"
    return "bracket orders no longer open"


def reconcile(exchange: ccxt.Exchange, state: dict[str, Any], live: bool) -> bool:
    """Reconcile local state against the exchange before considering an entry.

    Returns True when a bracket order pair from a previous run is still live, in
    which case no new position should be opened.
    """
    entry = state.get("entry")

    if entry:
        # Order and balance endpoints are authenticated. In paper mode we have
        # no credentials by design, so an existing bracket cannot be verified.
        # Treat it as live anyway: a live run would decline to open a second
        # position, and the paper preview must not disagree with that.
        if not live:
            LOGGER.warning(
                "Paper mode: cannot verify the %s bracket from %s without "
                "credentials; treating it as still live.",
                config.SYMBOL,
                entry.get("entry_date", "an earlier run"),
            )
            return True

        tracked_ids = {entry.get("stop_order_id"), entry.get("tp_order_id")}
        tracked_ids.discard(None)

        orders: dict[str, dict[str, Any]] = {}
        for leg, key in (("stop", "stop_order_id"), ("take_profit", "tp_order_id")):
            order_id = entry.get(key)
            if not order_id:
                continue
            try:
                orders[leg] = retry_call(
                    f"fetch_order({leg})", exchange.fetch_order, order_id, config.SYMBOL
                )
            except Exception as exc:  # noqa: BLE001 - an unfetchable order is not fatal
                LOGGER.warning("Could not fetch %s order %s: %s", leg, order_id, exc)

        still_open = [
            leg for leg, order in orders.items() if order.get("status") == "open"
        ]

        if still_open:
            LOGGER.info(
                "Previous bracket still live (%s) -- entry %.8f at %.8f.",
                ", ".join(still_open),
                float(entry.get("amount", 0)),
                float(entry.get("entry_price", 0)),
            )
            return True

        # Nothing live: the trade resolved, or the orders were cancelled by hand.
        outcome = _describe_outcome(entry, orders)
        pnl_pct = (
            (float(orders.get("take_profit", {}).get("average") or 0) / float(entry["entry_price"]) - 1)
            if orders.get("take_profit", {}).get("filled")
            else None
        )
        if pnl_pct is not None:
            outcome += f" ({pnl_pct * 100:+.2f}%)"
        LOGGER.info("Previous trade closed: %s", outcome)

        state["entry"] = None
        state["last_outcome"] = outcome
        save_state(state)

    # Below here we have no tracked entry. In paper mode the remaining checks
    # are all authenticated, so there is nothing further to reconcile.
    if not live:
        return False

    # Sweep orphans: any open order on this symbol that we did not create.
    try:
        open_orders = retry_call(
            "fetch_open_orders", exchange.fetch_open_orders, config.SYMBOL
        )
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("Could not list open orders: %s", exc)
        return False

    for order in open_orders:
        LOGGER.warning(
            "ORPHAN CANCELLED: untracked order %s (%s %s @ %s)",
            order.get("id"),
            order.get("side"),
            order.get("amount"),
            order.get("price"),
        )
        try:
            exchange.cancel_order(order["id"], config.SYMBOL)
        except Exception as exc:  # noqa: BLE001
            LOGGER.error("Failed to cancel orphan %s: %s", order.get("id"), exc)

    # A non-zero free base balance means an untracked position exists.
    try:
        base = exchange.market(config.SYMBOL)["base"]
        free = float(
            retry_call("fetch_balance", exchange.fetch_balance).get("free", {}).get(base, 0)
        )
        if free > 0:
            LOGGER.warning(
                "Free %s balance is %.8f but no tracked position exists.", base, free
            )
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("Could not check %s balance: %s", config.SYMBOL, exc)

    return False


# --------------------------------------------------------------------------- #
# Stage 3 -- sizing and price construction
# --------------------------------------------------------------------------- #
def resolve_stop_fraction(atr: float, price: float) -> float:
    """Stop distance as a fraction of entry, from ATR under vol targeting.

    Clamped to ``MIN_STOP_PCT``/``MAX_STOP_PCT``. The clamp is a safety rail, not
    a tuning knob: without a floor, a quiet market produces a tiny ATR, a tiny
    stop, and a position size large enough to be reckless; without a ceiling, a
    volatility spike produces a stop so wide that the position rounds to nothing.
    """
    raw = config.ATR_STOP_MULT * atr / price
    return min(max(raw, config.MIN_STOP_PCT), config.MAX_STOP_PCT)


def resolve_equity(exchange: ccxt.Exchange, live: bool) -> float:
    """Account equity to size against.

    Live mode reads the real quote balance, because sizing against a stale
    configured number is how a bot quietly over-exposes an account. Paper mode
    has no credentials, so it uses the configured figure.
    """
    if not live:
        return config.ACCOUNT_EQUITY_USD

    quote = exchange.market(config.SYMBOL)["quote"]
    try:
        total = retry_call("fetch_balance", exchange.fetch_balance).get("total", {})
        equity = float(total.get(quote, 0) or 0)
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning(
            "Could not read %s balance (%s); falling back to ACCOUNT_EQUITY_USD.",
            quote,
            exc,
        )
        return config.ACCOUNT_EQUITY_USD

    if equity <= 0:
        raise RuntimeError(
            f"Total {quote} balance is zero. Refusing to size a position against "
            f"no equity."
        )
    return equity


def build_trade_plan(
    exchange: ccxt.Exchange, entry_price: float, equity: float, atr: float
) -> dict[str, float]:
    """Size the position and compute exit prices, honouring exchange limits."""
    market = exchange.market(config.SYMBOL)
    limits = market.get("limits") or {}

    if config.SIZING_MODE == "vol_target":
        stop_fraction = resolve_stop_fraction(atr, entry_price)
        # Constant USD at risk, so position size falls as volatility rises.
        target_notional = equity * config.RISK_PCT / stop_fraction
        notional_target = min(target_notional, config.MAX_NOTIONAL_USD)
        target_fraction = stop_fraction * config.TARGET_RATIO
    else:
        stop_fraction = config.STOP_LOSS_PCT
        notional_target = config.TRADE_SIZE_USD
        target_fraction = config.TAKE_PROFIT_PCT

    # amount_to_precision truncates, so the order lands at or just under the
    # target notional and never overshoots it.
    raw_amount = notional_target / entry_price
    amount = float(exchange.amount_to_precision(config.SYMBOL, raw_amount))
    if amount <= 0:
        raise RuntimeError(
            f"Target notional {notional_target:.2f} USDT is too small to buy a "
            f"tradable amount of {config.SYMBOL} at {entry_price} (lot step "
            f"truncates it to {amount})."
        )

    notional = amount * entry_price
    min_amount = (limits.get("amount") or {}).get("min")
    min_cost = (limits.get("cost") or {}).get("min")

    if min_amount is not None and amount < float(min_amount):
        raise RuntimeError(
            f"Order amount {amount} is below the exchange minimum of {min_amount}."
        )
    if min_cost is not None and notional < float(min_cost):
        raise RuntimeError(
            f"Order notional {notional:.2f} is below the exchange minimum of {min_cost}."
        )

    # price_to_precision rounds to the nearest tick, so the stop and target
    # land within half a tick of the intended percentages.
    sl_price = float(
        exchange.price_to_precision(config.SYMBOL, entry_price * (1 - stop_fraction))
    )
    tp_price = float(
        exchange.price_to_precision(config.SYMBOL, entry_price * (1 + target_fraction))
    )

    # A stop-limit needs its limit price strictly below its trigger price, and
    # below the market, or Binance rejects it (-1013 / "would immediately
    # trigger"). Step down by whole ticks until the constraint holds.
    tick = float((market.get("precision") or {}).get("price") or 0.01)
    sl_limit_price = float(
        exchange.price_to_precision(
            config.SYMBOL, sl_price * (1 - config.STOP_LOSS_LIMIT_BUFFER_PCT)
        )
    )
    for _ in range(5):
        if sl_limit_price < sl_price:
            break
        sl_limit_price = float(
            exchange.price_to_precision(config.SYMBOL, sl_limit_price - tick)
        )
    else:
        raise RuntimeError(
            "Could not derive a valid stop-limit price below the stop trigger; "
            "check STOP_LOSS_LIMIT_BUFFER_PCT against the market's price tick."
        )

    max_price = (limits.get("price") or {}).get("max")
    if max_price is not None and sl_limit_price > float(max_price):
        raise RuntimeError(f"Stop-limit {sl_limit_price} exceeds exchange max price {max_price}.")

    risk_usd = (entry_price - sl_price) * amount
    reward_usd = (tp_price - entry_price) * amount
    reward_risk = reward_usd / risk_usd if risk_usd > 0 else 0.0

    return {
        "amount": amount,
        "notional": notional,
        "stop_fraction": stop_fraction,
        "target_fraction": target_fraction,
        "risk_pct_of_equity": (risk_usd / equity * 100) if equity > 0 else 0.0,
        "sl_price": sl_price,
        "sl_limit_price": sl_limit_price,
        "tp_price": tp_price,
        "risk_usd": risk_usd,
        "reward_usd": reward_usd,
        "reward_risk": reward_risk,
    }


def log_trade_plan(plan: dict[str, float], entry_price: float, equity: float) -> None:
    """Print the full intended trade, in paper mode and on submission."""
    LOGGER.info("-" * 62)
    LOGGER.info("Sizing    : %s (%.0f%% of %.2f USDT equity at risk)",
                config.SIZING_MODE, config.RISK_PCT * 100, equity)
    LOGGER.info("Entry     : %.8f %s (market buy)", entry_price, config.SYMBOL)
    LOGGER.info("Size      : %.8f %s  (%.2f USDT)", plan["amount"], config.SYMBOL, plan["notional"])
    LOGGER.info("Stop-Loss : trigger %.8f / limit %.8f  (-%.2f%%)",
                plan["sl_price"], plan["sl_limit_price"], plan["stop_fraction"] * 100)
    LOGGER.info("Take-Profit: %.8f  (+%.2f%%)",
                plan["tp_price"], plan["target_fraction"] * 100)
    LOGGER.info(
        "Risk      : %.4f USDT (%.2f%% of equity)   Reward: %.4f USDT   R:R = %.2f:1",
        plan["risk_usd"],
        plan["risk_pct_of_equity"],
        plan["reward_usd"],
        plan["reward_risk"],
    )
    LOGGER.info("-" * 62)


# --------------------------------------------------------------------------- #
# Stage 4 -- execution
# --------------------------------------------------------------------------- #
def _flatten_position(exchange: ccxt.Exchange, reason: str) -> None:
    """Sell whatever free base balance we hold, so no position is left naked.

    This is the last line of defence during a failure, so it is written to be
    incapable of raising: any escaping exception would both abandon the
    position and mask the error that triggered this cleanup in the first place.
    """
    try:
        base = exchange.market(config.SYMBOL)["base"]
    except Exception as exc:  # noqa: BLE001
        LOGGER.critical(
            "Could not resolve the %s base currency; MANUAL INTERVENTION "
            "REQUIRED: %s",
            config.SYMBOL,
            exc,
        )
        return

    try:
        free = float(
            retry_call("fetch_balance", exchange.fetch_balance).get("free", {}).get(base, 0)
        )
    except Exception as exc:  # noqa: BLE001
        LOGGER.critical("Could not read %s balance to flatten: %s", base, exc)
        return

    if free <= 0:
        LOGGER.info("No %s balance to flatten.", base)
        return

    try:
        amount = float(exchange.amount_to_precision(config.SYMBOL, free))
    except Exception as exc:  # noqa: BLE001
        LOGGER.critical(
            "Could not size the %s position to flatten; MANUAL INTERVENTION "
            "REQUIRED: %s",
            base,
            exc,
        )
        return

    if amount <= 0:
        LOGGER.critical(
            "Free %s balance %.8f is below the tradable lot step -- MANUAL INTERVENTION "
            "REQUIRED.",
            base,
            free,
        )
        return

    try:
        order = exchange.create_order(config.SYMBOL, "market", "sell", amount)
        LOGGER.critical(
            "FLATTENED %.8f %s at market (%s). Order id: %s",
            amount,
            base,
            reason,
            order.get("id"),
        )
    except Exception as exc:  # noqa: BLE001
        LOGGER.critical(
            "EMERGENCY: could not flatten %.8f %s (%s). MANUAL INTERVENTION REQUIRED.",
            amount,
            base,
            exc,
        )


def _cancel_our_orders(exchange: ccxt.Exchange, order_ids: list[str]) -> None:
    """Best-effort cancellation of orders this run placed."""
    if not order_ids:
        return
    try:
        live_ids = {o["id"] for o in exchange.fetch_open_orders(config.SYMBOL)}
    except Exception as exc:  # noqa: BLE001
        LOGGER.error("Could not list open orders while cleaning up: %s", exc)
        return

    for order_id in order_ids:
        if order_id not in live_ids:
            continue
        try:
            exchange.cancel_order(order_id, config.SYMBOL)
            LOGGER.warning("Cancelled in-flight order %s during cleanup.", order_id)
        except Exception as exc:  # noqa: BLE001
            LOGGER.error("Failed to cancel order %s: %s", order_id, exc)


def execute_live(exchange: ccxt.Exchange, plan: dict[str, float]) -> dict[str, Any]:
    """Submit the entry and both exit legs, guaranteeing a protected position.

    Order submission is deliberately *not* wrapped in :func:`retry_call`: a
    timed-out write may have been accepted by the exchange, and blindly
    resending it would double the position. On any failure we instead query the
    order book to discover what actually landed, then clean up.
    """
    placed: list[str] = []
    amount = plan["amount"]

    try:
        entry_order = exchange.create_order(config.SYMBOL, "market", "buy", amount)
        placed.append(entry_order["id"])

        # Sell what we were actually filled with, not what we asked for.
        filled = float(entry_order.get("filled") or 0) or amount
        entry_price = float(entry_order.get("average") or entry_order.get("price") or 0)
        LOGGER.info(
            "Entry filled: %.8f %s at %.8f (order %s)",
            filled,
            config.SYMBOL,
            entry_price,
            entry_order["id"],
        )

        stop_order = exchange.create_order(
            config.SYMBOL,
            "stop_loss_limit",
            "sell",
            filled,
            plan["sl_limit_price"],
            {"stopPrice": plan["sl_price"], "timeInForce": "GTC"},
        )
        placed.append(stop_order["id"])
        LOGGER.info("Stop-loss live (order %s).", stop_order["id"])

        # No reduceOnly: Binance spot forwards it verbatim and rejects the
        # order with -1104. On spot a stale take-profit cannot fill without the
        # base balance, and reconcile() cancels orphans before that can happen.
        tp_order = exchange.create_order(
            config.SYMBOL, "limit", "sell", filled, plan["tp_price"]
        )
        placed.append(tp_order["id"])
        LOGGER.info("Take-profit live (order %s).", tp_order["id"])

        return {
            "symbol": config.SYMBOL,
            "entry_date": utc_today(),
            "entry_price": entry_price,
            "amount": filled,
            "stop_order_id": stop_order["id"],
            "tp_order_id": tp_order["id"],
            "sl_price": plan["sl_price"],
            "tp_price": plan["tp_price"],
        }

    except Exception as exc:  # noqa: BLE001 - the whole point is to catch everything
        LOGGER.critical("Order placement failed: %s", exc)
        _cancel_our_orders(exchange, placed)
        _flatten_position(exchange, reason=f"failed to place exits: {exc}")
        raise


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def run_bot() -> int:
    """One daily evaluation-and-execution cycle."""
    live = not config.PAPER_TRADING

    LOGGER.info("=" * 62)
    LOGGER.info("CCXT-Daily-Bot | %s | %s/%s", utc_today(), config.EXCHANGE_ID, config.SYMBOL)
    LOGGER.info("Mode: %s", "LIVE" if live else "PAPER (no orders will be submitted)")
    LOGGER.info("=" * 62)

    exchange = build_exchange()
    retry_call("load_markets", exchange.load_markets)

    if config.SYMBOL not in exchange.markets:
        raise RuntimeError(f"{config.SYMBOL} is not listed on {config.EXCHANGE_ID}.")

    state = load_state()
    state.setdefault("symbol", config.SYMBOL)

    check_run_continuity(state)
    record_run(state)

    if reconcile(exchange, state, live):
        LOGGER.info("A previous trade is still managed by the exchange. Nothing to do.")
        save_state(state)
        return 0

    if state.get("last_entry_date") == utc_today():
        LOGGER.info("Already opened a position today. Skipping to avoid a double entry.")
        save_state(state)
        return 0

    is_bullish, context = evaluate_signal(exchange)
    if not is_bullish:
        LOGGER.info("No trade. Price is below the %d SMA.", config.SMA_PERIOD)
        save_state(state)
        return 0

    equity = resolve_equity(exchange, live)
    plan = build_trade_plan(exchange, context["close"], equity, context["atr"])
    log_trade_plan(plan, context["close"], equity)

    if not live:
        LOGGER.info("PAPER TRADE: simulation complete. No orders submitted.")
        # Still persist the run stamp, so missed-day alerting can be exercised in
        # paper mode without going live.
        save_state(state)
        return 0

    state["entry"] = execute_live(exchange, plan)
    state["last_entry_date"] = utc_today()
    save_state(state)

    LOGGER.info("Bracket is live on the exchange. This machine can now be powered off.")
    return 0


def main() -> int:
    setup_logging()
    try:
        return run_bot()
    except Exception as exc:  # noqa: BLE001 - top-level guard for the scheduler
        LOGGER.critical("Run aborted: %s", exc)
        # A silent daily job is a daily job that quietly dies. If the process
        # exits non-zero the scheduler may only ever write it to a log nobody
        # reads, so push an alert before giving up.
        notify(f"Run aborted: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
