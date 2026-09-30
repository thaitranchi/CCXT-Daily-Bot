"""Historical backtest for the CCXT-Daily-Bot bracket strategy.

Replays the same signal and bracket logic that ``daily_trade.py`` trades live,
over a long history of closed candles pulled from the exchange, so profitability
can be *measured* instead of assumed.

Every modelling choice below is deliberate. They change the result, so they are
stated up front rather than buried:

* **Entry fills at the next candle's open.** The bot decides on a settled close
  and submits a market buy immediately afterwards; it cannot reliably transact at
  that close price. Filling at the following open is the honest assumption.
* **Gaps fill at the open, not at the level.** A candle opening below the stop
  exits at the open -- a real loss larger than the one planned. A candle opening
  above the take-profit exits at the open, better than planned.
* **A candle touching both levels is ambiguous.** Daily bars carry no intrabar
  ordering, so a single candle spanning the stop *and* the target has an
  unknowable outcome. ``config.AMBIGUOUS_CANDLE_POLICY`` picks which side to
  assume: ``"pessimistic"`` takes the stop, ``"optimistic"`` takes the target.
* **Fees are charged on both legs** at the venue's taker rate, which is why the
  fee-adjusted break-even win rate sits well above the naive ``stop/target`` one.
* **One position at a time**, matching the live bot: no signal is taken while a
  bracket is still open.

``run_backtest`` takes its parameters explicitly and defaults to ``config``, so
``sweep.py`` can search a parameter space through this exact same engine rather
than a lookalike reimplementation.

This script only ever calls public read endpoints. It places no orders and needs
no credentials.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import time
from typing import Any

import ccxt
import pandas as pd

import config
from daily_trade import LOGGER, build_exchange, retry_call, setup_logging
from signals import get_signal, warmup

# Milliseconds per supported timeframe, used to page backwards through history.
TIMEFRAME_MS = {
    "1m": 60_000,
    "3m": 180_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "2h": 7_200_000,
    "4h": 14_400_000,
    "6h": 21_600_000,
    "12h": 43_200_000,
    "1d": 86_400_000,
}

PAGE_LIMIT = 1000


@dataclass(frozen=True)
class StrategyParams:
    """One point in the strategy's parameter space.

    Frozen so instances stay hashable and comparable, which lets the sweeps key
    and de-duplicate candidates by value.
    """

    sma_period: int
    stop_pct: float
    target_pct: float
    notional: float
    fee_pct: float
    signal: str = "baseline"
    regime_period: int = 100
    breakout_period: int = 20
    rsi_period: int = 14
    atr_period: int = 14
    atr_threshold: float = 0.03
    rsi_threshold: float = 50.0

    # Sizing. "fixed" spends ``notional`` on every trade regardless of
    # volatility, which is backwards: it takes the largest position exactly when
    # stops are likeliest to be hit. "vol_target" sizes each trade so that a stop
    # out costs ``risk_pct`` of equity, so risk is constant across regimes.
    sizing: str = "fixed"
    risk_pct: float = 0.01
    atr_stop_mult: float = 2.0
    starting_equity: float = 1000.0
    max_position_pct: float = 1.0

    @property
    def label(self) -> str:
        if self.sizing == "vol_target":
            stop = f"{self.atr_stop_mult:g}xATR"
        else:
            stop = f"{self.stop_pct:.1%}"
        return f"{self.signal} SMA{self.sma_period} {stop}/{self.target_pct:.1%}"


def params_from_config() -> StrategyParams:
    """The live strategy's parameters, for parity-checking the backtest."""
    return StrategyParams(
        sma_period=config.SMA_PERIOD,
        stop_pct=config.STOP_LOSS_PCT,
        target_pct=config.TAKE_PROFIT_PCT,
        notional=config.TRADE_SIZE_USD,
        fee_pct=config.TAKER_FEE_PCT,
        signal="baseline",
    )


def _cache_path(symbol: str) -> Path:
    safe = symbol.replace("/", "_").replace(":", "_")
    return config.CACHE_DIR / f"{safe}_{config.TIMEFRAME}.csv"


def _read_cache(symbol: str, wanted: int) -> pd.DataFrame | None:
    """Return a cached frame when it is complete and recent enough to trust."""
    path = _cache_path(symbol)
    if not path.exists():
        return None
    try:
        frame = pd.read_csv(path)
        if len(frame) < min(wanted, 1) or "timestamp" not in frame:
            return None
        newest = int(frame["timestamp"].iloc[-1])
    except (OSError, ValueError, KeyError, IndexError):
        return None

    # A daily candle is final once the next one opens. If the newest cached
    # candle is at least a full period old the cache cannot be missing anything.
    step_ms = TIMEFRAME_MS.get(config.TIMEFRAME, 86_400_000)
    if newest < int(time.time() * 1000) - 2 * step_ms:
        return None
    return frame


def _write_cache(symbol: str, frame: pd.DataFrame) -> None:
    try:
        config.CACHE_DIR.mkdir(parents=True, exist_ok=True)
        frame.to_csv(_cache_path(symbol), index=False)
    except OSError as exc:  # noqa: BLE001 - a cache miss must never abort a run
        LOGGER.warning("Could not cache %s: %s", symbol, exc)


def fetch_history(
    exchange: ccxt.Exchange, wanted: int, symbol: str | None = None
) -> pd.DataFrame:
    """Page backwards through closed candles until ``wanted`` rows are collected.

    Results are cached to ``cache/`` so repeated research runs do not re-download
    years of candles for every symbol.

    The final candle returned by the exchange is the still-forming one, and it is
    dropped for the same reason ``daily_trade.evaluate_signal`` drops it: a live
    intrabar tick must not be compared against an average containing itself.
    """
    target_symbol = symbol or config.SYMBOL
    step_ms = TIMEFRAME_MS.get(config.TIMEFRAME)
    if step_ms is None:
        raise RuntimeError(
            f"TIMEFRAME {config.TIMEFRAME!r} is not supported by the backtest. "
            f"Add it to TIMEFRAME_MS."
        )

    cached = _read_cache(target_symbol, wanted)
    if cached is not None:
        # A fresh cache already holds everything the venue can supply for this
        # symbol, so skip paging entirely. Converting back to positional rows
        # keeps the loop below working on a single representation.
        columns = ("timestamp", "open", "high", "low", "close", "volume")
        candles = [list(row) for row in cached[list(columns)].to_numpy()]
    else:
        candles = []
        since: int | None = None

        while len(candles) < wanted + 1:
            raw = retry_call(
                "fetch_ohlcv",
                exchange.fetch_ohlcv,
                target_symbol,
                config.TIMEFRAME,
                since=since,
                limit=PAGE_LIMIT,
            )
            if not raw:
                break

            if candles:
                # ``since`` is a *lower bound*, and the venue answers with up to
                # PAGE_LIMIT candles starting there -- not the page ending at it.
                # So a page requested one candle too early overlaps the previous
                # page by all but one row. Keep only the genuinely older rows, or
                # the frame fills with duplicates and every downstream statistic is
                # fiction.
                #
                # Older pages are prepended, so the oldest held candle is candles[0].
                oldest_held = candles[0][0]
                fresh = [row for row in raw if row[0] < oldest_held]
                if not fresh:
                    break
                candles = fresh + candles
                oldest = fresh[0][0]
            else:
                candles = raw
                oldest = raw[0][0]

            if len(raw) < PAGE_LIMIT:
                break
            # Step back a whole page, not one candle.
            since = int(oldest) - PAGE_LIMIT * step_ms

    if len(candles) > wanted:
        candles = candles[-wanted:]

    frame = pd.DataFrame(
        candles, columns=["timestamp", "open", "high", "low", "close", "volume"]
    )
    if frame.empty:
        raise RuntimeError("Exchange returned no OHLCV data.")

    if cached is None:
        _write_cache(target_symbol, frame)

    frame["datetime"] = pd.to_datetime(frame["timestamp"], unit="ms", utc=True)
    # Drop the in-progress candle, exactly as the live bot does.
    frame = frame.iloc[:-1].reset_index(drop=True)
    LOGGER.info(
        "History: %s -> %s (%d closed candles, %s)",
        frame["datetime"].iloc[0].date(),
        frame["datetime"].iloc[-1].date(),
        len(frame),
        target_symbol,
    )
    return frame


def _atr(frame: pd.DataFrame, period: int) -> pd.Series:
    """Average true range in price units.

    Used for volatility-targeted sizing: a stop placed a fixed number of ATRs
    away is roughly equidistant in volatility terms across assets and regimes,
    where a fixed percentage is not.
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


def resolve_exit(
    candle: pd.Series, stop: float, target: float
) -> tuple[float, str] | None:
    """Return ``(exit_price, reason)`` if ``candle`` closes the position, else None.

    Checks gaps first, because an open beyond a level transacts at the open
    rather than at the resting level.
    """
    open_price = float(candle["open"])
    low = float(candle["low"])
    high = float(candle["high"])

    if open_price >= target:
        return open_price, "target(gap)"
    if open_price <= stop:
        return open_price, "stop(gap)"

    hit_stop = low <= stop
    hit_target = high >= target

    if hit_stop and hit_target:
        if config.AMBIGUOUS_CANDLE_POLICY == "optimistic":
            return target, "target(ambiguous)"
        return stop, "stop(ambiguous)"
    if hit_stop:
        return stop, "stop"
    if hit_target:
        return target, "target"
    return None


def run_backtest(
    frame: pd.DataFrame,
    params: StrategyParams,
    symbol: str | None = None,
) -> list[dict[str, Any]]:
    """Walk the candles forward, entering wherever ``params.signal`` fires."""
    trades: list[dict[str, Any]] = []
    signal = get_signal(params.signal)(frame, params)
    atr = _atr(frame, params.atr_period) if params.sizing == "vol_target" else None
    equity = params.starting_equity
    i = warmup(params.signal, params)

    while i < len(frame) - 1:
        if not bool(signal.iloc[i]):
            i += 1
            continue

        entry = float(frame["open"].iloc[i + 1])

        if params.sizing == "vol_target":
            atr_now = float(atr.iloc[i + 1]) if atr is not None else float("nan")
            if not atr_now or atr_now != atr_now or atr_now <= 0:
                i += 1
                continue
            stop_distance = params.atr_stop_mult * atr_now
            stop = entry - stop_distance
            target = entry + stop_distance * (params.target_pct / params.stop_pct)
            risk_amount = equity * params.risk_pct
            amount = risk_amount / stop_distance
            cap = equity * params.max_position_pct
            if amount * entry > cap:
                amount = cap / entry
                risk_amount = amount * stop_distance
            notional = amount * entry
        else:
            stop = entry * (1 - params.stop_pct)
            target = entry * (1 + params.target_pct)
            amount = params.notional / entry
            notional = amount * entry
            risk_amount = (entry - stop) * amount

        signal_date = frame["datetime"].iloc[i].date().isoformat()

        exit_price: float | None = None
        reason = ""
        exit_date = ""
        j = i + 1
        while j < len(frame):
            outcome = resolve_exit(frame.iloc[j], stop, target)
            if outcome is not None:
                exit_price, reason = outcome
                exit_date = frame["datetime"].iloc[j].date().isoformat()
                break
            j += 1

        if exit_price is None:
            # The trade never hit a bracket before the data ran out. Deleting it
            # would quietly bias the sample *against* honesty: a coin gets
            # delisted because it collapsed, so its last trade is usually its
            # worst one. Dropping exactly those trades inflates every headline
            # number. Mark the position to market at the final close instead, so
            # an unfinished trade is scored at the truth it actually reached.
            last_close = float(frame["close"].iloc[-1])
            exit_price = last_close
            exit_date = frame["datetime"].iloc[-1].date().isoformat()
            reason = "mark_to_market"
            LOGGER.debug(
                "Trade from %s unresolved; marked to market at %s.", signal_date, last_close
            )

        gross = amount * (exit_price - entry)
        fees = amount * (entry + exit_price) * params.fee_pct
        net = gross - fees
        equity += net

        trades.append(
            {
                "symbol": symbol or config.SYMBOL,
                "signal": params.signal,
                "signal_date": signal_date,
                "entry_date": frame["datetime"].iloc[i + 1].date().isoformat(),
                "exit_date": exit_date,
                "bars_held": j - i,
                "entry": entry,
                "exit": exit_price,
                "stop": stop,
                "target": target,
                "reason": reason,
                "notional": notional,
                "gross": gross,
                "fees": fees,
                "net": net,
                "equity": equity,
                "r": net / risk_amount if risk_amount > 0 else 0.0,
            }
        )
        # The bracket occupies the bot until it exits; resume from there. An
        # unresolved trade consumed the rest of the history, so stop scanning.
        i = j + 1 if exit_price is not None and reason != "mark_to_market" else len(frame)

    return trades


def net_win(params: StrategyParams) -> float:
    """Profit on a target hit, after both legs' fees."""
    return params.notional * (
        params.target_pct - params.fee_pct * (2 + params.target_pct)
    )


def net_loss(params: StrategyParams) -> float:
    """Loss on a stop hit (negative), after both legs' fees."""
    return -params.notional * (params.stop_pct + params.fee_pct * (2 - params.stop_pct))


def break_even(params: StrategyParams) -> float:
    """Fee-adjusted win rate at which this bracket is exactly flat.

    Solves ``w * net_win == (1-w) * |net_loss|`` for ``w``. This is the bar a
    strategy must clear, and it sits above the naive ``stop/(stop+target)``
    because fees are charged on losers too.

    Only defined for fixed-percentage sizing. Under volatility targeting the stop
    distance is an ATR multiple that changes every trade, so there is no single
    analytic rate; ``compute_stats`` reports the empirical requirement instead.
    """
    if params.sizing != "fixed":
        return float("nan")
    win = net_win(params)
    loss = abs(net_loss(params))
    total = win + loss
    return loss / total if total > 0 else 0.0


def compute_stats(trades: list[dict[str, Any]], params: StrategyParams) -> dict[str, float]:
    """Summary metrics for a set of trades, independent of how it was produced."""
    if not trades:
        return {
            "trades": 0,
            "wins": 0,
            "win_rate": 0.0,
            "expectancy_r": 0.0,
            "total_r": 0.0,
            "total_net": 0.0,
            "profit_factor": 0.0,
            "max_dd_r": 0.0,
            "max_dd_pct": 0.0,
            "worst_streak": 0,
            "total_fees": 0.0,
            "final_equity": params.starting_equity,
            "return_pct": 0.0,
            "required_win_rate": 0.0,
            "beatable": False,
        }

    nets = [t["net"] for t in trades]
    rs = [t["r"] for t in trades]
    wins = [n for n in nets if n > 0]
    losses = [n for n in nets if n <= 0]

    gross_win = sum(wins)
    gross_loss = abs(sum(losses))

    cumulative = peak = max_dd = 0.0
    equity = peak_eq = params.starting_equity
    max_dd_pct = 0.0
    for trade in trades:
        value = trade["r"]
        cumulative += value
        peak = max(peak, cumulative)
        max_dd = max(max_dd, peak - cumulative)
        equity = trade["equity"]
        peak_eq = max(peak_eq, equity)
        if peak_eq > 0:
            max_dd_pct = max(max_dd_pct, (peak_eq - equity) / peak_eq)

    streak = worst = 0
    for value in rs:
        streak = streak + 1 if value <= 0 else 0
        worst = max(worst, streak)

    win_rate = len(wins) / len(trades)
    final_equity = trades[-1]["equity"]

    # Empirical break-even: the win rate this exact trade distribution would need
    # to be flat. Unlike the closed form it accounts for whatever the realised
    # win/loss sizes were, including gap fills and fee drag.
    avg_win = gross_win / len(wins) if wins else 0.0
    avg_loss_abs = gross_loss / len(losses) if losses else 0.0
    required = (
        avg_loss_abs / (avg_win + avg_loss_abs)
        if (avg_win + avg_loss_abs) > 0
        else 0.0
    )

    return {
        "trades": len(trades),
        "wins": len(wins),
        "win_rate": win_rate,
        "expectancy_r": sum(rs) / len(rs),
        "total_r": sum(rs),
        "total_net": sum(nets),
        "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else float("inf"),
        "max_dd_r": max_dd,
        "max_dd_pct": max_dd_pct,
        "worst_streak": worst,
        "total_fees": sum(t["fees"] for t in trades),
        "final_equity": final_equity,
        "return_pct": (final_equity / params.starting_equity - 1) * 100,
        "required_win_rate": required,
        "beatable": win_rate > required,
    }


def filter_by_date(
    trades: list[dict[str, Any]], start: str | None, end: str | None
) -> list[dict[str, Any]]:
    """Select trades whose *entry* falls inside ``[start, end]``.

    Filtering by entry rather than slicing the candle frame keeps the SMA warmup
    continuous across the boundary, so the out-of-sample window is not penalised
    by an artificial cold start.
    """
    return [
        t
        for t in trades
        if (start is None or t["entry_date"] >= start) and (end is None or t["entry_date"] < end)
    ]


def summarise(trades: list[dict[str, Any]], params: StrategyParams) -> None:
    """Print win rate, expectancy and drawdown -- the numbers that matter."""
    if not trades:
        LOGGER.warning("No trades were taken over this history; the signal never fired.")
        return

    stats = compute_stats(trades, params)
    ambiguous = [t for t in trades if "ambiguous" in t["reason"]]
    wins = [t for t in trades if t["net"] > 0]
    losses = [t for t in trades if t["net"] <= 0]
    hold_wins = [float(t["bars_held"]) for t in wins]
    hold_losses = [float(t["bars_held"]) for t in losses]

    def avg(values: list[float]) -> float:
        return sum(values) / len(values) if values else 0.0

    LOGGER.info("=" * 62)
    LOGGER.info(
        "BACKTEST RESULTS | %s | %s | %s", config.EXCHANGE_ID, config.SYMBOL, config.TIMEFRAME
    )
    LOGGER.info("Parameters   : %s", params.label)
    LOGGER.info(
        "Ambiguous    : %d trades (%s assumed)",
        len(ambiguous),
        "stop" if config.AMBIGUOUS_CANDLE_POLICY == "pessimistic" else "target",
    )
    LOGGER.info("=" * 62)
    LOGGER.info("Trades       : %d", stats["trades"])
    LOGGER.info("Wins / Losses: %d / %d", stats["wins"], stats["trades"] - stats["wins"])
    LOGGER.info("Win rate     : %.2f%%", stats["win_rate"] * 100)
    analytic = break_even(params)
    if analytic == analytic:
        LOGGER.info(
            "Break-even   : %.2f%% (fee-adjusted; naive is %.2f%%)",
            analytic * 100,
            params.stop_pct / (params.stop_pct + params.target_pct) * 100,
        )
    LOGGER.info(
        "Required     : %.2f%% empirical win rate to break even on these trades",
        stats["required_win_rate"] * 100,
    )
    LOGGER.info("-" * 62)
    LOGGER.info("Net P&L      : %+.4f USDT", stats["total_net"])
    LOGGER.info("Final equity : %.2f USDT (%+.1f%% from %.0f)",
                stats["final_equity"], stats["return_pct"], params.starting_equity)
    LOGGER.info("Total fees   : %.4f USDT", stats["total_fees"])
    LOGGER.info("Avg win      : %+.4f USDT", avg([t["net"] for t in wins]))
    LOGGER.info("Avg loss     : %+.4f USDT", avg([t["net"] for t in losses]))
    LOGGER.info("Profit factor: %.2f", stats["profit_factor"])
    LOGGER.info("-" * 62)
    LOGGER.info("Expectancy   : %+.3f R per trade", stats["expectancy_r"])
    LOGGER.info("Total R      : %+.2f R", stats["total_r"])
    LOGGER.info("Max drawdown : %.2f R  (%.1f%% of peak equity)", stats["max_dd_r"], stats["max_dd_pct"] * 100)
    LOGGER.info("Worst streak : %d consecutive losses", stats["worst_streak"])
    LOGGER.info(
        "Avg bars held: %.1f (winners) / %.1f (losers)", avg(hold_wins), avg(hold_losses)
    )
    LOGGER.info("-" * 62)
    if stats["total_net"] > 0:
        LOGGER.info("VERDICT      : profitable over this history.")
    else:
        LOGGER.info(
            "VERDICT      : NOT profitable. Needed %.2f%% wins, got %.2f%%.",
            stats["required_win_rate"] * 100,
            stats["win_rate"] * 100,
        )


def print_trades(trades: list[dict[str, Any]], limit: int = 15) -> None:
    """List the most recent trades so the run can be eyeballed."""
    if not trades:
        return
    LOGGER.info("-" * 62)
    LOGGER.info("Last %d of %d trades:", min(limit, len(trades)), len(trades))
    LOGGER.info(
        "%-11s %-11s %-11s %10s %7s  %-16s",
        "ENTERED", "EXITED", "ENTRY PX", "NET", "R", "REASON",
    )
    for trade in trades[-limit:]:
        LOGGER.info(
            "%-11s %-11s %-11.2f %+10.4f %+6.2f  %-16s",
            trade["entry_date"],
            trade["exit_date"],
            trade["entry"],
            trade["net"],
            trade["r"],
            trade["reason"],
        )


def main() -> int:
    setup_logging()
    try:
        LOGGER.info("=" * 62)
        LOGGER.info("CCXT-Daily-Bot BACKTEST (read-only, no orders)")
        LOGGER.info("=" * 62)

        exchange = build_exchange()
        retry_call("load_markets", exchange.load_markets)
        if config.SYMBOL not in exchange.markets:
            raise RuntimeError(f"{config.SYMBOL} is not listed on {config.EXCHANGE_ID}.")

        frame = fetch_history(exchange, config.BACKTEST_CANDLES)
        trades = run_backtest(frame, params_from_config())
        summarise(trades, params_from_config())
        print_trades(trades)
        return 0
    except Exception as exc:  # noqa: BLE001 - top-level guard, matches daily_trade
        LOGGER.critical("Backtest aborted: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
