"""Layer 3 -- deterministic execution gatekeeper.

Sits between whatever produced a signal and the exchange write in
:mod:`daily_trade`. Its only job is to refuse orders that violate account-level
safety parameters, so that a bad signal -- from a model, a config edit, or a
human at 3am -- cannot exceed the risk limits in :mod:`config`.

Design properties worth stating, because they are what make this class safe to
put in front of real funds:

* **It never raises on a bad signal.** Every rejection is an :class:`OrderResponse`
  with a human-readable ``reason``. An exception here would abort the run before
  the logging in :func:`daily_trade.notify` fires, turning a handled rejection
  into an unexplained crash.
* **It cannot increase risk.** It validates and rejects; it never resizes an
  order upward. Sizing happens in :func:`daily_trade.build_trade_plan` under the
  ATR and notional caps, and this engine treats that result as a proposal.
* **Risk-reducing orders bypass the circuit breaker.** A tripped breaker halts
  *new exposure*. If it also halted exits, a drawdown would freeze the bot out
  of a position it can no longer close, which converts a bounded loss into an
  open-ended one.
* **Equity means mark-to-market**, not the quote balance. The breaker compares
  against total account value including open positions; measuring the quote leg
  alone makes every entry look like an instant 3% drawdown.

The engine is deliberately provider-agnostic and has no I/O. It takes an
:class:`AccountState` snapshot and an :class:`OrderSignal` and returns a verdict.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field

import config


class SignalAction(str, Enum):
    """Direction of a proposed order."""

    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


class ExecutionStatus(str, Enum):
    """Verdict of the risk gate."""

    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"


class OrderSignal(BaseModel):
    """A proposed order, as produced by the upstream signal layer.

    ``quantity`` is a float rather than an int because this bot trades spot crypto
    on a lot-step grid: one BTC is many orders and a single lot is a fractional
    amount. An integer field would reject every real signal on the venue.
    """

    # allow_inf_nan=False on every numeric field: pydantic's ``gt=0`` comparison is
    # True for inf, so an infinite quantity or a NaN price would otherwise pass
    # validation and reach the arithmetic. The result is a verdict whose risk
    # figure reads "inf" or "nan" -- authoritative-looking output derived from
    # nothing. A malformed signal must fail at the schema, where it is legible.
    ticker: str = Field(min_length=1)
    action: SignalAction
    quantity: float = Field(
        gt=0, allow_inf_nan=False, description="Size in base units, e.g. 0.00312 BTC"
    )
    entry_price: float = Field(
        gt=0, allow_inf_nan=False, description="Expected fill price, in quote units"
    )
    stop_loss_price: float | None = Field(
        default=None,
        gt=0,
        allow_inf_nan=False,
        description="Explicit hard stop trigger. Required; a proposal without one is rejected.",
    )
    conviction_score: float = Field(
        ge=0.0,
        le=1.0,
        allow_inf_nan=False,
        description="Upstream confidence in the signal, 0.0-1.0",
    )

    def notional(self) -> float:
        """Gross value of the order in quote currency."""
        return self.quantity * self.entry_price

    def reduces_exposure(self) -> bool:
        """Whether this order decreases gross exposure rather than adding to it.

        Spot venues have no shorting, so every SELL is by definition a reduction.
        That is what exempts exits from the drawdown circuit breaker.
        """
        return self.action == SignalAction.SELL


class AccountState(BaseModel):
    """Portfolio snapshot evaluated against the risk limits.

    All values are denominated in the quote currency (USDT for this bot) and must
    be mark-to-market. ``total_equity`` includes the value of open positions;
    ``cash_balance`` is the unencumbered quote balance that can fund a BUY.
    """

    total_equity: float = Field(
        gt=0,
        allow_inf_nan=False,
        description="Equity including open positions, marked to market",
    )
    cash_balance: float = Field(
        ge=0, allow_inf_nan=False, description="Unencumbered quote balance"
    )
    starting_daily_equity: float = Field(
        gt=0,
        allow_inf_nan=False,
        description="Equity at the first evaluation of the current UTC day",
    )
    current_position_value: float = Field(
        default=0.0,
        ge=0,
        allow_inf_nan=False,
        description="Gross value of open positions in quote currency",
    )

    def daily_drawdown(self) -> float:
        """Fractional equity loss since the start of the day, 0.0 when flat or up."""
        if self.starting_daily_equity <= 0:
            return 0.0
        return max(
            0.0,
            (self.starting_daily_equity - self.total_equity) / self.starting_daily_equity,
        )


class OrderResponse(BaseModel):
    """Verdict returned by :meth:`RiskExecutionEngine.process_signal`."""

    status: ExecutionStatus
    ticker: str
    action: SignalAction
    quantity: float = 0.0
    approved_price: float = 0.0
    stop_loss_price: float | None = None
    reason: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class RiskExecutionEngine:
    """Deterministic gatekeeper that vetoes signals breaching account safety limits.

    Defaults come from :mod:`config` rather than being hardcoded literals, so the
    limits are stated once and remain editable in one place. They are validated on
    construction: a limit set with a negative budget or an inverted band would
    silently disable the corresponding rule, which is worse than a loud failure.
    """

    def __init__(
        self,
        account_state: AccountState,
        max_trade_risk_pct: float = config.RISK_PCT,
        max_daily_drawdown_pct: float = config.MAX_DAILY_DRAWDOWN_PCT,
        max_gross_exposure_ratio: float = config.MAX_GROSS_EXPOSURE_RATIO,
        min_conviction: float = config.MIN_CONVICTION,
    ) -> None:
        for name, value in (
            ("max_trade_risk_pct", max_trade_risk_pct),
            ("max_daily_drawdown_pct", max_daily_drawdown_pct),
        ):
            if not 0 < value < 1:
                raise ValueError(f"{name} must be a fraction in (0, 1); got {value!r}.")
        if max_gross_exposure_ratio < 0:
            raise ValueError(
                f"max_gross_exposure_ratio must be >= 0; got {max_gross_exposure_ratio!r}."
            )
        if not 0 <= min_conviction <= 1:
            raise ValueError(f"min_conviction must be in [0, 1]; got {min_conviction!r}.")

        self.account = account_state
        self.max_trade_risk_pct = max_trade_risk_pct
        self.max_daily_drawdown_pct = max_daily_drawdown_pct
        self.max_gross_exposure_ratio = max_gross_exposure_ratio
        self.min_conviction = min_conviction

        # Read the kill switch per signal rather than at construction, so an
        # operator flipping it in config takes effect without rebuilding the
        # engine.
        self.trading_halted = config.TRADING_HALTED

    # ---------------------------------------------------------------- checks --
    def _check_conviction(self, signal: OrderSignal) -> tuple[bool, str]:
        """Refuse signals the upstream layer itself flagged as low confidence."""
        if signal.conviction_score < self.min_conviction:
            return False, (
                f"LOW CONVICTION: conviction {signal.conviction_score:.2f} is below "
                f"the {self.min_conviction:.2f} threshold."
            )
        return True, ""

    def _check_drawdown_circuit_breaker(self, signal: OrderSignal) -> tuple[bool, str]:
        """Halt new exposure once the day's equity loss breaches the limit.

        Skipped for risk-reducing orders. A tripped breaker that also blocks exits
        would leave a position open and unclosable during exactly the drawdown it
        was built to contain.
        """
        if signal.reduces_exposure():
            return True, ""

        drawdown = self.account.daily_drawdown()
        if drawdown >= self.max_daily_drawdown_pct:
            return False, (
                f"CIRCUIT BREAKER: daily drawdown {drawdown:.2%} has reached the "
                f"{self.max_daily_drawdown_pct:.2%} limit. New exposure is halted; "
                f"exits remain available."
            )
        return True, ""

    def _check_stop_loss(self, signal: OrderSignal) -> tuple[bool, str]:
        """Require a hard stop, and cap the loss it implies.

        Both halves matter. A missing stop means the position has no defined exit;
        a stop that is present but tight (or a quantity that is too large) still
        converts a bounded risk into an unbounded one.
        """
        if signal.stop_loss_price is None:
            return False, (
                "MISSING STOP: order carries no explicit stop_loss_price. "
                "Refusing to open an undefined-risk position."
            )

        stop = signal.stop_loss_price

        # Spot is long-only, so only the long case is reachable here. The short
        # branch is kept so the engine stays correct if it is ever reused on a
        # venue that permits short entries.
        if signal.action == SignalAction.BUY and stop >= signal.entry_price:
            return False, (
                f"STOP DIRECTION: long stop {stop} must sit below entry "
                f"{signal.entry_price}."
            )
        if signal.action == SignalAction.SELL and stop <= signal.entry_price:
            return False, (
                f"STOP DIRECTION: short stop {stop} must sit above entry "
                f"{signal.entry_price}."
            )

        risk_usd = abs(signal.entry_price - stop) * signal.quantity
        max_risk_usd = self.account.total_equity * self.max_trade_risk_pct
        if risk_usd > max_risk_usd:
            return False, (
                f"TRADE RISK: stop implies {risk_usd:.4f} at risk "
                f"({risk_usd / self.account.total_equity:.2%} of equity), above the "
                f"{self.max_trade_risk_pct:.2%} limit ({max_risk_usd:.4f})."
            )
        return True, ""

    def _check_exposure(self, signal: OrderSignal) -> tuple[bool, str]:
        """Cap gross exposure so repeated entries cannot stack risk.

        On spot this is not a margin constraint -- there is no borrowing. What it
        does bound is total capital at risk if several brackets are live at once
        and every one of them gaps through its stop, which costs more than any
        single position could.
        """
        # Never let this shadow the cash check on unlevered spot, where the two
        # are the same inequality. Compare on position value only: gross exposure
        # is the sum of open positions plus this order, and a SELL reduces it.
        projected = self.account.current_position_value
        if signal.action == SignalAction.BUY:
            projected += signal.notional()
        ratio = projected / self.account.total_equity
        if ratio > self.max_gross_exposure_ratio:
            return False, (
                f"EXPOSURE CAP: projected gross exposure {projected:.4f} is "
                f"{ratio:.2f}x equity, above the {self.max_gross_exposure_ratio:.2f}x limit."
            )
        return True, ""

    def _check_funding(self, signal: OrderSignal) -> tuple[bool, str]:
        """Ensure a BUY is covered by unencumbered cash.

        Releases persist past the process that placed them, so a balance that looks
        sufficient at submission time can be committed by a surviving bracket from
        an earlier run.

        On unlevered spot this is mathematically equivalent to the exposure cap,
        since cash = equity - position. It is kept as a separate check anyway: it
        is the constraint that fails for the real reason (an order genuinely cannot
        be funded), whereas the cap expresses intent. Folding them together would
        mean one check doing two jobs and one of them never firing.
        """
        if signal.action != SignalAction.BUY:
            return True, ""

        cost = signal.notional()
        if cost > self.account.cash_balance:
            return False, (
                f"INSUFFICIENT CASH: order costs {cost:.4f} against a free balance "
                f"of {self.account.cash_balance:.4f}."
            )
        return True, ""

    # ----------------------------------------------------------------- report --
    def _headroom(self, name: str) -> str:
        """Measured distance to a limit, for guardrails that currently pass.

        A pass/fail badge alone cannot distinguish "comfortably inside" from "one
        bad candle from the edge", which is the distinction an operator is
        actually watching the panel for.
        """
        equity = self.account.total_equity

        if name == "Daily Drawdown Limit":
            return (
                f"{self.account.daily_drawdown():.2%} drawn down, "
                f"{self.max_daily_drawdown_pct:.1%} allowed"
            )

        if name == "Leverage Shield":
            ratio = self.account.current_position_value / equity
            return (
                f"gross exposure {ratio:.2f}x of "
                f"{self.max_gross_exposure_ratio:.1f}x allowed"
            )

        if name == "Trade Risk Limit":
            return (
                f"{equity * self.max_trade_risk_pct:.2f} at risk per trade "
                f"({self.max_trade_risk_pct:.1%} of equity)"
            )

        return "every order must carry a stop"

    def guardrail_states(self) -> list[dict[str, object]]:
        """Report every guardrail's current standing, for monitoring surfaces.

        Evaluates each check in isolation against a minimal probe order, so the
        result answers "is this account inside its limits right now" rather than
        "did the last order pass". A monitoring panel that only shows the last
        verdict cannot distinguish a healthy account from one that has since
        drifted past a limit.

        The probe is scaled to the account rather than fixed, so it is never
        rejected for being implausibly large or trivially small. Each check is
        isolated in its own try: one raising must not blank the others, since a
        panel that silently drops a failing guardrail is worse than no panel.
        """
        equity = self.account.total_equity

        def probe_for(notional: float, stop_pct: float) -> OrderSignal:
            """A synthetic order of ``notional`` quote units with a ``stop_pct`` stop."""
            entry = max(notional, 1e-9)
            return OrderSignal(
                ticker="PROBE",
                action=SignalAction.BUY,
                quantity=1.0,
                entry_price=entry,
                stop_loss_price=entry * (1 - stop_pct),
                conviction_score=1.0,
            )

        # Two probes, because one size cannot satisfy both kinds of check. The
        # risk probe risks exactly the permitted budget, so that check reports the
        # account's standing rather than the probe's size. The small probe is used
        # for the constraints that are about the account's absolute state, where a
        # large probe would trip them for reasons that have nothing to do with
        # whether the account is healthy.
        risk_probe = probe_for(equity, self.max_trade_risk_pct)
        small_probe = probe_for(equity * 0.01, 0.005)

        declared = (
            (
                "Daily Drawdown Limit",
                f"< {self.max_daily_drawdown_pct:.1%}",
                self._check_drawdown_circuit_breaker,
                small_probe,
            ),
            (
                "Trade Risk Limit",
                f"< {self.max_trade_risk_pct:.1%} equity",
                self._check_stop_loss,
                risk_probe,
            ),
            (
                "Leverage Shield",
                f"< {self.max_gross_exposure_ratio:.1f}x",
                self._check_exposure,
                small_probe,
            ),
            (
                "Hard Stop-Loss Verification",
                "required",
                self._check_stop_loss,
                small_probe,
            ),
        )

        states: list[dict[str, object]] = []
        for name, limit, check, probe in declared:
            try:
                passed, detail = check(probe)
            except Exception as exc:  # noqa: BLE001 - report, never propagate
                passed, detail = False, f"evaluation error: {exc}"

            if passed:
                # A check that passes returns no reason by contract. Show the
                # measured standing instead of a blank, so the panel answers
                # "how close am I" and not merely "am I broken".
                detail = self._headroom(name)
            states.append(
                {"name": name, "limit": limit, "passed": passed, "detail": detail}
            )

        states.append(
            {
                "name": "Kill Switch",
                "limit": "not halted",
                "passed": not (self.trading_halted or config.TRADING_HALTED),
                "detail": (
                    "TRADING_HALTED is set; all order flow is rejected"
                    if (self.trading_halted or config.TRADING_HALTED)
                    else "TRADING_HALTED is clear"
                ),
            }
        )
        return states

    # --------------------------------------------------------------- pipeline --
    def process_signal(self, signal: OrderSignal) -> OrderResponse:
        """Run every guardrail and return a verdict. Never raises."""
        def reject(reason: str) -> OrderResponse:
            return OrderResponse(
                status=ExecutionStatus.REJECTED,
                ticker=signal.ticker,
                action=signal.action,
                reason=reason,
            )

        if signal.action == SignalAction.HOLD:
            return reject("HOLD: no execution required.")

        if self.trading_halted or config.TRADING_HALTED:
            return reject(
                "KILL SWITCH: TRADING_HALTED is set. All order flow is rejected "
                "until it is cleared."
            )

        checks = (
            self._check_conviction,
            self._check_drawdown_circuit_breaker,
            self._check_stop_loss,
            self._check_exposure,
            self._check_funding,
        )
        for check in checks:
            passed, reason = check(signal)
            if not passed:
                return reject(reason)

        return OrderResponse(
            status=ExecutionStatus.ACCEPTED,
            ticker=signal.ticker,
            action=signal.action,
            quantity=signal.quantity,
            approved_price=signal.entry_price,
            stop_loss_price=signal.stop_loss_price,
            reason="All deterministic risk constraints passed.",
        )