"""Configuration and risk budget. Everything tunable lives here."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def load_dotenv(path: Path | None = None) -> None:
    """Minimal .env loader. Existing environment variables win."""
    path = path or REPO_ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


DATA_HOST = "https://data.alpaca.markets"
PAPER_HOST = "https://paper-api.alpaca.markets"


@dataclass(frozen=True)
class RiskBudget:
    """Deterministic bounds. See ARCHITECTURE.md for the rationale of each."""

    # per-trade
    max_loss_per_trade_pct: float = 0.01      # 1% of equity
    # portfolio
    max_total_open_risk_pct: float = 0.05     # 5% of equity across all open structures
    max_abs_delta: float = 150.0              # share-equivalents
    max_abs_vega: float = 400.0               # $ per 1 vol point
    max_abs_gamma: float = 50.0
    min_theta: float = -250.0                 # $/day floor (theta is negative when long premium)
    max_gross_delta: float = 1000.0           # do not let cross-underlying netting hide size
    max_dollar_delta_1pct_equity_pct: float = 0.02
    max_local_stress_loss_pct: float = 0.03    # delta/gamma/vega correlated shock
    max_positions_per_underlying: int = 2
    # circuit breaker
    daily_drawdown_halt_pct: float = 0.03     # -3% on the day stops trading
    # contract selection
    min_dte: int = 14                          # sub-2-week options price off delayed
                                              # quotes badly: theta and gamma move too fast
    max_dte: int = 45
    max_quote_spread_pct: float = 0.15        # measured live: near-ATM SPY quoted 5.63/6.83 = ~19% wide
    min_bid: float = 0.05                     # avoid untradeable pennies
    min_quote_size: int = 1
    max_quote_age_seconds: float = 120.0
    max_underlying_age_seconds: float = 120.0
    max_leg_quote_skew_seconds: float = 30.0
    # spread shape: keep candidates genuinely spread-like
    min_short_delta: float = 0.15             # short leg must finance the long
    max_short_delta: float = 0.40             # ...without capping upside too early
    debit_long_target_deltas: tuple[float, ...] = (0.35, 0.45, 0.55)
    credit_short_target_deltas: tuple[float, ...] = (0.15, 0.25, 0.35)
    anchor_delta_tolerance: float = 0.08
    min_credit_long_delta: float = 0.03
    max_credit_long_delta: float = 0.25
    max_width_pct_of_spot: float = 0.06       # a 58-wide SPY spread is a naked long
    min_credit_width_ratio: float = 0.20      # collect at least 20% of width
    max_debit_width_ratio: float = 0.75       # do not spend nearly all potential value
    min_reward_risk_ratio: float = 0.25       # hard tail-asymmetry floor
    credit_short_target_delta: float = 0.25   # short leg ~25 delta: premium with room
    max_edge_erosion_pct: float = 0.30        # execution drag vs max loss
    require_positive_expectancy: bool = False # requires calibrated, policy-aware model output
    shortlist_size: int = 10
    max_shortlist_per_cluster: int = 2
    max_shortlist_per_anchor: int = 1
    shortlist_correlation_groups: tuple[tuple[str, ...], ...] = (
        ("SPY", "QQQ", "IWM"),
    )
    max_shortlist_per_factor_direction: int = 3
    max_shortlist_per_factor_direction_dte: int = 2
    shortlist_dte_bucket_days: int = 7
    max_reprice_deterioration_pct: float = 0.10
    # scheduled event and American-option assignment controls
    require_event_calendar: bool = False
    macro_entry_blackout_hours: float = 24.0
    macro_post_event_blackout_minutes: float = 30.0
    ex_dividend_lookahead_days: int = 5
    assignment_extrinsic_buffer: float = 0.05
    short_put_carry_rate: float = 0.05
    # exits - entries without exits is not a strategy
    exit_profit_target_pct: float = 0.50      # bank at +50% of cost basis
    exit_stop_loss_pct: float = 0.60          # cut at -60%, before max loss
    exit_time_stop_dte: int = 3               # never hold into expiry week


@dataclass(frozen=True)
class Settings:
    api_key: str
    secret_key: str
    paper: bool = True
    universe: tuple[str, ...] = ("SPY", "QQQ", "IWM")
    risk: RiskBudget = field(default_factory=RiskBudget)
    db_path: Path = REPO_ROOT / "state" / "bookbound.db"
    audit_path: Path = REPO_ROOT / "state" / "decisions.jsonl"
    # None keeps library/tests side-effect free; load_settings enables captures.
    capture_root: Path | None = None
    # A finite, source-labelled JSON snapshot. None is explicit unavailability.
    event_calendar_path: Path | None = None
    # LLM
    featherless_key: str = ""
    featherless_base: str = "https://api.featherless.ai/v1"
    proposer_model: str = "Qwen/Qwen2.5-72B-Instruct"
    adversary_model: str = "zai-org/GLM-5.2"
    # GLM-5.2 is a reasoning model: it spends hundreds of tokens before answering,
    # and a tight budget yields an empty string rather than an error.
    adversary_max_tokens: int = 2500

    @property
    def auth_headers(self) -> dict[str, str]:
        return {
            "APCA-API-KEY-ID": self.api_key,
            "APCA-API-SECRET-KEY": self.secret_key,
        }


def load_settings() -> Settings:
    load_dotenv()
    key = os.environ.get("ALPACA_API_KEY", "")
    secret = os.environ.get("ALPACA_SECRET_KEY", "")
    if not key or not secret:
        raise RuntimeError(
            "ALPACA_API_KEY / ALPACA_SECRET_KEY not set. Copy .env.example to .env."
        )
    settings = Settings(
        api_key=key,
        secret_key=secret,
        paper=os.environ.get("ALPACA_PAPER_TRADE", "true").lower() != "false",
        featherless_key=os.environ.get("FEATHERLESS_API_KEY", ""),
        capture_root=Path(os.environ.get(
            "BOOKBOUND_CAPTURE_ROOT", str(REPO_ROOT / "state" / "captures")
        )),
        event_calendar_path=(
            Path(os.environ["BOOKBOUND_EVENT_CALENDAR"])
            if os.environ.get("BOOKBOUND_EVENT_CALENDAR") else None
        ),
    )
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    return settings
