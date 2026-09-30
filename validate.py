"""Robustness validation: signal variants and symbols, judged out-of-sample.

``sweep.py`` searched parameters of one signal on one asset and found only
curve-fit. This script attacks the two remaining explanations for that result --
wrong parameters *and* wrong asset -- with the discipline the first one lacked.

Two questions, one harness:

1. **Does a different entry rule help?** Eight signals, each with a stated reason
   to exist, specified before any results were seen.
2. **Does it hold across assets?** Every signal is run on twelve independent
   pairs, not just BTC.

Breadth across symbols is the real test. A single backtest can be made to look
good by luck; a rule that is profitable on 10 of 12 unrelated instruments is
behaving like an edge, and one that is profitable on 2 of 12 is noise that
happened to be measured twelve times. So the headline number here is *how many
symbols stayed profitable out-of-sample*, not the best pooled return.

The in-sample/out-of-sample boundary is an absolute date, so every symbol is
graded on the same calendar window and the comparison is like-for-like.

Multiple-testing caveat, stated up front: ``configs tested`` configurations are
evaluated against the same out-of-sample window. Some will look profitable by
chance, and the script reports how many, precisely so a stray winner is not
mistaken for a discovery.

Read-only: no orders, no credentials.
"""

from __future__ import annotations

import itertools
from typing import Any

import ccxt
import pandas as pd

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
from signals import SIGNALS
from universe import build_universe, describe

# Cap on pairs loaded, to keep runtime bounded. The stride in build_universe
# preserves the live/delisted ratio, so this is a fair sample of the full
# universe rather than an alphabetical accident.
MAX_SYMBOLS = 120

# Absolute boundary, so every symbol is graded on the same calendar window.
# Roughly the last two years are held out from selection.
OOS_START = "2024-07-01"

# Compact grid: enough to let each signal express itself, small enough that the
# multiple-testing penalty stays meaningful.
SMA_PERIODS = [20, 50]
STOP_PCTS = [0.02, 0.04]
REWARD_RISK = 2.0

REGIME_PERIOD = 100
BREAKOUT_PERIOD = 20

# A signal must be profitable on at least this share of symbols before its
# out-of-sample result is called anything more than chance.
MIN_BREADTH = 0.6


def candidate_params() -> list[StrategyParams]:
    """Every (signal, parameter) combination under test."""
    candidates = []
    for signal, sma, stop in itertools.product(SIGNALS, SMA_PERIODS, STOP_PCTS):
        candidates.append(
            StrategyParams(
                sma_period=sma,
                stop_pct=stop,
                target_pct=round(stop * REWARD_RISK, 6),
                notional=config.TRADE_SIZE_USD,
                fee_pct=config.TAKER_FEE_PCT,
                signal=signal,
                regime_period=REGIME_PERIOD,
                breakout_period=BREAKOUT_PERIOD,
            )
        )
    return candidates


def load_frames(exchange: ccxt.Exchange) -> dict[str, pd.DataFrame]:
    """Load the survivorship-neutral universe, skipping any pair without history.

    Every directional USDT pair the venue has ever listed, live *and* delisted,
    capped at an even stride. Filtering to pairs that still exist in 2026 would
    quietly remove every coin that went to zero and make every long-only result
    look better than it was.
    """
    universe = build_universe(exchange, max_symbols=MAX_SYMBOLS)
    total, live, dead = describe(universe)
    LOGGER.info(
        "Universe: %d directional USDT pairs (%d live, %d delisted)", total, live, dead
    )

    frames: dict[str, pd.DataFrame] = {}
    for market in universe:
        symbol = market["symbol"]
        try:
            frames[symbol] = fetch_history(exchange, config.BACKTEST_CANDLES, symbol)
        except Exception as exc:  # noqa: BLE001 - one bad pair must not abort
            LOGGER.warning("%s failed to load (%s); skipping.", symbol, exc)
    return frames


def evaluate(frames: dict[str, pd.DataFrame]) -> list[dict[str, Any]]:
    """Score every candidate on every symbol, split at ``OOS_START``."""
    results = []
    for params in candidate_params():
        per_symbol = []
        for symbol, frame in frames.items():
            trades = run_backtest(frame, params, symbol=symbol)
            in_sample = compute_stats(filter_by_date(trades, None, OOS_START), params)
            out_sample = compute_stats(filter_by_date(trades, OOS_START, None), params)
            per_symbol.append(
                {
                    "symbol": symbol,
                    "is": in_sample,
                    "oos": out_sample,
                    "is_profitable": in_sample["total_r"] > 0,
                    "oos_profitable": out_sample["total_r"] > 0,
                }
            )

        results.append(
            {
                "params": params,
                "per_symbol": per_symbol,
                "pooled_is_r": sum(r["is"]["total_r"] for r in per_symbol),
                "pooled_oos_r": sum(r["oos"]["total_r"] for r in per_symbol),
                "pooled_oos_net": sum(r["oos"]["total_net"] for r in per_symbol),
                "is_wins": sum(1 for r in per_symbol if r["is_profitable"]),
                "oos_wins": sum(1 for r in per_symbol if r["oos_profitable"]),
                "oos_trades": sum(r["oos"]["trades"] for r in per_symbol),
                "is_trades": sum(r["is"]["trades"] for r in per_symbol),
            }
        )
    return results


def report(results: list[dict[str, Any]], symbol_count: int) -> None:
    """Print breadth-first results: how many symbols held up, per signal."""
    LOGGER.info("=" * 78)
    LOGGER.info(
        "VALIDATION | %s | %s", config.EXCHANGE_ID, config.TIMEFRAME
    )
    LOGGER.info("Signals tested        : %d", len(SIGNALS))
    LOGGER.info("Configurations tested : %d", len(results))
    LOGGER.info("In-sample ends before : %s", OOS_START)
    LOGGER.info("Out-of-sample starts  : %s (held out from selection)", OOS_START)
    LOGGER.info("=" * 78)

    _report_headline(results, symbol_count)
    _report_per_signal(results, symbol_count)
    _report_best_configs(results, symbol_count)
    _report_baseline(results, symbol_count)
    _report_verdict(results, symbol_count)


def _report_headline(results: list[dict[str, Any]], symbol_count: int) -> None:
    LOGGER.info("-" * 78)
    LOGGER.info("HEADLINE")
    is_pos = sum(1 for r in results if r["is_wins"] > symbol_count / 2)
    oos_pos = sum(1 for r in results if r["oos_wins"] > symbol_count / 2)
    LOGGER.info(
        "  Configurations profitable on a MAJORITY of symbols in-sample     : %d / %d",
        is_pos,
        len(results),
    )
    LOGGER.info(
        "  Configurations profitable on a MAJORITY of symbols out-of-sample : %d / %d",
        oos_pos,
        len(results),
    )
    LOGGER.info(
        "  Breadth threshold for a credible claim: %d of %d symbols", 
        int(symbol_count * MIN_BREADTH),
        symbol_count,
    )


def _report_per_signal(results: list[dict[str, Any]], symbol_count: int) -> None:
    LOGGER.info("-" * 78)
    LOGGER.info("PER SIGNAL (pooled across symbols; R is not strictly comparable")
    LOGGER.info("across assets with different volatility, so trust breadth first)")
    LOGGER.info(
        "%-17s %-15s %8s %8s %9s %9s %8s",
        "SIGNAL", "PARAMS", "IS n", "OOS n", "IS sym", "OOS sym", "OOS R",
    )
    for signal in SIGNALS:
        rows = [r for r in results if r["params"].signal == signal]
        if not rows:
            continue
        for params_label, subset in _group_by_label(rows):
            is_sym = sum(r["is_wins"] for r in subset)
            oos_sym = sum(r["oos_wins"] for r in subset)
            LOGGER.info(
                "%-17s %-15s %8d %8d %5d/%-3d %5d/%-3d %+8.1f",
                signal,
                params_label,
                sum(r["is_trades"] for r in subset),
                sum(r["oos_trades"] for r in subset),
                is_sym,
                symbol_count,
                oos_sym,
                symbol_count,
                sum(r["pooled_oos_r"] for r in subset),
            )


def _group_by_label(rows: list[dict[str, Any]]) -> list[tuple[str, list[dict[str, Any]]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        p = row["params"]
        grouped.setdefault(f"SMA{p.sma_period} {p.stop_pct:.0%}", []).append(row)
    return sorted(grouped.items())


def _report_best_configs(
    results: list[dict[str, Any]], symbol_count: int
) -> None:
    LOGGER.info("-" * 78)
    LOGGER.info("TOP 10 BY OUT-OF-SAMPLE BREADTH (symbols profitable, then pooled R)")
    ranked = sorted(
        results, key=lambda r: (r["oos_wins"], r["pooled_oos_r"]), reverse=True
    )[:10]
    LOGGER.info(
        "%-34s %7s %8s %9s %9s",
        "CONFIG", "OOS n", "OOS sym", "OOS R", "OOS USDT",
    )
    for row in ranked:
        LOGGER.info(
            "%-34s %7d %6d/%-3d %+9.1f %+9.2f",
            row["params"].label,
            row["oos_trades"],
            row["oos_wins"],
            symbol_count,
            row["pooled_oos_r"],
            row["pooled_oos_net"],
        )


def _report_baseline(results: list[dict[str, Any]], symbol_count: int) -> None:
    LOGGER.info("-" * 78)
    LOGGER.info("BASELINE COMPARISON (the live rule: close above SMA, every candle)")
    for row in results:
        if row["params"].signal != "baseline":
            continue
        p = row["params"]
        if p.sma_period != config.SMA_PERIOD or abs(p.stop_pct - config.STOP_LOSS_PCT) > 1e-9:
            continue
        LOGGER.info(
            "  Live config %s: %d trades in-sample, %d out",
            p.label,
            row["is_trades"],
            row["oos_trades"],
        )
        LOGGER.info(
            "    profitable on %d/%d symbols in-sample, %d/%d out-of-sample",
            row["is_wins"], symbol_count, row["oos_wins"], symbol_count,
        )
        LOGGER.info(
            "    pooled %+.1f R in-sample, %+.1f R out-of-sample",
            row["pooled_is_r"], row["pooled_oos_r"],
        )


def _report_verdict(results: list[dict[str, Any]], symbol_count: int) -> None:
    survivors = [
        r
        for r in results
        if r["oos_wins"] >= symbol_count * MIN_BREADTH and r["oos_trades"] >= 30
    ]
    LOGGER.info("-" * 78)
    LOGGER.info("VERDICT")
    if not survivors:
        LOGGER.info(
            "  No configuration stayed profitable on %d of %d symbols out-of-sample.",
            int(symbol_count * MIN_BREADTH), symbol_count,
        )
        LOGGER.info(
            "  Across %d configurations, isolated winners of this kind are expected"
            % len(results)
        )
        LOGGER.info(
            "  by chance. None of them is evidence of an edge worth trading."
        )
        return
    for row in survivors:
        LOGGER.info(
            "  Candidate worth further work: %s (%d/%d symbols, %+.1f R, %d trades)",
            row["params"].label,
            row["oos_wins"], symbol_count, row["pooled_oos_r"], row["oos_trades"],
        )
    LOGGER.info(
        "  Still not proof: these were chosen with the out-of-sample window in view."
    )
    LOGGER.info(
        "  A genuine holdout (live forward-testing, or a window never used here)"
    )
    LOGGER.info(
        "  is required before risking money."
    )


def main() -> int:
    setup_logging()
    try:
        LOGGER.info("=" * 78)
        LOGGER.info("CCXT-Daily-Bot VALIDATION (read-only, no orders)")
        LOGGER.info("=" * 78)

        exchange = build_exchange()
        retry_call("load_markets", exchange.load_markets)
        frames = load_frames(exchange)
        if not frames:
            raise RuntimeError("No symbols could be loaded.")

        results = evaluate(frames)
        report(results, len(frames))
        return 0
    except Exception as exc:  # noqa: BLE001 - top-level guard, matches daily_trade
        LOGGER.critical("Validation aborted: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
