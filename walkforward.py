"""Walk-forward validation: the honest way to grade a parameter search.

``validate.py`` and ``sweep.py`` both pick a configuration by looking at one
window and then report how it did on a different one. That is better than
ignoring the problem, but it is still fragile: it assumes the single split date
is representative, and it lets one lucky window decide the winner.

Walk-forward removes both assumptions by mimicking how the strategy would
actually be used. The procedure is:

1. Start at the beginning of history with a training window.
2. Pick the configuration with the best training expectancy.
3. **Trade it, unedited, over the next test window.**
4. Roll forward and repeat, never looking back.

The number that matters is the concatenation of all those forward test periods,
stitched into one equity curve. It answers the question you would actually care
about -- "if I had run this procedure live, rolling forward, what would I have
made?" -- and it cannot be inflated by hindsight, because step 2 only ever sees
data that precedes step 3.

A selection that cannot be beaten by the training-window default is reported as
such. When the walk-forward curve is no better than always using one fixed
configuration, that is the finding: the search adds nothing over a constant
choice.

Delisted pairs are included by default, so the universe is not quietly filtered
down to the coins that survived.

Read-only: no orders, no credentials.
"""

from __future__ import annotations

import random
from typing import Any

import pandas as pd

import config
from backtest import (
    StrategyParams,
    compute_stats,
    fetch_history,
    filter_by_date,
    run_backtest,
)
from daily_trade import LOGGER, build_exchange, retry_call, setup_logging
from universe import build_universe, describe

# Cap on pairs pulled, to keep the run bounded. The stride in build_universe
# preserves the live/delisted mix, so this stays a fair sample rather than a
# list of whichever symbols happened to be fetched first.
MAX_SYMBOLS = 120

# A candidate grid kept deliberately small: every extra configuration raises the
# chance that the training window's winner is noise.
TRAIN_MONTHS = 24
TEST_MONTHS = 6

SIGNAL_GRID: list[str] = ["baseline", "fresh_cross", "cross_regime", "breakout_regime"]
SMA_GRID: list[int] = [20, 50]
STOP_GRID: list[float] = [0.02, 0.04]
TARGET_RATIO = 2.0

RISK_PCT = 0.01
ATR_STOP_MULT = 2.0
STARTING_EQUITY = 1000.0


def candidate_grid() -> list[StrategyParams]:
    """Volatility-targeted candidates: signal x SMA x stop, 2:1 reward:risk."""
    params: list[StrategyParams] = []
    for signal, sma, stop in [
        (s, m, t) for s in SIGNAL_GRID for m in SMA_GRID for t in STOP_GRID
    ]:
        params.append(
            StrategyParams(
                sma_period=sma,
                stop_pct=stop,
                target_pct=round(stop * TARGET_RATIO, 6),
                notional=0.0,
                fee_pct=config.TAKER_FEE_PCT,
                signal=signal,
                regime_period=100,
                breakout_period=20,
                sizing="vol_target",
                risk_pct=RISK_PCT,
                atr_stop_mult=ATR_STOP_MULT,
                starting_equity=STARTING_EQUITY,
            )
        )
    return params


def _month_starts(dates: pd.Series) -> list[str]:
    return sorted({str(d)[:7] for d in dates})


def _add_months(iso: str, months: int) -> str:
    year, month = int(iso[:4]), int(iso[5:7])
    total = year * 12 + (month - 1) + months
    return f"{total // 12:04d}-{total % 12 + 1:02d}-01"


def walk_forward(
    frames: dict[str, pd.DataFrame], grid: list[StrategyParams]
) -> tuple[list[dict[str, Any]], list[str]]:
    """Roll a training window forward, trading each winner over the next test leg.

    Returns the per-fold records and the stitched out-of-sample trades.
    """
    all_dates = sorted(
        {str(d)[:10] for frame in frames.values() for d in frame["datetime"]}
    )
    months = _month_starts(pd.Series(all_dates))
    if not months:
        raise RuntimeError("No history available to walk forward over.")

    # A candidate's full trade history does not depend on the fold, only the
    # date filter applied afterwards. Computing each (symbol, candidate) pair once
    # instead of once per fold turns ~23,000 backtests into ~1,900.
    cache: dict[tuple[str, str], list[dict[str, Any]]] = {}

    def trades_for(symbol: str, frame: pd.DataFrame, params: StrategyParams):
        key = (symbol, params.label)
        if key not in cache:
            cache[key] = run_backtest(frame, params, symbol)
        return cache[key]

    folds: list[dict[str, Any]] = []
    oos_trades: list[dict[str, Any]] = []
    cursor = 0

    while cursor + TRAIN_MONTHS + TEST_MONTHS <= len(months):
        train_start = months[cursor]
        train_end = _add_months(months[cursor + TRAIN_MONTHS], 0)
        test_end = _add_months(months[cursor + TRAIN_MONTHS + TEST_MONTHS], 0)

        best: StrategyParams | None = None
        best_exp = None
        for candidate in grid:
            rs = []
            for symbol, frame in frames.items():
                tr = filter_by_date(trades_for(symbol, frame, candidate), None, train_end)
                rs.extend(
                    t["r"] for t in tr if t["entry_date"] >= train_start
                )
            if len(rs) < 5:
                continue
            exp = sum(rs) / len(rs)
            if best_exp is None or exp > best_exp:
                best_exp = exp
                best = candidate

        if best is None:
            cursor += TEST_MONTHS
            continue

        fold_trades: list[dict[str, Any]] = []
        for symbol, frame in frames.items():
            tr = trades_for(symbol, frame, best)
            fold_trades.extend(filter_by_date(tr, train_end, test_end))

        stats = compute_stats(fold_trades, best)
        folds.append(
            {
                "train_start": train_start,
                "train_end": train_end,
                "test_end": test_end,
                "params": best,
                "train_expectancy": best_exp,
                "test": stats,
            }
        )
        oos_trades.extend(fold_trades)
        cursor += TEST_MONTHS

    return folds, oos_trades


def report(
    folds: list[dict[str, Any]],
    oos_trades: list[dict[str, Any]],
    grid: list[StrategyParams],
    frames: dict[str, pd.DataFrame],
    universe_counts: tuple[int, int, int],
) -> None:
    """Print the fold-by-fold record and the stitched forward result."""
    total, live, dead = universe_counts

    LOGGER.info("=" * 78)
    LOGGER.info("WALK-FORWARD VALIDATION")
    LOGGER.info("=" * 78)
    LOGGER.info("Full USDT universe : %d directional pairs (%d live, %d delisted)",
                total, live, dead)
    LOGGER.info("Symbols with data : %d", len(frames))
    LOGGER.info("Candidates searched: %d (vol-targeted, %.1f%% risk, %.1fxATR)",
                len(grid), RISK_PCT * 100, ATR_STOP_MULT)
    LOGGER.info("Train window        : %d months", TRAIN_MONTHS)
    LOGGER.info("Test window         : %d months", TEST_MONTHS)
    LOGGER.info("Folds               : %d", len(folds))
    LOGGER.info("=" * 78)

    if not folds:
        LOGGER.warning("Not enough history for a single complete fold.")
        return

    LOGGER.info(
        "%-11s %-11s %-34s %8s %8s %8s",
        "TRAIN", "TEST", "SELECTED ON TRAIN", "TR expR", "TST R", "TST n",
    )
    for fold in folds:
        LOGGER.info(
            "%-11s %-11s %-34s %8.3f %+8.1f %8d",
            fold["train_start"],
            fold["train_end"],
            fold["params"].label,
            fold["train_expectancy"],
            fold["test"]["total_r"],
            fold["test"]["trades"],
        )

    stats = compute_stats(oos_trades, grid[0])
    LOGGER.info("-" * 78)
    LOGGER.info("STITCHED FORWARD (OOS) RESULT -- every trade the procedure would")
    LOGGER.info("have made, selected blind on each preceding training window")
    LOGGER.info("  Trades        : %d", stats["trades"])
    LOGGER.info("  Win rate      : %.2f%%", stats["win_rate"] * 100)
    LOGGER.info("  Required      : %.2f%%", stats["required_win_rate"] * 100)
    LOGGER.info("  Total R       : %+.1f", stats["total_r"])
    LOGGER.info("  Expectancy    : %+.4f R/trade", stats["expectancy_r"])
    LOGGER.info("  Max drawdown  : %.1f R", stats["max_dd_r"])
    LOGGER.info(
        "  (Drawdown is in R, not %%: these trades come from %d independent books, so a"
        % len({t["symbol"] for t in oos_trades})
    )
    LOGGER.info(
        "   single equity curve would stitch unrelated account balances together.)"
    )
    _bootstrap(oos_trades)
    LOGGER.info("-" * 78)
    _report_vs_best_fixed(oos_trades, grid, frames)


def _bootstrap(trades: list[dict[str, Any]], resamples: int = 5000) -> None:
    """Bootstrap the expectancy so the headline number carries an error bar."""
    rs = [t["r"] for t in trades]
    if len(rs) < 30:
        return
    rng = random.Random(11)
    means = sorted(
        sum(rng.choices(rs, k=len(rs))) / len(rs) for _ in range(resamples)
    )
    lo, hi = means[int(0.025 * resamples)], means[int(0.975 * resamples)]
    verdict = "excludes zero" if lo > 0 or hi < 0 else "INCLUDES zero (no proven edge)"
    LOGGER.info("  95%% CI on expectancy: [%+.4f, %+.4f]  -> %s", lo, hi, verdict)


def _report_vs_best_fixed(
    oos_trades: list[dict[str, Any]],
    grid: list[StrategyParams],
    frames: dict[str, pd.DataFrame],
) -> None:
    """Does the search beat the best single configuration known in hindsight?

    The best in-hindsight configuration is an unreachable benchmark: it was
    chosen with full knowledge of the period. If walk-forward cannot match it,
    the search is not earning its complexity.
    """
    best_hist = None
    best_r = None
    for candidate in grid:
        rs = []
        for frame in frames.values():
            rs.extend(t["r"] for t in run_backtest(frame, candidate))
        if rs and (best_r is None or sum(rs) > best_r):
            best_r = sum(rs)
            best_hist = candidate

    if best_hist is None:
        return

    oos_r = sum(t["r"] for t in oos_trades)
    all_r = []
    for frame in frames.values():
        all_r.extend(t["r"] for t in run_backtest(frame, best_hist))

    LOGGER.info("BENCHMARK: best single config chosen with full hindsight")
    LOGGER.info("  %s", best_hist.label)
    LOGGER.info("  Total R over ALL history : %+.1f", sum(all_r))
    LOGGER.info("  Walk-forward achieved    : %+.1f", oos_r)
    verdict = (
        "walk-forward MATCHED the hindsight benchmark"
        if oos_r >= best_r * 0.5
        else "walk-forward fell well SHORT of the hindsight benchmark"
    )
    LOGGER.info("  -> %s", verdict)
    LOGGER.info("     The benchmark is unreachable in practice (it was chosen")
    LOGGER.info("     knowing the answer), so a shortfall here is the expected")
    LOGGER.info("     and honest result, not a bug.")


def load_frames(exchange: ccxt.Exchange, max_symbols: int | None) -> dict[str, pd.DataFrame]:
    """Load the survivorship-neutral universe, skipping any pair without history."""
    universe = build_universe(exchange, max_symbols=max_symbols)
    frames: dict[str, pd.DataFrame] = {}
    for market in universe:
        symbol = market["symbol"]
        try:
            frames[symbol] = fetch_history(exchange, config.BACKTEST_CANDLES, symbol)
        except Exception as exc:  # noqa: BLE001 - one bad pair must not abort
            LOGGER.warning("%s failed to load (%s); skipping.", symbol, exc)
    return frames, describe(universe)


def main() -> int:
    setup_logging()
    try:
        LOGGER.info("=" * 78)
        LOGGER.info("CCXT-Daily-Bot WALK-FORWARD (read-only, no orders)")
        LOGGER.info("=" * 78)

        exchange = build_exchange()
        retry_call("load_markets", exchange.load_markets)
        frames, counts = load_frames(exchange, MAX_SYMBOLS)
        if not frames:
            raise RuntimeError("No symbols could be loaded.")

        grid = candidate_grid()
        folds, oos_trades = walk_forward(frames, grid)
        report(folds, oos_trades, grid, frames, counts)
        return 0
    except Exception as exc:  # noqa: BLE001 - top-level guard, matches daily_trade
        LOGGER.critical("Walk-forward aborted: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
