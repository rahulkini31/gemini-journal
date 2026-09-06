"""Leg reconciliation: intended structure vs what the broker actually holds.

Alpaca's paper environment gives "partial fills for a random size 10% of the
time". On a two-leg spread a partial fill does not produce a smaller spread - it
can produce a NAKED long or short option. The defined-risk structure the risk
engine approved no longer exists.

Every cycle therefore re-derives what we hold and flags any position that is not
part of a balanced structure.
"""
from __future__ import annotations

from dataclasses import dataclass

from .exits import ExitPlan, PositionGroup, group_option_positions


@dataclass
class Breach:
    underlying: str
    expiry: str
    kind: str
    detail: str
    symbols: list[str]
    severity: str          # "naked_short" | "unbalanced"


@dataclass(frozen=True)
class RepairPlan:
    """Executable alternatives for one reconciliation breach.

    ``repair`` removes only the excess contracts and restores a 1:1 vertical.
    ``flatten`` closes every leg in the malformed series.  Both are concrete
    close-only plans; policy code can choose the conservative full flatten
    without having to reconstruct quantities itself.
    """

    breach: Breach
    repair: ExitPlan
    flatten: ExitPlan
    recommended_action: str


def find_breaches(positions: list[dict]) -> list[Breach]:
    """Detect option positions that are not part of a balanced vertical.

    A balanced vertical has one long and one short leg of the same kind and
    expiry, in equal quantity. Anything else is either a naked leg (urgent) or
    an unbalanced ratio (needs attention).
    """
    breaches: list[Breach] = []
    equity_underlyings = {
        str(position.get("symbol") or "")
        for position in positions
        if position.get("asset_class") == "us_equity"
        and abs(float(position.get("qty") or 0)) > 0
    }
    for group in group_option_positions(positions):
        longs = group.long_qty
        shorts = group.short_qty
        symbols = [leg.symbol for leg in group.legs]
        expiry = group.expiry.isoformat()

        if group.underlying in equity_underlyings:
            breaches.append(Breach(
                group.underlying, expiry, group.kind,
                "equity position and residual option share an underlying; "
                "possible early assignment requires operator/ledger remediation",
                [group.underlying, *symbols], "probable_assignment",
            ))
        elif group.is_balanced_vertical:
            continue
        elif shorts > longs:
            breaches.append(Breach(
                group.underlying, expiry, group.kind,
                f"{shorts} short vs {longs} long: {shorts - longs} naked short "
                f"contract(s) - undefined risk",
                symbols, "naked_short",
            ))
        elif len(group.legs) == 2 and longs != shorts and shorts > 0:
            breaches.append(Breach(
                group.underlying, expiry, group.kind,
                f"{longs} long vs {shorts} short: unbalanced vertical",
                symbols, "unbalanced",
            ))
        else:
            breaches.append(Breach(
                group.underlying, expiry, group.kind,
                f"{len(group.legs)} option leg(s) cannot be mapped to one "
                "unambiguous 1:1 vertical",
                symbols, "unmanaged",
            ))
    return breaches


def _repair_for_group(group: PositionGroup, breach: Breach) -> ExitPlan:
    if group.short_qty > group.long_qty:
        remaining = group.short_qty - group.long_qty
        candidates = [leg for leg in group.legs if leg.is_short]
        side_detail = "short"
    else:
        remaining = group.long_qty - group.short_qty
        candidates = [leg for leg in group.legs if not leg.is_short]
        side_detail = "long"

    close_legs = []
    for leg in candidates:
        if remaining <= 0:
            break
        close_qty = min(leg.qty, remaining)
        close_legs.append(leg.close_leg(qty=close_qty))
        remaining -= close_qty

    # ``breach`` and ``group`` are derived from the same normalized positions,
    # so this signals an internal invariant violation rather than a tradeable
    # condition.  Failing here is safer than producing an incomplete repair.
    if remaining != 0 or not close_legs:
        raise ValueError(
            f"could not allocate {side_detail} excess for "
            f"{group.underlying}/{group.expiry.isoformat()}/{group.kind}"
        )

    repaired_qty = sum(leg.qty for leg in close_legs)
    return ExitPlan(
        underlying=group.underlying,
        expiry=group.expiry,
        kind=group.kind,
        legs=tuple(close_legs),
        reason="reconciliation_repair",
        detail=(
            f"close {repaired_qty} excess {side_detail} contract(s) to restore "
            f"a balanced vertical; {breach.detail}"
        ),
    )


def build_repair_plans(positions: list[dict]) -> list[RepairPlan]:
    """Turn every detected breach into explicit repair and flatten plans.

    This is deliberately a pure planning function.  It performs no broker call
    and gives the caller no reason to improvise per-leg quantities or intents.
    """
    breaches = {
        (breach.underlying, breach.expiry, breach.kind): breach
        for breach in find_breaches(positions)
    }
    plans: list[RepairPlan] = []
    for group in group_option_positions(positions):
        key = (group.underlying, group.expiry.isoformat(), group.kind)
        breach = breaches.get(key)
        if breach is None:
            continue
        if breach.severity == "probable_assignment":
            # Options-only automation cannot safely decide how to pair shares
            # with residual contracts; retain the breach and halt upstream.
            continue
        flatten = group.close_plan(
            reason="reconciliation_flatten",
            detail=f"flatten malformed option series; {breach.detail}",
        )
        repair = (
            flatten if breach.severity == "unmanaged"
            else _repair_for_group(group, breach)
        )
        plans.append(RepairPlan(
            breach=breach,
            repair=repair,
            flatten=flatten,
            recommended_action=(
                "flatten" if breach.severity == "unmanaged"
                else "repair_balance"
            ),
        ))
    return plans


plan_reconciliation = build_repair_plans


def summarise(positions: list[dict]) -> dict:
    breaches = find_breaches(positions)
    return {
        "option_positions": sum(
            1 for p in positions if p.get("asset_class") == "us_option"
        ),
        "breaches": [b.__dict__ for b in breaches],
        "naked_shorts": sum(1 for b in breaches if b.severity == "naked_short"),
        "healthy": not breaches,
    }
