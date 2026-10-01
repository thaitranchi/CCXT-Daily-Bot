"""Local control-center dashboard for the Layer 1-3 pipeline.

Serves a single static page from ``static/`` and one JSON endpoint,
``/api/snapshot``, which is assembled from the deterministic state this repo
already tracks: the risk limits in :mod:`config`, the latest verdict from
:mod:`risk_engine`, the trade plan from :mod:`daily_trade`, and the OHLCV frame
the chart draws.

Two deliberate constraints:

* **Read-only.** This serves a GET endpoint and a static file. It cannot place,
  cancel, or modify an order. The kill switch is a display of
  ``config.TRADING_HALTED``, not a control of it -- flipping it means editing
  config and restarting, which is a friction that is appropriate for the one
  action that must not be a single misclick. Making this page able to halt
  trading would also mean exposing a write endpoint on the same origin as the
  audit feed, which is the wrong tradeoff for a monitoring tool.
* **Binds to loopback only.** The snapshot contains account equity and positions.
  There is no auth here, so it must not be reachable off-box.

Run: ``python dashboard.py`` then open http://127.0.0.1:8787
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import config
import risk_engine

LOGGER = logging.getLogger("dashboard")

HOST = "127.0.0.1"  # noqa: S104 - deliberate; see module docstring
PORT = 8787
STATIC_DIR = Path(__file__).resolve().parent / "static"

# Candles shipped to the chart. The panel draws ~120 at a legible width, so
# sending more costs payload without adding readable resolution.
CHART_CANDLES = 120


def _equity_snapshot(exchange: Any, live: bool) -> risk_engine.AccountState:
    """Build the account state the guardrails are evaluated against.

    Equity is mark-to-market: quote balance plus the value of any open position.
    Passing the quote balance alone would understate equity the moment a position
    exists, which makes the drawdown breaker fire on the entry that created the
    position rather than on an actual loss.
    """
    base_quote = "USDT"

    if not live:
        equity = config.ACCOUNT_EQUITY_USD
        return risk_engine.AccountState(
            total_equity=equity,
            cash_balance=equity,
            starting_daily_equity=equity,
            current_position_value=0.0,
        )

    try:
        market = exchange.market(config.SYMBOL)
        base_quote = market["quote"]
        balances = exchange.fetch_balance()
        free = float(balances.get("free", {}).get(base_quote, 0) or 0)
        held = float(balances.get("total", {}).get(market["base"], 0) or 0)
        mark = float(exchange.fetch_ticker(config.SYMBOL).get("last", 0) or 0)
    except Exception as exc:  # noqa: BLE001 - the dashboard must render degraded
        LOGGER.warning("Could not read balances (%s); reporting zeros.", exc)
        free = held = mark = 0.0

    position_value = held * mark
    equity = free + position_value
    if equity <= 0:
        equity = config.ACCOUNT_EQUITY_USD

    return risk_engine.AccountState(
        total_equity=equity,
        cash_balance=free,
        starting_daily_equity=equity,
        current_position_value=position_value,
    )


def _candles(exchange: Any) -> list[dict[str, Any]]:
    """Fetch OHLCV for the chart, dropping the still-forming candle.

    The live partial bar is included in ``fetch_ohlcv`` output and would render
    as a candle whose close keeps moving, so it is trimmed here for the same
    reason :func:`daily_trade.evaluate_signal` trims it.
    """
    try:
        raw = exchange.fetch_ohlcv(
            config.SYMBOL, config.TIMEFRAME, limit=CHART_CANDLES + 1
        )
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("Could not fetch candles for the chart: %s", exc)
        return []

    series = [
        {
            "t": int(row[0]),
            "o": float(row[1]),
            "h": float(row[2]),
            "l": float(row[3]),
            "c": float(row[4]),
            "v": float(row[5]),
        }
        for row in raw[:-1]
    ]
    return series


def _guardrail_states(
    engine: risk_engine.RiskExecutionEngine,
) -> list[dict[str, Any]]:
    """Each guardrail's current standing, straight from the engine.

    Delegates to :meth:`RiskExecutionEngine.guardrail_states` rather than
    re-deriving the checks here. A monitoring panel that computed its own copy of
    a risk rule would report the account's status under the panel's idea of the
    limit, which is exactly the sort of second source of truth that lets a real
    breach display as healthy.
    """
    return engine.guardrail_states()  # type: ignore[return-value]


def build_snapshot(demo: bool = False) -> dict[str, Any]:
    """Assemble the full dashboard payload. Never raises.

    With ``demo=True`` the payload is synthesised from fixed values and no
    exchange call is made, so the UI can be developed and reviewed without
    credentials or network. Every panel renders from this one shape, so the demo
    path exercises the real rendering code rather than a separate mock UI.
    """
    """Assemble the full dashboard payload. Never raises."""
    import daily_trade

    snapshot: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "symbol": config.SYMBOL,
        "exchange": config.EXCHANGE_ID,
        "paper_trading": config.PAPER_TRADING,
        "trading_halted": config.TRADING_HALTED,
        "candles": [],
        "guardrails": [],
        "account": None,
        "recent_verdicts": [],
        "news": [],
        "latest_extraction": None,
        "signals": [],
        "engine_error": None,
    }

    if demo:
        snapshot.update(_demo_payload())
        return snapshot

    try:
        exchange = daily_trade.build_exchange()
        exchange.load_markets()
        live = not config.PAPER_TRADING
    except Exception as exc:  # noqa: BLE001 - render degraded rather than 500
        snapshot["engine_error"] = f"exchange unavailable: {exc}"
        return snapshot

    try:
        account = _equity_snapshot(exchange, live)
        engine = risk_engine.RiskExecutionEngine(account)
        snapshot["account"] = account.model_dump()
        snapshot["guardrails"] = _guardrail_states(engine)
    except Exception as exc:  # noqa: BLE001
        snapshot["engine_error"] = f"account snapshot failed: {exc}"

    try:
        snapshot["candles"] = _candles(exchange)
    except Exception as exc:  # noqa: BLE001
        snapshot["engine_error"] = f"candle fetch failed: {exc}"

    return snapshot


def _demo_candles(count: int = 120) -> list[dict[str, Any]]:
    """Deterministic synthetic OHLCV, so the chart renders identically every run.

    Uses a fixed-seed LCG rather than ``random`` so a screenshot taken now matches
    one taken after a restart, which makes visual review of a chart change
    meaningful.
    """
    now_ms = int(datetime(2026, 3, 14, tzinfo=timezone.utc).timestamp() * 1000)
    step = 86_400_000
    price, seed = 182.0, 7
    out: list[dict[str, Any]] = []
    for i in range(count):
        seed = (1103515245 * seed + 12345) % (2**31)
        drift = ((seed / 2**31) - 0.46) * 4.2
        price = max(1.0, price + drift)
        high = price + abs(drift) * 0.9 + 0.6
        low = price - abs(drift) * 0.9 - 0.6
        out.append({
            "t": now_ms - (count - i) * step,
            "o": round(price - drift / 2, 4),
            "h": round(high, 4),
            "l": round(low, 4),
            "c": round(price, 4),
            "v": round(1_000_000 + (seed % 900_000), 2),
        })
    return out


def _demo_payload() -> dict[str, Any]:
    """A representative snapshot: one live signal, one rejection, one breach.

    Deliberately shows a *failing* guardrail and a rejected order. A demo where
    every panel is green cannot demonstrate the states that matter most in review,
    and those are the states a dashboard exists to make visible.
    """
    # The day opened at 103,500 and equity is now 100,000: a 3.38% loss, which is
    # past the 3% limit. That makes the drawdown guard report FAIL from its own
    # arithmetic, so the demo exercises the panel's breach styling without any
    # state being overridden to fake it.
    account = risk_engine.AccountState(
        total_equity=100_000.00,
        cash_balance=63_000.00,
        starting_daily_equity=103_500.00,
        current_position_value=37_000.00,
    )
    engine = risk_engine.RiskExecutionEngine(account)

    signal = risk_engine.OrderSignal(
        ticker=config.SYMBOL,
        action=risk_engine.SignalAction.BUY,
        quantity=195.0,
        entry_price=189.74,
        stop_loss_price=185.12,
        conviction_score=0.85,
    )

    # The acceptance is replayed against a healthy account: it happened earlier in
    # the session, before the day went on to breach its drawdown limit. Evaluating
    # it against the current account would reject it, which is correct behaviour
    # but would leave the audit log showing one reason twice and hide the two
    # distinct rejection modes the panel exists to show.
    earlier = account.model_copy(update={"starting_daily_equity": 100_000.0})
    verdict = risk_engine.RiskExecutionEngine(earlier).process_signal(signal)

    # Deliberately oversized: risks 4.94% against a 1% budget.
    oversized = risk_engine.OrderSignal(
        ticker=config.SYMBOL,
        action=risk_engine.SignalAction.BUY,
        quantity=520.0,
        entry_price=190.10,
        stop_loss_price=180.60,
        conviction_score=0.91,
    )
    rejected = engine.process_signal(oversized)

    def audit(sig: risk_engine.OrderSignal, resp: risk_engine.OrderResponse) -> dict[str, Any]:
        return {
            "ticker": sig.ticker,
            "action": sig.action.value,
            "quantity": sig.quantity,
            "accepted": resp.status is risk_engine.ExecutionStatus.ACCEPTED,
            "reason": resp.reason,
            "timestamp": resp.timestamp.isoformat(),
        }

    now = datetime.now(timezone.utc)
    # Guards come from the real engine, so the demo cannot drift from the rules
    # it is illustrating. The account above starts the day at 98,760 against
    # 100,000 of equity, which is a loss, so the drawdown guard reports a real
    # FAIL against its own logic -- no state is forced here.
    guards = _guardrail_states(engine)

    return {
        "account": account.model_dump(),
        "guardrails": guards,
        "candles": _demo_candles(),
        "signals": [
            {
                "ticker": signal.ticker,
                "action": signal.action.value,
                "quantity": signal.quantity,
                "entry_price": signal.entry_price,
                "stop_loss_price": signal.stop_loss_price,
                "notional": signal.notional(),
                "conviction_score": signal.conviction_score,
                "rationale": (
                    "Momentum and sentiment aligned: price above the rising 20-SMA "
                    "with a constructive news skew, so the directional read is "
                    "confirmed. Size is ATR-derived, not fixed."
                ),
            }
        ],
        "latest_extraction": {
            "ticker": config.SYMBOL,
            "sentiment_score": 0.34,
            "confidence_score": 0.78,
            "event_type": "MACRO_FED",
            "macro_event_risk": "MEDIUM",
        },
        "news": [
            {
                "headline": "Fed minutes signal patience on cuts; risk assets extend gains",
                "published_at": (now.replace(microsecond=0) - timedelta(minutes=6)).isoformat(),
                "event_type": "MACRO_FED",
                "ticker": "MACRO",
                "extraction": {
                    "ticker": "MACRO",
                    "sentiment_score": 0.41,
                    "confidence_score": 0.72,
                    "event_type": "MACRO_FED",
                    "macro_event_risk": "MEDIUM",
                },
            },
            {
                "headline": "8-K: quarterly results ahead of consensus, guidance reaffirmed",
                "published_at": (now.replace(microsecond=0) - timedelta(minutes=31)).isoformat(),
                "event_type": "SEC_FILING",
                "ticker": config.SYMBOL,
                "extraction": {
                    "ticker": config.SYMBOL,
                    "sentiment_score": 0.62,
                    "confidence_score": 0.88,
                    "event_type": "SEC_FILING",
                    "macro_event_risk": "LOW",
                },
            },
            {
                "headline": "Analyst downgrade cites margin compression in industrial segment",
                "published_at": (now.replace(microsecond=0) - timedelta(minutes=58)).isoformat(),
                "event_type": "ANALYST_NOTE",
                "ticker": config.SYMBOL,
                "extraction": {
                    "ticker": config.SYMBOL,
                    "sentiment_score": -0.47,
                    "confidence_score": 0.69,
                    "event_type": "ANALYST_NOTE",
                    "macro_event_risk": "LOW",
                },
            },
        ],
        "recent_verdicts": [
            audit(signal, verdict),
            audit(oversized, risk_engine.RiskExecutionEngine(earlier).process_signal(oversized)),
            audit(oversized, rejected),
        ],
        "generated_at": now.isoformat(),
    }


class Handler(BaseHTTPRequestHandler):
    """Static files plus one JSON endpoint. GET only."""

    server_version = "CCXTDashboard/1.0"

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        path = self.path.split("?")[0]
        if path == "/api/snapshot":
            # ?demo=1 renders synthetic data with no exchange call.
            self._serve_json(build_snapshot(demo="demo=1" in self.path))
            return
        self._serve_static(path)

    def _serve_json(self, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        # The page is same-origin and must not be cached, or a halted switch or a
        # tripped breaker would linger in the browser after it cleared.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _serve_static(self, path: str) -> None:
        name = "index.html" if path in ("/", "") else path.lstrip("/")
        target = (STATIC_DIR / name).resolve()

        # Path traversal guard: resolve first, then confirm the result is still
        # inside STATIC_DIR. Checking the input string for ".." is not enough.
        if not target.is_relative_to(STATIC_DIR) or not target.is_file():
            self.send_error(404, "Not found")
            return

        types = {".html": "text/html", ".css": "text/css", ".js": "text/javascript"}
        ctype = types.get(target.suffix, "application/octet-stream")
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: Any) -> None:
        LOGGER.debug(fmt, *args)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-8s %(message)s"
    )
    if not STATIC_DIR.is_dir():
        LOGGER.error("Static directory missing: %s", STATIC_DIR)
        return 1

    server = ThreadingHTTPServer((HOST, PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    LOGGER.info(
        "Dashboard on http://%s:%d -- loopback only, no auth, read-only.",
        HOST,
        PORT,
    )
    LOGGER.info("Press Ctrl+C to stop.")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        LOGGER.info("Shutting down.")
        server.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())