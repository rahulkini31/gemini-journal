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
    max_positions_per_underlying: int = 2
    # circuit breaker
    daily_drawdown_halt_pct: float = 0.03     # -3% on the day stops trading
    # contract selection
    min_dte: int = 7
    max_dte: int = 45
    max_quote_spread_pct: float = 0.15        # measured live: near-ATM SPY quoted 5.63/6.83 = ~19% wide
    min_bid: float = 0.05                     # avoid untradeable pennies
    # spread shape: keep candidates genuinely spread-like
    min_short_delta: float = 0.15             # short leg must finance the long
    max_short_delta: float = 0.40             # ...without capping upside too early
    max_width_pct_of_spot: float = 0.06       # a 58-wide SPY spread is a naked long
    max_edge_erosion_pct: float = 0.30        # execution drag vs max loss
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
    )
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    return settings
