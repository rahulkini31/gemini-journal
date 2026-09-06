#!/usr/bin/env python3
"""Generate a static dashboard from the agent's own audit trail.

Answers the 'demo application URL' requirement without any server: the page is
built from state/decisions.jsonl plus a live account snapshot, written to
docs/index.html, and served by GitHub Pages straight from the repository.

    PYTHONPATH=src python3 scripts/build_dashboard.py
"""
from __future__ import annotations

import collections
import html
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bookbound.audit import AuditLog          # noqa: E402
from bookbound.book import load_book          # noqa: E402
from bookbound.config import load_settings    # noqa: E402
from bookbound.exits import group_option_positions  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def _alpaca(*args: str) -> object:
    try:
        out = subprocess.run(["alpaca", *args], capture_output=True, text=True,
                             timeout=45).stdout
        return json.loads(out) if out.strip() else None
    except Exception:
        return None


def _esc(value: object) -> str:
    return html.escape(str(value))


def build() -> Path:
    settings = load_settings()
    records = AuditLog(settings.audit_path).read_all()
    book = load_book(settings, {}, spots={})
    orders = _alpaca("order", "list", "--status", "all") or []
    filled = [o for o in orders if o.get("status") == "filled"]

    groups = list(group_option_positions(book.raw_positions))
    at_risk = sum(abs(g.cost_basis) for g in groups)
    unrealised = sum(g.unrealized_pl for g in groups)
    cap = book.equity * settings.risk.max_total_open_risk_pct

    decisions = [r for r in records if r["event"] in
                 ("order_submitted", "no_trade", "refused", "exit")]
    outcomes = collections.Counter(
        r.get("outcome") or r["event"] for r in decisions
    )
    vetoes = [r for r in records
              if r["event"] == "no_trade" and r.get("outcome") == "VETOED"]

    rows_pos = "".join(
        f"<tr><td>{_esc(g.underlying)}</td><td>{_esc(g.expiry)}</td>"
        f"<td>{_esc(g.kind)}</td>"
        f"<td>{'balanced' if g.is_balanced_vertical else 'UNBALANCED'}</td>"
        f"<td class='n'>${abs(g.cost_basis):,.0f}</td>"
        f"<td class='n {'up' if g.unrealized_pl >= 0 else 'dn'}'>"
        f"{g.unrealized_pl:+,.0f}</td></tr>"
        for g in groups
    ) or "<tr><td colspan='6'>no open structures</td></tr>"

    rows_fill = "".join(
        f"<tr><td>{_esc(o.get('submitted_at',''))[:19]}</td>"
        f"<td>{_esc(o.get('order_class'))}</td>"
        f"<td class='n'>{_esc(o.get('limit_price'))}</td>"
        f"<td class='n'>{_esc(o.get('filled_avg_price'))}</td>"
        f"<td>{_esc(o.get('status'))}</td></tr>"
        for o in filled
    ) or "<tr><td colspan='5'>no fills yet</td></tr>"

    rows_veto = "".join(
        f"<tr><td>{_esc(r['ts'])[:19]}</td><td>{_esc(r.get('reason'))[:220]}</td></tr>"
        for r in vetoes[-8:]
    ) or "<tr><td colspan='2'>no vetoes recorded</td></tr>"

    chips = "".join(
        f"<span class='chip'>{_esc(k)} <b>{v}</b></span>"
        for k, v in sorted(outcomes.items(), key=lambda kv: -kv[1])
    )

    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Bookbound — live agent state</title>
<style>
:root{{--bg:#0d1117;--fg:#e6edf3;--mut:#8b949e;--line:#30363d;--card:#161b22;
--up:#3fb950;--dn:#f85149;--ac:#58a6ff}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.6 ui-sans-serif,-apple-system,Segoe UI,Roboto,sans-serif;padding:2rem 1.25rem}}
.wrap{{max-width:1000px;margin:0 auto}}
h1{{font-size:1.6rem;margin:0 0 .25rem}}
.sub{{color:var(--mut);margin:0 0 1.5rem}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:.75rem;margin-bottom:1.5rem}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:1rem}}
.card .k{{color:var(--mut);font-size:.78rem;text-transform:uppercase;letter-spacing:.04em}}
.card .v{{font-size:1.45rem;font-weight:600;margin-top:.2rem}}
.up{{color:var(--up)}}.dn{{color:var(--dn)}}
table{{width:100%;border-collapse:collapse;background:var(--card);
border:1px solid var(--line);border-radius:10px;overflow:hidden;margin-bottom:1.5rem}}
th,td{{padding:.55rem .7rem;text-align:left;border-bottom:1px solid var(--line);font-size:.9rem}}
th{{color:var(--mut);font-weight:600;font-size:.75rem;text-transform:uppercase}}
tr:last-child td{{border-bottom:none}}
td.n{{text-align:right;font-variant-numeric:tabular-nums}}
h2{{font-size:1rem;margin:1.5rem 0 .6rem;color:var(--ac)}}
.chip{{display:inline-block;background:var(--card);border:1px solid var(--line);
border-radius:999px;padding:.2rem .7rem;margin:0 .35rem .35rem 0;font-size:.82rem}}
.note{{color:var(--mut);font-size:.82rem;border-top:1px solid var(--line);padding-top:1rem}}
.scroll{{overflow-x:auto}}
</style></head><body><div class="wrap">
<h1>Bookbound</h1>
<p class="sub">Autonomous options agent · Alpaca paper account
<code>{_esc(book.raw_positions and 'PA3S4EFAEQLX' or 'PA3S4EFAEQLX')}</code> ·
generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC</p>

<div class="grid">
  <div class="card"><div class="k">Equity</div><div class="v">${book.equity:,.2f}</div></div>
  <div class="card"><div class="k">Day P&amp;L</div>
    <div class="v {'up' if book.day_pnl_pct >= 0 else 'dn'}">{book.day_pnl_pct:+.2%}</div></div>
  <div class="card"><div class="k">Open structures</div><div class="v">{len(groups)}</div></div>
  <div class="card"><div class="k">Capital at risk</div>
    <div class="v">${at_risk:,.0f}</div></div>
  <div class="card"><div class="k">Portfolio cap</div><div class="v">${cap:,.0f}</div></div>
  <div class="card"><div class="k">Unrealised</div>
    <div class="v {'up' if unrealised >= 0 else 'dn'}">${unrealised:+,.0f}</div></div>
</div>

<h2>Book greeks</h2>
<div class="grid">
  <div class="card"><div class="k">Delta</div><div class="v">{book.greeks.as_dict()['delta']:+,.1f}</div></div>
  <div class="card"><div class="k">Gamma</div><div class="v">{book.greeks.as_dict()['gamma']:+,.2f}</div></div>
  <div class="card"><div class="k">Theta</div><div class="v">{book.greeks.as_dict()['theta']:+,.1f}</div></div>
  <div class="card"><div class="k">Vega</div><div class="v">{book.greeks.as_dict()['vega']:+,.1f}</div></div>
</div>

<h2>Open structures</h2>
<div class="scroll"><table><thead><tr><th>Underlier</th><th>Expiry</th><th>Kind</th>
<th>Integrity</th><th class="n">At risk</th><th class="n">Unrealised</th></tr></thead>
<tbody>{rows_pos}</tbody></table></div>

<h2>Filled orders</h2>
<div class="scroll"><table><thead><tr><th>Submitted (UTC)</th><th>Class</th>
<th class="n">Limit</th><th class="n">Fill</th><th>Status</th></tr></thead>
<tbody>{rows_fill}</tbody></table></div>

<h2>Decision outcomes</h2>
<p>{chips}</p>

<h2>Model vetoes — every refusal is recorded with its reason</h2>
<div class="scroll"><table><thead><tr><th>When (UTC)</th><th>Cited hazard and evidence</th></tr></thead>
<tbody>{rows_veto}</tbody></table></div>

<p class="note">Static page built from the agent's own append-only audit log
(<code>state/decisions.jsonl</code>) and a live account snapshot. Deterministic code selects
every trade; the language model may only raise a substantiated objection. Paper trading only —
results are hypothetical and this is not investment advice.</p>
</div></body></html>"""

    out = ROOT / "docs" / "index.html"
    out.write_text(page, encoding="utf-8")
    return out


if __name__ == "__main__":
    path = build()
    print(f"wrote {path} ({path.stat().st_size:,} bytes)")
