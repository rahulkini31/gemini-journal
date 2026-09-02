"""Build and price candidate option structures.

Deterministic. Runs BEFORE any model does, so the shortlist a model sees
contains only structures that are real, priced, and tradeable. A model cannot
express an untradeable idea because untradeable ideas are not in its input.
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime

from .book import CONTRACT_MULTIPLIER, Leg
from .config import Settings
from .context import exchange_date
from .market import (
    Contract,
    cross_leg_quote_skew_seconds,
    quotes_are_synchronous,
    tradeable,
)
from .pricing import TerminalScenario, implied_forward, spread_terminal_scenario


@dataclass(frozen=True)
class Structure:
    """A defined-risk multi-leg options structure, priced from real quotes."""

    name: str
    underlying: str
    expiry: date
    qty: int
    long: Contract
    short: Contract
    net_debit: float          # per spread, per share. POSITIVE = we pay a debit,
                              # NEGATIVE = we receive a credit. This mirrors
                              # Alpaca's mleg limit_price convention exactly, so
                              # the sign never has to be flipped at the boundary.
    max_loss: float           # dollars, total for qty
    max_gain: float           # dollars, total for qty
    spot: float = 0.0         # underlying at construction, for terminal scenario
    realised_vol: float = 0.0 # vol assumption for the physical-measure proxy
    forward: float = 0.0      # parity-implied forward for this expiry
    rationale: str = ""
    spot_source: str = "unspecified"
    realised_vol_source: str = "unspecified"
    forward_source: str = "unspecified"
    valuation_date: date | None = None
    scenario_calibrated: bool = False
    scenario_policy_aware: bool = False

    @property
    def is_credit(self) -> bool:
        return self.net_debit < 0

    @property
    def direction(self) -> str:
        """Economic spot direction used for diversification buckets."""
        if self.name.startswith("bull_"):
            return "bullish"
        if self.name.startswith("bear_"):
            return "bearish"
        net_delta = self.long.delta - self.short.delta
        if net_delta > 0:
            return "bullish"
        if net_delta < 0:
            return "bearish"
        return "neutral"

    @property
    def anchor_symbol(self) -> str:
        """The leg selected from the delta grid (long for debit, short for credit)."""
        return self.short.symbol if self.is_credit else self.long.symbol

    @property
    def dte(self) -> int:
        return (self.expiry - (self.valuation_date or exchange_date())).days

    @property
    def missing_model_inputs(self) -> tuple[str, ...]:
        """Inputs without which a physical-distribution EV is undefined.

        A strike is not an observed spot, one option leg's IV is not realised
        volatility, and spot is not a parity-implied forward.  Treating any of
        those substitutions as data made a missing-input failure look like a
        confident model output, so availability is explicit and fail-closed.
        """
        values = {
            "spot": self.spot,
            "realised_vol": self.realised_vol,
            "forward": self.forward,
        }
        return tuple(name for name, value in values.items() if value <= 0)

    @property
    def model_inputs_valid(self) -> bool:
        return not self.missing_model_inputs

    def model_input_provenance(self) -> dict:
        """Machine-readable source and availability for every EV input."""
        values_and_sources = {
            "spot": (self.spot, self.spot_source),
            "realised_vol": (self.realised_vol, self.realised_vol_source),
            "forward": (self.forward, self.forward_source),
        }
        inputs = {}
        for name, (value, source) in values_and_sources.items():
            available = value > 0
            inputs[name] = {
                "value": value if available else None,
                "source": source if available else "missing",
                "available": available,
            }
        return {
            "valid": self.model_inputs_valid,
            "measure": "physical_proxy_lognormal",
            "horizon": {
                "type": "expiry",
                "date": self.expiry.isoformat(),
                "dte": self.dte,
            },
            "calibrated": self.scenario_calibrated,
            "policy_aware": self.scenario_policy_aware,
            "missing_inputs": list(self.missing_model_inputs),
            "inputs": inputs,
        }

    def terminal_expiry_scenario(
        self, net_debit: float | None = None
    ) -> TerminalScenario:
        """Return the explicitly scoped terminal payoff scenario."""
        if not self.model_inputs_valid:
            # ``spread_terminal_scenario`` intentionally supports a general
            # spot-as-forward fallback.  Shortlist EV does not: all three
            # inputs have distinct provenance and must be observed explicitly.
            return TerminalScenario(
                ev_at_market=0.0,
                ev_under_rv=0.0,
                variance_premium=0.0,
                market_mid_value=0.0,
                spread_value_at_rv=0.0,
                probability_of_profit=0.0,
                breakeven=0.0,
                valid=False,
                calibrated=self.scenario_calibrated,
                policy_aware=self.scenario_policy_aware,
            )
        result = spread_terminal_scenario(
            kind=self.long.kind,
            long_strike=self.long.strike,
            short_strike=self.short.strike,
            net_debit=self.net_debit if net_debit is None else net_debit,
            spot=self.spot,
            dte=self.dte,
            market_mid_value=self.long.mid - self.short.mid,
            realised_vol=self.realised_vol,
            forward=self.forward,
            multiplier=CONTRACT_MULTIPLIER * self.qty,
        )
        return replace(
            result,
            calibrated=self.scenario_calibrated,
            policy_aware=self.scenario_policy_aware,
        )

    def ev(self, net_debit: float | None = None) -> TerminalScenario:
        """Compatibility alias for :meth:`terminal_expiry_scenario`."""
        return self.terminal_expiry_scenario(net_debit=net_debit)

    @property
    def width(self) -> float:
        return abs(self.long.strike - self.short.strike)

    @property
    def legs(self) -> list[Leg]:
        return [
            Leg.from_contract(self.long, self.qty),
            Leg.from_contract(self.short, -self.qty),
        ]

    @property
    def key(self) -> str:
        return f"{self.name}:{self.long.symbol}/{self.short.symbol}"

    def at_qty(self, qty: int) -> "Structure":
        scale = qty / self.qty if self.qty else 0
        return replace(
            self, qty=qty, max_loss=self.max_loss * scale, max_gain=self.max_gain * scale
        )

    def limit_price(self, *, aggression: float = 0.5) -> float:
        """Limit price in Alpaca's mleg convention.

        Per the Trading API reference and the alpaca-py source:
          "A positive value indicates a debit, representing a cost or payment to
           be made. A negative value signifies a credit, reflecting an amount to
           be received."

        There is NO server-side sign validation - a wrong sign is accepted
        silently and fills against you. A credit spread submitted at +0.01 reads
        as "I will pay up to a 1c debit", which a structure worth 2.16 of credit
        satisfies immediately, handing the credit away.

        Both directions cross the spread, so ``aggression`` always moves the
        price AGAINST us: 0.0 = optimistic mid, 1.0 = the natural price.
        """
        natural = self.long.ask - self.short.bid   # pay more / receive less
        mid = self.long.mid - self.short.mid
        price = mid + (natural - mid) * aggression
        # keep the sign; only clamp the magnitude away from zero
        if price < 0:
            return min(round(price, 2), -0.01)
        return max(round(price, 2), 0.01)

    def summary(self) -> dict:
        terminal = self.terminal_expiry_scenario()
        expected_pnl = terminal.expected_pnl_at_expiry
        profit_probability = terminal.profit_probability_at_expiry
        scenario = {
            "valid": terminal.valid,
            "measure": terminal.measure,
            "horizon": {
                "type": terminal.horizon,
                "date": self.expiry.isoformat(),
                "dte": self.dte,
            },
            "calibrated": terminal.calibrated,
            "policy_aware": terminal.policy_aware,
            "expected_pnl": (
                round(expected_pnl, 2) if expected_pnl is not None else None
            ),
            "profit_probability": (
                round(profit_probability, 4)
                if profit_probability is not None else None
            ),
            "breakeven": (
                round(terminal.breakeven_at_expiry, 4)
                if terminal.breakeven_at_expiry is not None else None
            ),
            "variance_premium_vs_mid": (
                round(terminal.variance_premium, 2) if terminal.valid else None
            ),
            "model_inputs": self.model_input_provenance()["inputs"],
        }
        return {
            "key": self.key,
            "name": self.name,
            "underlying": self.underlying,
            "expiry": self.expiry.isoformat(),
            "dte": self.dte,
            "qty": self.qty,
            "long": self.long.symbol,
            "short": self.short.symbol,
            "width": self.width,
            "net_debit": round(self.net_debit, 2),
            "structure_type": "credit" if self.is_credit else "debit",
            "limit_price": self.limit_price(),
            "max_loss": round(self.max_loss, 2),
            "max_gain": round(self.max_gain, 2),
            "reward_risk": round(self.max_gain / self.max_loss, 2) if self.max_loss else 0,
            "expiration_breakeven": round(expiration_breakeven(self), 4),
            # Compatibility keys.  They remain nullable so missing model data
            # cannot masquerade as a measured zero.  New consumers should use
            # the explicitly-scoped ``terminal_expiry_scenario`` object.
            "expected_value_under_realised_vol": scenario["expected_pnl"],
            "ev_per_dollar_risked": (
                round(expected_pnl / self.max_loss, 4)
                if expected_pnl is not None and self.max_loss else None
            ),
            "quality_score": round(quality_score(self), 4),
            "execution_cost": round(submitted_execution_cost(self), 2),
            "natural_execution_cost": round(execution_cost(self), 2),
            "payoff_shape": payoff_shape_metrics(self),
            "quote_provenance": {
                "long": contract_quote_summary(self.long),
                "short": contract_quote_summary(self.short),
                "cross_leg_skew_seconds": cross_leg_quote_skew_seconds(
                    (self.long, self.short)
                ),
                "submitted_limit_price": self.limit_price(),
                "limit_aggression_from_mid_to_natural": 0.5,
            },
            "variance_premium": scenario["variance_premium_vs_mid"],
            "probability_of_profit": scenario["profit_probability"],
            "execution_drag": (
                round(execution_drag(self), 2) if terminal.valid else None
            ),
            "long_delta": round(self.long.delta, 4),
            "short_delta": round(self.short.delta, 4),
            "long_iv": round(self.long.iv, 4),
            "short_iv": round(self.short.iv, 4),
            "ev_model": self.model_input_provenance(),
            "terminal_expiry_scenario": scenario,
            "rationale": self.rationale,
        }


def expiration_breakeven(structure: Structure) -> float:
    """Model-free expiry breakeven implied by strikes and entry cash flow."""
    if structure.name == "bull_call_spread":
        return structure.long.strike + structure.net_debit
    if structure.name == "bear_put_spread":
        return structure.long.strike - structure.net_debit
    if structure.name == "bear_call_spread":
        return structure.short.strike - structure.net_debit
    if structure.name == "bull_put_spread":
        return structure.short.strike + structure.net_debit
    raise ValueError(f"unsupported vertical structure: {structure.name}")


def contract_quote_summary(contract: Contract) -> dict:
    """Complete leg-level evidence consumed by the proposer and adversary."""
    return {
        "symbol": contract.symbol,
        "bid": contract.bid,
        "ask": contract.ask,
        "mid": round(contract.mid, 4),
        "spread_pct": round(contract.spread_pct, 6),
        "bid_size": contract.bid_size,
        "ask_size": contract.ask_size,
        "bid_exchange": contract.bid_exchange,
        "ask_exchange": contract.ask_exchange,
        "quote_conditions": list(contract.quote_conditions),
        "quote_timestamp": (
            contract.quote_timestamp.isoformat()
            if contract.quote_timestamp is not None else None
        ),
        "greeks": {
            "delta": contract.delta,
            "gamma": contract.gamma,
            "theta": contract.theta,
            "vega": contract.vega,
            "iv": contract.iv,
        },
    }


def forwards_by_series(contracts: list[Contract]) -> dict[tuple[str, date], float]:
    """Parity-implied forward per (underlying, expiry).

    Pricing options off raw spot with a guessed interest rate was measurably
    wrong: SPY calls and puts were misvalued by up to a dollar in OPPOSITE
    directions, which is the signature of a missing forward drift and which made
    edge-versus-mid come out spuriously POSITIVE. The forward is observable, so
    observe it rather than guessing its components.
    """
    calls: dict[tuple[str, date, float], float] = {}
    puts: dict[tuple[str, date, float], float] = {}
    for c in contracts:
        target = calls if c.kind == "C" else puts
        target[(c.underlying, c.expiry, c.strike)] = c.mid

    grouped: dict[tuple[str, date], list[tuple[float, float, float]]] = {}
    for (underlying, expiry, strike), call_mid in calls.items():
        put_mid = puts.get((underlying, expiry, strike))
        if put_mid is None:
            continue
        grouped.setdefault((underlying, expiry), []).append(
            (strike, call_mid, put_mid)
        )

    out: dict[tuple[str, date], float] = {}
    for series, pairs in grouped.items():
        fwd = implied_forward(pairs)
        if fwd:
            out[series] = fwd
    return out


def _contract_key(contract: Contract) -> tuple:
    """Total ordering for contracts, independent of vendor/page ordering."""
    return (
        contract.underlying,
        contract.expiry.isoformat(),
        contract.kind,
        contract.strike,
        contract.symbol,
    )


def select_delta_anchors(
    contracts: Sequence[Contract],
    target_deltas: Sequence[float],
    *,
    tolerance: float | None = None,
) -> list[Contract]:
    """Select at most one unique contract for each absolute-delta target.

    Targets are normalised and visited from high to low delta.  For each target
    the nearest *unused* contract wins, with a total contract ordering breaking
    ties.  The result is therefore reproducible even if an API page arrives in
    a different order, and nearby grid points cannot silently reuse one anchor.

    ``tolerance=None`` preserves the historical nearest-contract behaviour.
    A numeric tolerance fails closed when the chain has no sufficiently close
    contract instead of accepting a deep-ITM/OTM anchor.
    """
    if tolerance is not None and tolerance < 0:
        raise ValueError("anchor delta tolerance must be non-negative")

    targets = sorted({abs(float(target)) for target in target_deltas}, reverse=True)
    if any(target > 1 for target in targets):
        raise ValueError("anchor delta targets must be between 0 and 1")

    available = sorted(contracts, key=_contract_key)
    selected: list[Contract] = []
    used: set[str] = set()
    for target in targets:
        eligible = [contract for contract in available if contract.symbol not in used]
        if not eligible:
            break
        nearest = min(
            eligible,
            key=lambda contract: (
                abs(abs(contract.delta) - target),
                _contract_key(contract),
            ),
        )
        distance = abs(abs(nearest.delta) - target)
        if tolerance is not None and distance > tolerance:
            continue
        selected.append(nearest)
        used.add(nearest.symbol)
    return selected


def _series_spot(
    underlying: str,
    *,
    spot: float | None,
    spots: Mapping[str, float] | None,
) -> float:
    value = (spots or {}).get(underlying, spot or 0.0)
    return float(value) if value and value > 0 else 0.0


def _rank_key(structure: Structure) -> tuple:
    """Execution-first total ordering used by every deterministic selector."""
    return (
        -quality_score(structure),
        submitted_execution_cost(structure),
        structure.max_loss,
        -structure.max_gain,
        structure.underlying,
        structure.expiry.isoformat(),
        structure.name,
        structure.anchor_symbol,
        structure.long.strike,
        structure.short.strike,
        structure.key,
    )


def _truncate(
    candidates: list[Structure], max_candidates: int | None
) -> list[Structure]:
    if max_candidates is None:
        return candidates
    if max_candidates < 0:
        raise ValueError("max_candidates must be non-negative or None")
    return candidates[:max_candidates]


def build_vertical_debit_spreads(
    contracts: list[Contract],
    settings: Settings,
    *,
    kind: str,
    target_delta: float = 0.45,
    target_deltas: Sequence[float] | None = None,
    anchor_delta_tolerance: float | None = None,
    max_candidates: int | None = 12,
    spot: float | None = None,
    spots: Mapping[str, float] | None = None,
    max_loss_cap: float | None = None,
    realised_vols: dict[str, float] | None = None,
    as_of: datetime | None = None,
) -> list[Structure]:
    """Construct defined-risk vertical DEBIT spreads.

    Debit rather than credit, deliberately. The sign convention for MLEG
    ``limit_price`` on net-credit structures is not yet verified against the
    live API (knowledge-base/10-verification-log.md); a debit spread's limit is
    unambiguous - it is the most we will pay. Max loss is the debit, known
    exactly at entry. The short American-option leg still has early-assignment
    risk, which lifecycle and event gates must manage separately.

    ``kind`` "C" builds bull call spreads, "P" builds bear put spreads.
    """
    budget = settings.risk
    forwards = forwards_by_series(contracts)
    usable = [
        c for c in contracts
        if c.kind == kind and tradeable(c, settings, as_of=as_of)
    ]
    if len(usable) < 2:
        return []

    # Group by (underlying, expiry). Grouping by expiry alone silently pairs
    # legs from DIFFERENT underlyings - e.g. long IWM 295 against short SPY 773 -
    # which is not a vertical, cannot be filled as one, and prices as nonsense.
    by_series: dict[tuple[str, date], list[Contract]] = {}
    for contract in usable:
        by_series.setdefault((contract.underlying, contract.expiry), []).append(contract)

    anchor_targets = target_deltas if target_deltas is not None else (target_delta,)
    candidates_by_key: dict[str, Structure] = {}
    for (_underlying, expiry), group in by_series.items():
        group.sort(key=lambda c: c.strike)
        anchors = select_delta_anchors(
            group, anchor_targets, tolerance=anchor_delta_tolerance
        )
        for anchor in anchors:
            observed_spot = _series_spot(_underlying, spot=spot, spots=spots)
            realised_vol = float((realised_vols or {}).get(_underlying, 0.0) or 0.0)
            forward = float(forwards.get((_underlying, expiry), 0.0) or 0.0)
            for short in group:
                if not _is_valid_pair(anchor, short, kind):
                    continue
                if as_of is not None and not quotes_are_synchronous(
                    (anchor, short), settings
                ):
                    continue
                # The short leg must carry real premium. Selling a 0.02-delta
                # wing finances nothing and consumes delta like a naked option.
                if not (
                    budget.min_short_delta
                    <= abs(short.delta)
                    <= budget.max_short_delta
                ):
                    continue
                reference = observed_spot or anchor.strike
                if (
                    abs(anchor.strike - short.strike)
                    > reference * budget.max_width_pct_of_spot
                ):
                    continue
                structure = _price_debit_spread(
                    anchor,
                    short,
                    kind,
                    expiry,
                    spot=observed_spot,
                    realised_vol=realised_vol,
                    forward=forward,
                    spot_source="underlying_spot",
                    realised_vol_source="historical_realised_vol",
                    forward_source="put_call_parity",
                    valuation_date=exchange_date(as_of),
                )
                if structure is not None:
                    candidates_by_key[structure.key] = structure

    candidates = list(candidates_by_key.values())

    candidates = [
        candidate for candidate in candidates
        if not payoff_shape_violations(candidate, settings)
    ]

    # Enforce the per-trade loss cap HERE, not downstream. Ranking cannot be
    # relied on to preserve compliant candidates: sorting by EV (absolute or
    # risk-adjusted) favours wide, high-notional spreads, which crowded every
    # affordable candidate off a truncated shortlist. The generator must not
    # emit what the risk engine will certainly refuse.
    if max_loss_cap is not None:
        candidates = [c for c in candidates if c.max_loss <= max_loss_cap]

    candidates.sort(key=_rank_key)
    return _truncate(candidates, max_candidates)


def terminal_expected_pnl(structure: Structure) -> float | None:
    """Modelled P&L at expiry under the declared physical-measure proxy.

    ``None`` means the scenario cannot be evaluated.  Even when available this
    is uncalibrated and ignores the actual exit policy unless the corresponding
    structure flags explicitly say otherwise.
    """
    result = structure.terminal_expiry_scenario()
    return result.expected_pnl_at_expiry


def expected_value(structure: Structure) -> float:
    """Compatibility alias; unavailable terminal scenarios map to zero.

    New code must use :func:`terminal_expected_pnl` so missing data stays
    distinguishable from an actual zero-P&L scenario.
    """
    value = terminal_expected_pnl(structure)
    return value if value is not None else 0.0


def execution_cost(structure: Structure) -> float:
    """What crossing the spread costs us, in dollars. OBSERVED, not modelled.

    This used to be derived from Black-Scholes at the vendor's per-leg implied
    vols, which was both unnecessary and wrong: the vendor's IVs violate
    put-call parity by a uniform 1.6 vol points, so the "model" invented edge.

    Our execution price and the quoted mid are both directly observable, so
    subtract them. No model can improve on that, and none can corrupt it.
    """
    mid_debit = structure.long.mid - structure.short.mid
    return abs(structure.net_debit - mid_debit) * CONTRACT_MULTIPLIER * structure.qty


def submitted_execution_cost(structure: Structure) -> float:
    """Observed midpoint concession at the limit actually sent to Alpaca.

    ``execution_cost`` retains the natural/full-cross diagnostic used by the
    terminal-scenario compatibility code.  Entry orders are submitted halfway
    from midpoint to natural, so gates and ranking must use this executable
    decision price instead of pretending the full cross was paid.
    """
    mid_debit = structure.long.mid - structure.short.mid
    return round(
        abs(structure.limit_price() - mid_debit)
        * CONTRACT_MULTIPLIER
        * structure.qty,
        2,
    )


def payoff_shape_metrics(structure: Structure) -> dict[str, float]:
    """Forecast-free payoff geometry, normalized per one spread."""
    units = CONTRACT_MULTIPLIER * structure.qty
    credit = structure.max_gain / units if structure.is_credit and units else 0.0
    debit = structure.max_loss / units if not structure.is_credit and units else 0.0
    return {
        "credit_width_ratio": credit / structure.width if structure.width else 0.0,
        "debit_width_ratio": debit / structure.width if structure.width else 0.0,
        "reward_risk_ratio": (
            structure.max_gain / structure.max_loss if structure.max_loss else 0.0
        ),
    }


def payoff_shape_violations(
    structure: Structure, settings: Settings,
) -> tuple[str, ...]:
    """Return every deterministic payoff floor breached by a candidate."""
    budget = settings.risk
    metrics = payoff_shape_metrics(structure)
    reasons: list[str] = []
    if structure.is_credit and metrics["credit_width_ratio"] < float(
        getattr(budget, "min_credit_width_ratio", 0.0)
    ):
        reasons.append(
            f"credit/width {metrics['credit_width_ratio']:.1%} below "
            f"{budget.min_credit_width_ratio:.1%}"
        )
    if not structure.is_credit and metrics["debit_width_ratio"] > float(
        getattr(budget, "max_debit_width_ratio", 1.0)
    ):
        reasons.append(
            f"debit/width {metrics['debit_width_ratio']:.1%} above "
            f"{budget.max_debit_width_ratio:.1%}"
        )
    if metrics["reward_risk_ratio"] < float(
        getattr(budget, "min_reward_risk_ratio", 0.0)
    ):
        reasons.append(
            f"reward/risk {metrics['reward_risk_ratio']:.3f} below "
            f"{budget.min_reward_risk_ratio:.3f}"
        )
    return tuple(reasons)


def variance_premium(structure: Structure) -> float:
    """Modelled value under realised vol MINUS the observed market mid.

    ⚠️ THIS IS NOT AN EDGE MEASURE AND MUST NOT BE RANKED ON.

    It is contaminated by skew, and the contamination has a fixed sign. A
    single-vol lognormal cannot reproduce a skewed market: for a put DEBIT
    spread the market prices the lower-strike short leg relatively richer than
    any flat-vol model will, so the model always values the structure ABOVE the
    market. Measured live, every SPY put debit spread showed a large positive
    "premium" while SPY put IV (14.5%) was in fact ABOVE realised vol (13.2%) -
    a genuine vol premium would have had the opposite sign.

    Separating skew from a true vol premium needs a smile-aware model compared
    against a real-world distribution carrying the same skew. That is out of
    scope here, so this number is reported for transparency and used for
    nothing.
    """
    result = structure.ev()
    return result.variance_premium if result.valid else 0.0


def build_vertical_credit_spreads(
    contracts: list[Contract],
    settings: Settings,
    *,
    kind: str,
    target_deltas: Sequence[float] | None = None,
    anchor_delta_tolerance: float | None = None,
    max_candidates: int | None = 12,
    spot: float | None = None,
    spots: Mapping[str, float] | None = None,
    max_loss_cap: float | None = None,
    realised_vols: dict[str, float] | None = None,
    as_of: datetime | None = None,
) -> list[Structure]:
    """Construct defined-risk vertical CREDIT spreads.

    ``kind`` "C" builds bear call spreads (sell the nearer call, buy the further
    one); "P" builds bull put spreads. Max loss is width minus credit, known
    exactly at entry.
    """
    budget = settings.risk
    forwards = forwards_by_series(contracts)
    usable = [
        c for c in contracts
        if c.kind == kind and tradeable(c, settings, as_of=as_of)
    ]
    if len(usable) < 2:
        return []

    by_series: dict[tuple[str, date], list[Contract]] = {}
    for contract in usable:
        by_series.setdefault((contract.underlying, contract.expiry), []).append(contract)

    anchor_targets = (
        target_deltas
        if target_deltas is not None
        else (budget.credit_short_target_delta,)
    )
    candidates_by_key: dict[str, Structure] = {}
    for (_underlying, expiry), group in by_series.items():
        group.sort(key=lambda c: c.strike)
        anchors = select_delta_anchors(
            group, anchor_targets, tolerance=anchor_delta_tolerance
        )
        for anchor in anchors:
            observed_spot = _series_spot(_underlying, spot=spot, spots=spots)
            realised_vol = float((realised_vols or {}).get(_underlying, 0.0) or 0.0)
            forward = float(forwards.get((_underlying, expiry), 0.0) or 0.0)
            for long in group:
                if not _is_valid_credit_pair(long, anchor, kind):
                    continue
                if not (
                    budget.min_credit_long_delta
                    <= abs(long.delta)
                    <= budget.max_credit_long_delta
                ):
                    continue
                if as_of is not None and not quotes_are_synchronous(
                    (long, anchor), settings
                ):
                    continue
                reference = observed_spot or anchor.strike
                if (
                    abs(anchor.strike - long.strike)
                    > reference * budget.max_width_pct_of_spot
                ):
                    continue
                structure = _price_credit_spread(
                    long,
                    anchor,
                    kind,
                    expiry,
                    spot=observed_spot,
                    realised_vol=realised_vol,
                    forward=forward,
                    spot_source="underlying_spot",
                    realised_vol_source="historical_realised_vol",
                    forward_source="put_call_parity",
                    valuation_date=exchange_date(as_of),
                )
                if structure is not None:
                    candidates_by_key[structure.key] = structure

    candidates = list(candidates_by_key.values())

    candidates = [
        candidate for candidate in candidates
        if not payoff_shape_violations(candidate, settings)
    ]

    # Enforce the per-trade loss cap HERE, not downstream. Ranking cannot be
    # relied on to preserve compliant candidates: sorting by EV (absolute or
    # risk-adjusted) favours wide, high-notional spreads, which crowded every
    # affordable candidate off a truncated shortlist. The generator must not
    # emit what the risk engine will certainly refuse.
    if max_loss_cap is not None:
        candidates = [c for c in candidates if c.max_loss <= max_loss_cap]

    candidates.sort(key=_rank_key)
    return _truncate(candidates, max_candidates)


def _model_input(value: float | None, source: str | None) -> tuple[float, str]:
    """Normalise an input without inventing a replacement value or source."""
    numeric = float(value) if value is not None else 0.0
    if numeric <= 0:
        return 0.0, "missing"
    return numeric, source or "caller"


def reprice_structure(
    structure: Structure,
    quotes: Mapping[str, Contract],
    *,
    spot: float | None = None,
    valuation_date: date | None = None,
) -> Structure | None:
    """Rebuild a selected structure from a newly fetched two-leg snapshot.

    Contract identity and shape are immutable, while quotes and Greeks are not.
    Re-running the original debit/credit constructor refreshes entry economics,
    max loss, and every post-trade Greek without silently changing the strategy.
    """
    long = quotes.get(structure.long.symbol)
    short = quotes.get(structure.short.symbol)
    if long is None or short is None:
        return None

    observed_spot = spot if spot is not None else structure.spot
    spot_source = (
        "underlying_spot_refresh" if spot is not None else structure.spot_source
    )
    pricer = _price_credit_spread if structure.is_credit else _price_debit_spread
    refreshed = pricer(
        long,
        short,
        structure.long.kind,
        structure.expiry,
        spot=observed_spot,
        realised_vol=structure.realised_vol,
        forward=structure.forward,
        spot_source=spot_source,
        realised_vol_source=structure.realised_vol_source,
        forward_source=structure.forward_source,
        valuation_date=valuation_date or structure.valuation_date,
    )
    if refreshed is None:
        return None
    return replace(
        refreshed.at_qty(structure.qty),
        scenario_calibrated=structure.scenario_calibrated,
        scenario_policy_aware=structure.scenario_policy_aware,
    )


def _price_credit_spread(
    long: Contract, short: Contract, kind: str, expiry: date,
    *,
    spot: float | None = 0.0,
    realised_vol: float | None = 0.0,
    forward: float | None = 0.0,
    spot_source: str | None = None,
    realised_vol_source: str | None = None,
    forward_source: str | None = None,
    valuation_date: date | None = None,
) -> Structure | None:
    """Price a credit vertical from real quotes. Returns None if not viable."""
    if long.underlying != short.underlying or long.expiry != short.expiry \
            or long.kind != short.kind:
        return None
    width = abs(long.strike - short.strike)
    if width <= 0:
        return None

    # we sell the short at the bid and buy the long at the ask: the honest credit
    credit = short.bid - long.ask
    if credit <= 0:
        return None                       # not a credit spread
    if credit >= width:
        return None                       # implausible: free money

    name = "bear_call_spread" if kind == "C" else "bull_put_spread"
    direction = "bearish" if kind == "C" else "bullish"
    observed_spot, observed_spot_source = _model_input(spot, spot_source)
    observed_rv, observed_rv_source = _model_input(
        realised_vol, realised_vol_source
    )
    observed_forward, observed_forward_source = _model_input(
        forward, forward_source
    )
    return Structure(
        name=name,
        underlying=long.underlying,
        expiry=expiry,
        qty=1,
        long=long,
        short=short,
        net_debit=-credit,                # NEGATIVE: Alpaca's credit convention
        max_loss=(width - credit) * CONTRACT_MULTIPLIER,
        max_gain=credit * CONTRACT_MULTIPLIER,
        spot=observed_spot,
        realised_vol=observed_rv,
        forward=observed_forward,
        rationale=(
            f"{direction} {width:.0f}-wide credit vertical, "
            f"collect {credit:.2f}, risk {width - credit:.2f}"
        ),
        spot_source=observed_spot_source,
        realised_vol_source=observed_rv_source,
        forward_source=observed_forward_source,
        valuation_date=valuation_date,
    )


def terminal_profit_probability(structure: Structure) -> float | None:
    """Probability of terminal profit under the declared scenario proxy."""
    result = structure.terminal_expiry_scenario()
    return result.profit_probability_at_expiry


def probability_of_profit(structure: Structure) -> float:
    """Compatibility alias; use :func:`terminal_profit_probability` instead."""
    value = terminal_profit_probability(structure)
    return value if value is not None else 0.0


def quality_score(structure: Structure) -> float:
    """Execution-only ranking score; higher means less observable friction.

    This function deliberately makes no alpha or win-rate claim.  Probability
    of profit and terminal EV are model outputs, not execution quality, and the
    old PoP-heavy score systematically promoted small-credit / large-loss
    structures.  Dividing friction by max loss also rewarded a farther wing for
    adding tail risk to the denominator.  The worst displayed leg spread is an
    execution-quality measure that is independent of the payoff tail.
    """
    if structure.max_loss <= 0:
        return 0.0
    return 1.0 - max(structure.long.spread_pct, structure.short.spread_pct)


def exposure_cluster_key(structure: Structure) -> tuple[str, date, str, str]:
    """Bucket structures that differ only in the wing around one anchor."""
    return (
        structure.underlying,
        structure.expiry,
        structure.direction,
        structure.anchor_symbol,
    )


def _dominates(left: Structure, right: Structure) -> bool:
    """Whether ``left`` is no worse on every robust, observable dimension."""
    comparisons = (
        left.max_loss <= right.max_loss,
        left.max_gain >= right.max_gain,
        submitted_execution_cost(left) <= submitted_execution_cost(right),
    )
    strict = (
        left.max_loss < right.max_loss
        or left.max_gain > right.max_gain
        or submitted_execution_cost(left) < submitted_execution_cost(right)
    )
    return all(comparisons) and strict


def pareto_prune(
    candidates: Sequence[Structure],
    *,
    cluster_key: Callable[[Structure], object] = exposure_cluster_key,
) -> list[Structure]:
    """Remove strictly dominated choices inside comparable exposure clusters.

    No comparison crosses underlying, expiry, direction, or delta-grid anchor:
    a superficially better bearish SPY spread cannot erase a bullish QQQ idea.
    Duplicate keys are collapsed deterministically before dominance checks.
    """
    unique: dict[str, Structure] = {}
    for candidate in sorted(candidates, key=_rank_key):
        unique.setdefault(candidate.key, candidate)

    groups: dict[object, list[Structure]] = defaultdict(list)
    for candidate in unique.values():
        groups[cluster_key(candidate)].append(candidate)

    kept: list[Structure] = []
    for group_key in sorted(groups, key=repr):
        group = groups[group_key]
        for candidate in group:
            if not any(
                other.key != candidate.key and _dominates(other, candidate)
                for other in group
            ):
                kept.append(candidate)
    return sorted(kept, key=_rank_key)


def select_diversified_shortlist(
    candidates: Sequence[Structure],
    top_k: int | None = 10,
    *,
    max_per_underlying_expiry_direction: int | None = 2,
    max_per_anchor: int | None = 1,
    apply_pareto: bool = True,
    correlation_groups: Sequence[Sequence[str]] | None = None,
    max_per_factor_direction: int | None = None,
    max_per_factor_direction_dte: int | None = None,
    dte_bucket_days: int = 7,
) -> list[Structure]:
    """Select deterministic top-K while bounding correlated lookalikes.

    Candidates first undergo within-cluster Pareto pruning, then compete on the
    execution-only rank.  Caps are combined ``(underlying, expiry, direction)``
    and per delta-grid anchor.  Caps diversify the menu presented to the LLM;
    they never manufacture a candidate from a weak or missing family.
    """
    for label, value in (
        ("top_k", top_k),
        (
            "max_per_underlying_expiry_direction",
            max_per_underlying_expiry_direction,
        ),
        ("max_per_anchor", max_per_anchor),
        ("max_per_factor_direction", max_per_factor_direction),
        ("max_per_factor_direction_dte", max_per_factor_direction_dte),
    ):
        if value is not None and value < 0:
            raise ValueError(f"{label} must be non-negative or None")
    if dte_bucket_days <= 0:
        raise ValueError("dte_bucket_days must be positive")

    factor_by_underlying: dict[str, str] = {}
    for group in correlation_groups or ():
        members = tuple(sorted({str(member) for member in group if str(member)}))
        if not members:
            continue
        label = "factor:" + ",".join(members)
        for member in members:
            existing = factor_by_underlying.get(member)
            if existing is not None and existing != label:
                raise ValueError(f"{member} appears in multiple correlation groups")
            factor_by_underlying[member] = label

    pool = pareto_prune(candidates) if apply_pareto else sorted(candidates, key=_rank_key)
    cluster_counts: defaultdict[tuple, int] = defaultdict(int)
    anchor_counts: defaultdict[tuple, int] = defaultdict(int)
    factor_direction_counts: defaultdict[tuple, int] = defaultdict(int)
    factor_dte_counts: defaultdict[tuple, int] = defaultdict(int)
    selected: list[Structure] = []
    seen: set[str] = set()

    for candidate in pool:
        if candidate.key in seen:
            continue
        cluster = (
            candidate.underlying,
            candidate.expiry,
            candidate.direction,
        )
        anchor = (
            candidate.underlying,
            candidate.expiry,
            candidate.anchor_symbol,
        )
        factor = factor_by_underlying.get(
            candidate.underlying, f"single:{candidate.underlying}"
        )
        factor_direction = (factor, candidate.direction)
        dte_bucket = max(0, candidate.dte) // dte_bucket_days
        factor_dte = (factor, candidate.direction, dte_bucket)
        if (
            max_per_underlying_expiry_direction is not None
            and cluster_counts[cluster] >= max_per_underlying_expiry_direction
        ):
            continue
        if max_per_anchor is not None and anchor_counts[anchor] >= max_per_anchor:
            continue
        if (
            max_per_factor_direction is not None
            and factor_direction_counts[factor_direction]
            >= max_per_factor_direction
        ):
            continue
        if (
            max_per_factor_direction_dte is not None
            and factor_dte_counts[factor_dte]
            >= max_per_factor_direction_dte
        ):
            continue

        selected.append(candidate)
        seen.add(candidate.key)
        cluster_counts[cluster] += 1
        anchor_counts[anchor] += 1
        factor_direction_counts[factor_direction] += 1
        factor_dte_counts[factor_dte] += 1
        if top_k is not None and len(selected) >= top_k:
            break
    return selected


def risk_adjusted_ev(structure: Structure) -> float:
    """Retained for reporting. Ranking uses ``quality_score``."""
    if structure.max_loss <= 0:
        return 0.0
    return expected_value(structure) / structure.max_loss


def fair_value_ev(structure: Structure) -> float:
    """EV under realised vol if we could transact at the midpoint of both legs."""
    mid_debit = structure.long.mid - structure.short.mid
    result = structure.ev(net_debit=mid_debit)
    return result.ev_under_rv if result.valid else 0.0


def execution_drag(structure: Structure) -> float:
    """Expected value surrendered to the bid-ask spread. Same units as EV."""
    return fair_value_ev(structure) - expected_value(structure)


def _is_valid_credit_pair(long: Contract, short: Contract, kind: str) -> bool:
    """Strike ordering for a CREDIT vertical - the inverse of the debit case.

    Bear call spread: sell the LOWER call, buy the HIGHER one for protection.
    Bull put spread:  sell the HIGHER put, buy the LOWER one for protection.
    """
    if long.symbol == short.symbol:
        return False
    if long.underlying != short.underlying or long.expiry != short.expiry \
            or long.kind != short.kind:
        return False
    if kind == "C":
        return long.strike > short.strike
    return long.strike < short.strike


def _is_valid_pair(long: Contract, short: Contract, kind: str) -> bool:
    if long.symbol == short.symbol:
        return False
    # A vertical is same underlying, same expiry, same kind - different strike.
    if long.underlying != short.underlying:
        return False
    if long.expiry != short.expiry:
        return False
    if long.kind != short.kind:
        return False
    if kind == "C":
        return short.strike > long.strike     # bull call: sell the higher strike
    return short.strike < long.strike         # bear put: sell the lower strike


def _price_debit_spread(
    long: Contract, short: Contract, kind: str, expiry: date,
    *,
    spot: float | None = 0.0,
    realised_vol: float | None = 0.0,
    forward: float | None = 0.0,
    spot_source: str | None = None,
    realised_vol_source: str | None = None,
    forward_source: str | None = None,
    valuation_date: date | None = None,
) -> Structure | None:
    """Price a debit vertical from real quotes. Returns None if not viable."""
    if long.underlying != short.underlying or long.expiry != short.expiry \
            or long.kind != short.kind:
        return None
    width = abs(long.strike - short.strike)
    if width <= 0:
        return None

    # we buy the long at the ask and sell the short at the bid: the honest cost
    net_debit = long.ask - short.bid
    if net_debit <= 0:
        return None                       # not a debit spread; skip
    if net_debit >= width:
        return None                       # no upside: pay more than we can make

    max_loss = net_debit * CONTRACT_MULTIPLIER
    max_gain = (width - net_debit) * CONTRACT_MULTIPLIER
    name = "bull_call_spread" if kind == "C" else "bear_put_spread"
    direction = "bullish" if kind == "C" else "bearish"
    observed_spot, observed_spot_source = _model_input(spot, spot_source)
    observed_rv, observed_rv_source = _model_input(
        realised_vol, realised_vol_source
    )
    observed_forward, observed_forward_source = _model_input(
        forward, forward_source
    )

    return Structure(
        name=name,
        underlying=long.underlying,
        expiry=expiry,
        qty=1,
        long=long,
        short=short,
        net_debit=net_debit,
        max_loss=max_loss,
        max_gain=max_gain,
        spot=observed_spot,
        realised_vol=observed_rv,
        forward=observed_forward,
        rationale=(
            f"{direction} {width:.0f}-wide vertical, "
            f"pay {net_debit:.2f} to make at most {width - net_debit:.2f}"
        ),
        spot_source=observed_spot_source,
        realised_vol_source=observed_rv_source,
        forward_source=observed_forward_source,
        valuation_date=valuation_date,
    )
