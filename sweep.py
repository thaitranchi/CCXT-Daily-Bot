"""Parameter sweep for the CCXT-Daily-Bot bracket strategy.

Searches SMA period x stop distance x reward:risk and reports what survives on
data the search never saw.

The split matters more than the sweep. Fitting a few hundred configurations to
one price series and reporting the winner's backtest would be curve-fitting: with
~300 trades per configuration, some candidates clear break-even purely by luck.
So the history is cut chronologically, candidates are *selected* on the early
(in-sample) portion only, and the shortlist is then judged on the later
(out-of-sample) portion it had no hand in producing. A parameter set that only
works in-sample is overfit, and this script is built to show that rather than
hide it.

Selection is deliberately restricted to round, conventional values. The more
freely one may search, the more reliably one manufactures a fake winner.

Reuses ``backtest.run_backtest`` so every candidate runs through the identical
simulation code as the live bot. Read-only: no orders, no credentials.
"""

from __future__ import annotations

import itertools
from typing import Any

import config
from backtest import (
    StrategyParams,
    break_even,
    compute_stats,
    fetch_history,
    filter_by_date,
    run_backtest,
)
from daily_trade import LOGGER, build_exchange, retry_call, setup_logging

SMA_PERIODS = [10, 15, 20, 30, 40, 50, 75, 100, 150, 200]
STOP_PCTS = [0.01, 0.015, 0.02, 0.03, 0.04, 0.05, 0.07]
REWARD_RISKS = [1.0, 1.5, 2.0, 3.0]

# Fraction of history reserved for out-of-sample scoring. 70/30 keeps the
# selection set large enough to rank candidates while leaving a stretch of
# unseen data to grade them on.
IS_FRACTION = 0.70

# Candidates must show at least this many out-of-sample trades before their
# expectancy is treated as meaningful rather than noise.
MIN_OOS_TRADES = 15


def candidate_params() -> list[StrategyParams]:
    """Every configuration under test, on a conventional grid."""
    candidates = []
    for period, stop, rr in itertools.product(SMA_PERIODS, STOP_PCTS, REWARD_RISKS):
        candidates.append(
            StrategyParams(
                sma_period=period,
                stop_pct=stop,
                target_pct=round(stop * rr, 6),
                notional=config.TRADE_SIZE_USD,
                fee_pct=config.TAKER_FEE_PCT,
            )
        )
    return candidates


def evaluate(frame, split_date: str) -> list[dict[str, Any]]:
    """Run every candidate and score its in-sample and out-of-sample halves."""
    results = []
    for params in candidate_params():
        trades = run_backtest(frame, params)
        in_sample = compute_stats(filter_by_date(trades, None, split_date), params)
        out_sample = compute_stats(filter_by_date(trades, split_date, None), params)
        results.append(
            {
                "params": params,
                "is": in_sample,
                "oos": out_sample,
                "is_profitable": in_sample["total_r"] > 0,
                "oos_profitable": out_sample["total_r"] > 0,
            }
        )
    return results


def report_split_date(frame) -> str:
    """The first out-of-sample entry date, for the header."""
    return str(frame["datetime"].iloc[int(len(frame) * IS_FRACTION)].date())


def report(results: list[dict[str, Any]], split_date: str) -> None:
    """Print the selection set, the grade, and the honest verdict."""
    LOGGER.info("=" * 78)
    LOGGER.info("PARAMETER SWEEP | %s | %s | %s", config.EXCHANGE_ID, config.SYMBOL, config.TIMEFRAME)
    LOGGER.info("Configurations tested : %d", len(results))
    LOGGER.info("In-sample  ends before : %s (70%% of history)", split_date)
    LOGGER.info("Out-of-sample starts  : %s (never used for selection)", split_date)
    LOGGER.info(
        "Minimum trades to trust a verdict: %d out-of-sample", MIN_OOS_TRADES
    )
    LOGGER.info("=" * 78)

    graded = [r for r in results if r["oos"]["trades"] >= MIN_OOS_TRADES]
    is_profitable = sum(1 for r in results if r["is_profitable"])
    oos_profitable = sum(1 for r in graded if r["oos_profitable"])

    LOGGER.info(
        "Profitable in-sample     : %d / %d (%.0f%%)",
        is_profitable,
        len(results),
        is_profitable / len(results) * 100,
    )
    LOGGER.info(
        "Profitable out-of-sample : %d / %d (%.0f%%) -- %d had enough trades to judge",
        oos_profitable,
        len(graded),
        oos_profitable / len(graded) * 100 if graded else 0.0,
        len(graded),
    )
    LOGGER.info("-" * 78)

    top_is = sorted(results, key=lambda r: r["is"]["total_r"], reverse=True)[:10]
    LOGGER.info("Top 10 by IN-SAMPLE result (what naive tuning would have picked):")
    LOGGER.info(
        "%-22s %7s %8s %9s | %7s %8s %9s %s",
        "PARAMS", "IS n", "IS win%", "IS R", "OOS n", "OOS win%", "OOS R", "OOS OK?",
    )
    for row in top_is:
        _log_row(row)

    LOGGER.info("-" * 78)
    top_oos = sorted(graded, key=lambda r: r["oos"]["total_r"], reverse=True)[:10]
    LOGGER.info("Top 10 by OUT-OF-SAMPLE result (honest ranking, but selection-biased too):")
    LOGGER.info(
        "%-22s %7s %8s %9s | %7s %8s %9s %s",
        "PARAMS", "IS n", "IS win%", "IS R", "OOS n", "OOS win%", "OOS R", "OOS OK?",
    )
    for row in top_oos:
        _log_row(row)

    _report_survival(top_is, graded)
    _report_live_config(results)


def _log_row(row: dict[str, Any]) -> None:
    params: StrategyParams = row["params"]
    is_stats = row["is"]
    oos_stats = row["oos"]
    LOGGER.info(
        "%-22s %7d %7.1f%% %+9.1f | %7d %7.1f%% %+9.1f %s",
        params.label,
        is_stats["trades"],
        is_stats["win_rate"] * 100,
        is_stats["total_r"],
        oos_stats["trades"],
        oos_stats["win_rate"] * 100,
        oos_stats["total_r"],
        "yes" if row["oos_profitable"] else "NO",
    )


def _report_survival(top_is: list[dict[str, Any]], graded: list[dict[str, Any]]) -> None:
    """How many of the in-sample winners held up out-of-sample?"""
    live = [r for r in top_is if r["oos"]["trades"] >= MIN_OOS_TRADES]
    survived = [r for r in live if r["oos_profitable"]]
    LOGGER.info("-" * 78)
    LOGGER.info("SURVIVAL CHECK")
    if not live:
        LOGGER.info(
            "  None of the top in-sample candidates had %d+ out-of-sample trades "
            "to be judged on.",
            MIN_OOS_TRADES,
        )
        return
    LOGGER.info(
        "  Top-10 in-sample candidates that stayed profitable out-of-sample: %d / %d",
        len(survived),
        len(live),
    )
    if survived:
        for row in survived:
            LOGGER.info(
                "    %s -> OOS %+.1f R over %d trades",
                row["params"].label,
                row["oos"]["total_r"],
                row["oos"]["trades"],
            )
    else:
        LOGGER.info(
            "  Every in-sample winner failed out-of-sample. That is the signature"
        )
        LOGGER.info(
            "  of curve-fitting: the edge was in the noise, not in the parameters."
        )


def _report_live_config(results: list[dict[str, Any]]) -> None:
    """Score the configuration the bot actually trades today."""
    from backtest import params_from_config

    live = params_from_config()
    match = next((r for r in results if r["params"] == live), None)
    if match is None:
        return
    LOGGER.info("-" * 78)
    LOGGER.info("CURRENT LIVE CONFIGURATION: %s", live.label)
    LOGGER.info(
        "  Break-even win rate : %.2f%% (fee-adjusted)",
        break_even(live) * 100,
    )
    LOGGER.info(
        "  In-sample  : %d trades, win %.1f%%, %+.1f R",
        match["is"]["trades"],
        match["is"]["win_rate"] * 100,
        match["is"]["total_r"],
    )
    LOGGER.info(
        "  Out-sample : %d trades, win %.1f%%, %+.1f R",
        match["oos"]["trades"],
        match["oos"]["win_rate"] * 100,
        match["oos"]["total_r"],
    )


def main() -> int:
    setup_logging()
    try:
        LOGGER.info("=" * 78)
        LOGGER.info("CCXT-Daily-Bot PARAMETER SWEEP (read-only, no orders)")
        LOGGER.info("=" * 78)

        exchange = build_exchange()
        retry_call("load_markets", exchange.load_markets)
        if config.SYMBOL not in exchange.markets:
            raise RuntimeError(f"{config.SYMBOL} is not listed on {config.EXCHANGE_ID}.")

        frame = fetch_history(exchange, config.BACKTEST_CANDLES)
        split_date = report_split_date(frame)
        results = evaluate(frame, split_date)
        report(results, split_date)
        return 0
    except Exception as exc:  # noqa: BLE001 - top-level guard, matches daily_trade
        LOGGER.critical("Sweep aborted: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
