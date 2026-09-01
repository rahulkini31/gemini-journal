"""Leg reconciliation: intended structure vs what the broker actually holds.

Alpaca's paper environment gives "partial fills for a random size 10% of the
time". On a two-leg spread a partial fill does not produce a smaller spread - it
can produce a NAKED long or short option. The defined-risk structure the risk
engine approved no longer exists.

Every cycle therefore re-derives what we hold and flags any position that is not
part of a balanced structure.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from .market import parse_occ


@dataclass
class Breach:
    underlying: str
    expiry: str
    kind: str
    detail: str
    symbols: list[str]
    severity: str          # "naked_short" | "unbalanced"


def find_breaches(positions: list[dict]) -> list[Breach]:
    """Detect option positions that are not part of a balanced vertical.

    A balanced vertical has one long and one short leg of the same kind and
    expiry, in equal quantity. Anything else is either a naked leg (urgent) or
    an unbalanced ratio (needs attention).
    """
    groups: dict[tuple[str, str, str], list[tuple[str, int]]] = defaultdict(list)
    for position in positions:
        if position.get("asset_class") != "us_option":
            continue
        parsed = parse_occ(position["symbol"])
        if not parsed:
            continue
        underlying, expiry, kind, _ = parsed
        qty = int(float(position["qty"]))
        if position.get("side") == "short" and qty > 0:
            qty = -qty
        groups[(underlying, expiry.isoformat(), kind)].append((position["symbol"], qty))

    breaches: list[Breach] = []
    for (underlying, expiry, kind), legs in groups.items():
        longs = sum(q for _, q in legs if q > 0)
        shorts = -sum(q for _, q in legs if q < 0)
        symbols = [s for s, _ in legs]

        if shorts > longs:
            breaches.append(Breach(
                underlying, expiry, kind,
                f"{shorts} short vs {longs} long: {shorts - longs} naked short "
                f"contract(s) - undefined risk",
                symbols, "naked_short",
            ))
        elif longs != shorts and shorts > 0:
            breaches.append(Breach(
                underlying, expiry, kind,
                f"{longs} long vs {shorts} short: unbalanced vertical",
                symbols, "unbalanced",
            ))
    return breaches


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
