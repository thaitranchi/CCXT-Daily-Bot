"""Alternative entry signals for the CCXT-Daily-Bot backtest harness.

Each function takes the candle frame and a :class:`~backtest.StrategyParams`, and
returns a boolean Series aligned to the frame's index that is True on the close
of a candle where a new position should be opened.

The variants are deliberately few and each has a stated economic reason to
exist. That restraint is the point: testing hundreds of signal ideas and
reporting the winner guarantees a good-looking backtest that does not survive.
These eight were specified before looking at any results.

The baseline is the live bot's rule -- close above a simple moving average --
which fires on *every* candle spent above the average. That is the structural
weakness the variants target: it produced ~60 entries a year on 3-day average
holds, paying fees continuously to maintain a trend-following position.
``fresh_cross`` is the minimal fix; the rest add regime, volatility or breakout
confirmation.

Import cycle note: this module knows nothing about :mod:`backtest`, which imports
it. Signal functions therefore cannot reach backtest internals.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable

import pandas as pd

if TYPE_CHECKING:
    from backtest import StrategyParams


def _sma(frame: pd.DataFrame, period: int) -> pd.Series:
    return frame["close"].rolling(window=period).mean()


def _rsi(frame: pd.DataFrame, period: int) -> pd.Series:
    """Cutler's RSI: simple rolling means of gains and losses.

    Wilder's smoothing is the more common form, but the simpler average is
    adequate here and keeps the series free of recursive warmup artefacts.
    """
    delta = frame["close"].diff()
    gains = delta.clip(lower=0).rolling(window=period).mean()
    losses = (-delta).clip(lower=0).rolling(window=period).mean()
    rs = gains / losses.replace(0, float("nan"))
    return (100 - 100 / (1 + rs)).fillna(100.0).where(losses.notna())


def _atr_pct(frame: pd.DataFrame, period: int) -> pd.Series:
    """Average true range expressed as a fraction of close."""
    prev_close = frame["close"].shift(1)
    true_range = pd.concat(
        [
            frame["high"] - frame["low"],
            (frame["high"] - prev_close).abs(),
            (frame["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return true_range.rolling(window=period).mean() / frame["close"]


def baseline(frame: pd.DataFrame, params: "StrategyParams") -> pd.Series:
    """The live rule: close above the SMA, on every candle spent above it."""
    return frame["close"] > _sma(frame, params.sma_period)


def fresh_cross(frame: pd.DataFrame, params: "StrategyParams") -> pd.Series:
    """Fire only on the candle where price crosses up through the SMA.

    Cuts entries from "every day above the average" to "once per regime change",
    which is where nearly all of the live bot's fee drag came from.
    """
    above = frame["close"] > _sma(frame, params.sma_period)
    return above & ~above.shift(1, fill_value=False)


def cross_regime(frame: pd.DataFrame, params: "StrategyParams") -> pd.Series:
    """Fresh cross, but only while the long-term average is rising and above it.

    Keeps the reduced entry count of ``fresh_cross`` and additionally refuses to
    buy into a falling long-term trend.
    """
    fast = _sma(frame, params.sma_period)
    slow = _sma(frame, params.regime_period)
    cross = (frame["close"] > fast) & ~(frame["close"] > fast).shift(1, fill_value=False)
    return cross & (fast > slow) & (slow.diff(5) > 0)


def regime_trend(frame: pd.DataFrame, params: "StrategyParams") -> pd.Series:
    """Stay in while price leads a rising long-term average, not just at the cross."""
    fast = _sma(frame, params.sma_period)
    slow = _sma(frame, params.regime_period)
    return (frame["close"] > fast) & (fast > slow) & (slow.diff(5) > 0)


def breakout(frame: pd.DataFrame, params: "StrategyParams") -> pd.Series:
    """Close above the prior N-candle high -- a Donchian/turtle entry."""
    prior_high = frame["high"].rolling(window=params.breakout_period).max().shift(1)
    return frame["close"] > prior_high


def breakout_regime(
    frame: pd.DataFrame, params: "StrategyParams"
) -> pd.Series:
    """Donchian breakout, confirmed by the long-term trend.

    Breakouts in a downtrend are the classic trend-following failure mode; the
    regime filter is the cheapest defence against it.
    """
    prior_high = frame["high"].rolling(window=params.breakout_period).max().shift(1)
    slow = _sma(frame, params.regime_period)
    return (frame["close"] > prior_high) & (frame["close"] > slow) & (slow.diff(5) > 0)


def rsi_trend(frame: pd.DataFrame, params: "StrategyParams") -> pd.Series:
    """Above-average price with momentum confirmation above 50."""
    return (frame["close"] > _sma(frame, params.sma_period)) & (
        _rsi(frame, params.rsi_period) > params.rsi_threshold
    )


def cross_volatility(
    frame: pd.DataFrame, params: "StrategyParams"
) -> pd.Series:
    """Fresh cross, skipping periods too quiet to reach the target.

    A 4% target needs room to move. When realised daily volatility is below the
    threshold the bracket cannot fill before the stop does, so those entries are
    close to coin flips that still pay full fees.
    """
    above = frame["close"] > _sma(frame, params.sma_period)
    cross = above & ~above.shift(1, fill_value=False)
    return cross & (_atr_pct(frame, params.atr_period) > params.atr_threshold)


SignalFn = Callable[[pd.DataFrame, "StrategyParams"], pd.Series]

SIGNALS: dict[str, SignalFn] = {
    "baseline": baseline,
    "fresh_cross": fresh_cross,
    "cross_regime": cross_regime,
    "regime_trend": regime_trend,
    "breakout": breakout,
    "breakout_regime": breakout_regime,
    "rsi_trend": rsi_trend,
    "cross_volatility": cross_volatility,
}

# Extra candles each signal needs before its first usable value.
WARMUP: dict[str, Callable[["StrategyParams"], int]] = {
    "baseline": lambda p: p.sma_period,
    "fresh_cross": lambda p: p.sma_period,
    "cross_regime": lambda p: max(p.sma_period, p.regime_period) + 5,
    "regime_trend": lambda p: max(p.sma_period, p.regime_period) + 5,
    "breakout": lambda p: p.breakout_period + 1,
    "breakout_regime": lambda p: max(p.breakout_period + 1, p.regime_period) + 5,
    "rsi_trend": lambda p: max(p.sma_period, p.rsi_period),
    "cross_volatility": lambda p: max(p.sma_period, p.atr_period),
}


def warmup(name: str, params: "StrategyParams") -> int:
    """First index at which ``name`` can produce a meaningful value."""
    if name not in WARMUP:
        raise KeyError(f"Unknown signal {name!r}; known: {sorted(SIGNALS)}")
    return WARMUP[name](params)


def get_signal(name: str) -> SignalFn:
    if name not in SIGNALS:
        raise KeyError(f"Unknown signal {name!r}; known: {sorted(SIGNALS)}")
    return SIGNALS[name]
