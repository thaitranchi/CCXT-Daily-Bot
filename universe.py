"""Survivorship-neutral symbol universe for backtesting.

The obvious way to pick symbols is to list the pairs that exist today. That is
the single largest source of optimism in a crypto backtest, because every pair
still listed in 2026 is a pair that survived. Coins that went to zero are
excluded by construction, and a long-only strategy then looks better than it
would have been if it had been forced to hold them too.

CCXT's ``load_markets`` keeps delisted pairs with ``active = False``, and their
candles remain fetchable, so the biased sample can be replaced with the real
one: every USDT spot pair the venue has ever listed, live or dead.

Two groups are filtered out because they are not comparable directional assets:

* **Leveraged tokens** (``BTCUP``, ``ETHDOWN``, ...) -- these decay to zero by
  design, so including them would poison the result in the opposite direction.
* **Stablecoin and fiat bases** (``EURA``, ``FDUSD``, ...) -- they do not trend.

Selection is deterministic: the universe is sorted, and when capped, symbols are
taken at an even stride so the live/delisted mix is preserved rather than
accidentally skewed by alphabetical order.
"""

from __future__ import annotations

import ccxt

# Bases that are pegged or fiat and carry no directional crypto signal.
NON_DIRECTIONAL_BASES = {
    "AEUR", "AUD", "BRL", "BUSD", "DAI", "EUR", "EURA", "EURS", "EURT",
    "FDUSD", "GBP", "GUSD", "IDRT", "NGN", "PAX", "RUB", "SUSD", "TUSD",
    "TRY", "UAH", "USD1", "USDC", "USDP", "USDS", "XUSD", "ZAR", "BIDR",
    "BKRW", "BVND", "DOGEUP", "PAXG",
}

# Leveraged token suffixes. These are path-dependent products, not spot holdings.
LEVERAGED_SUFFIXES = ("UP", "DOWN", "BULL", "BEAR", "3L", "3S")


def _is_directional(market: dict) -> bool:
    base = market.get("base") or ""
    if not base:
        return False
    if base.upper() in NON_DIRECTIONAL_BASES:
        return False
    if base.upper().endswith(LEVERAGED_SUFFIXES):
        return False
    return True


def build_universe(
    exchange: ccxt.Exchange, max_symbols: int | None = None
) -> list[dict]:
    """Every directional USDT spot pair the venue has ever listed.

    Returns market dicts annotated with ``"delisted"``. Sorted by symbol so the
    result is reproducible across runs, and capped at an even stride when
    ``max_symbols`` is set.
    """
    markets = [
        m
        for m in exchange.markets.values()
        if m.get("spot") and m.get("quote") == "USDT" and _is_directional(m)
    ]
    for m in markets:
        m["delisted"] = not m.get("active")

    markets.sort(key=lambda m: m["symbol"])

    if max_symbols is not None and len(markets) > max_symbols:
        stride = len(markets) / max_symbols
        picked = [markets[int(i * stride)] for i in range(max_symbols)]
        markets = picked
        markets.sort(key=lambda m: m["symbol"])

    return markets


def describe(universe: list[dict]) -> tuple[int, int, int]:
    """Return ``(total, live, delisted)`` for logging."""
    live = sum(1 for m in universe if not m["delisted"])
    return len(universe), live, len(universe) - live
