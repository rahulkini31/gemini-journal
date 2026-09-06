# 03 — Agent Surfaces: MCP vs CLI vs SDK vs Skills

The hackathon **requires** the MCP server or the CLI. This page is the decision matrix for which to
use where.

## The four surfaces

| Surface | Shape | Best at | Cost / caveat |
|---|---|---|---|
| **MCP server** | ~65–90 tools across 9+ toolsets; FastMCP generated from Alpaca's OpenAPI specs; stdio + streamable-http | LLM-driven exploration and inspection; the visibly "agentic" story judges can see | Large tool surface eats context; a model with 90 tools picks wrong ones |
| **Alpaca CLI** | `alpaca <group> <cmd> --flags`; JSON default, `--csv`, `--jq`; exit codes 0/1/2; auto-retry on 429/5xx | cron, CI, long-running loops, the deterministic execution path | Needs Go or Homebrew install |
| **alpaca-py SDK** | Typed request/response objects | Data pipelines, backtests, risk math, Greeks | **Does not satisfy the MCP-or-CLI requirement on its own** |
| **alpaca-skills** | Drop-in `SKILL.md` files for coding agents | Speeding up *your* development in Claude Code | A dev-time aid, not a runtime component |

## Recommended shape: hybrid

> **MCP for reasoning and inspection. CLI for the execution path.**

This satisfies the requirement twice over, and it maps cleanly onto the architectural pattern the
entire field has converged on (`07-competitive-landscape.md`): the model *reads* through MCP and
*proposes*; deterministic code *writes* through the CLI, after gates. The read/write split is not
decoration — it is enforceable.

Enforce it with `ALPACA_TOOLSETS`: give the LLM-facing MCP server **read-only toolsets** and let it
have no `trading` tools at all. Then the model *cannot* place an order even if prompt-injected or
confused; only your gated CLI path can.

```jsonc
// The LLM's MCP server — physically incapable of trading
{
  "mcpServers": {
    "alpaca-read": {
      "command": "uvx",
      "args": ["alpaca-mcp-server"],
      "env": {
        "ALPACA_API_KEY": "…",
        "ALPACA_SECRET_KEY": "…",
        "ALPACA_PAPER_TRADE": "true",
        "ALPACA_TOOLSETS": "account,stock-data,options-data,crypto-data,news,assets"
      }
    }
  }
}
```

That single env var is a real, demonstrable safety primitive and it is worth a slide.

## MCP server

- Docs: <https://docs.alpaca.markets/us/docs/alpaca-mcp-server>
- Repo: <https://github.com/alpacahq/alpaca-mcp-server> (MIT)
- V2 blog: <https://alpaca.markets/blog/alpaca-launches-mcp-server-v2/>

### Install
Requires Python 3.10+ and `uv`/`uvx`.

```bash
# Claude Code
claude mcp add alpaca --scope user --transport stdio uvx alpaca-mcp-server \
  --env ALPACA_API_KEY=your_key \
  --env ALPACA_SECRET_KEY=your_secret
```

```jsonc
// Claude Desktop / Cursor (~/.cursor/mcp.json) / VS Code (.vscode/mcp.json)
{ "mcpServers": { "alpaca": {
    "command": "uvx", "args": ["alpaca-mcp-server"],
    "env": { "ALPACA_API_KEY": "…", "ALPACA_SECRET_KEY": "…" } } } }
```

```bash
# Docker
git clone https://github.com/alpacahq/alpaca-mcp-server.git && cd alpaca-mcp-server
docker build -t mcp/alpaca:latest .
docker run -e ALPACA_API_KEY=… -e ALPACA_SECRET_KEY=… mcp/alpaca:latest
```

### Environment variables

| Var | Required | Default | Purpose |
|---|---|---|---|
| `ALPACA_API_KEY` | yes | — | key id |
| `ALPACA_SECRET_KEY` | yes | — | secret |
| `ALPACA_PAPER_TRADE` | no | **`true`** | `false` = live money |
| `ALPACA_TOOLSETS` | no | all | comma-separated allow-list |

### Toolsets
`account`, `trading`, `watchlists`, `assets`, `stock-data`, `crypto-data`, `options-data`,
`corporate-actions`, `news`, `fixed-income-data`, `index-data`, `locates`

### Tools that matter for an options agent

| Tool | Why |
|---|---|
| `get_option_chain` | full chain for an underlying — **returns Greeks + IV** |
| `get_option_snapshot` | per-contract snapshot with Greeks and IV |
| `get_option_latest_quote` | bid/ask for pricing a spread |
| `get_option_contracts` | discovery by expiry/strike |
| `place_option_order` | single-leg **and multi-leg** |
| `get_stock_bars` / `get_stock_snapshot` | underlying context, realised vol |
| `get_market_movers`, `get_most_active_stocks` | candidate screening |
| `get_news` | catalyst / event awareness |
| `get_clock`, `get_calendar` | is the market open |
| `get_account_info`, `get_portfolio_history` | equity curve, buying power |
| `get_all_positions`, `close_position` | position management |
| `cancel_all_orders`, `close_all_positions` | **kill switch** |

Full inventory in `03a-mcp-tool-inventory.md`.

### Version warning
**V2 is not backward compatible with V1** — tool names and parameters changed. Blog posts and
tutorials written before V2 will not work. Pin V1 only if you must:
`"args": ["alpaca-mcp-server==1.x.x", "serve"]`.

## Alpaca CLI

- Docs: <https://docs.alpaca.markets/us/docs/alpacas-cli>
- Repo: <https://github.com/alpacahq/cli>

```bash
brew install alpacahq/tap/cli          # or: go install github.com/alpacahq/cli/cmd/alpaca@latest
alpaca version && alpaca doctor
alpaca profile login                    # OAuth, paper by default
alpaca profile login --api-key          # or key-based
```

Config via env: `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, `ALPACA_LIVE_TRADE`, `ALPACA_PROFILE`,
`ALPACA_OUTPUT` (`json`|`csv`), `ALPACA_CONFIG_DIR`.

### Why it is built for agents
Alpaca designed this CLI for exactly this use case, and says so:

- **"Commands execute immediately. No interactive 'are you sure?' dialogs."**
- Structured JSON errors on stderr; exit codes **0** success, **1** error, **2** auth.
- **Automatic retry on 429/5xx**, max 3, respects `Retry-After`.
- `--dry-run` previews an order without placing it.
- `--schema` prints a response shape **without making an API call** — cheap for prompt-building.
- `--jq '<filter>'` filters JSON inline; `--quiet` suppresses noise.

### Idempotent order pattern — use this
```bash
CLIENT_ORDER_ID="$(uuidgen)"
alpaca order submit --symbol AAPL --side buy --qty 10 --type market \
  --client-order-id "$CLIENT_ORDER_ID"
```

### Command groups
`account` · `order` · `position` · `option` · `data` · `watchlist` · `asset` · `clock` · `calendar`
· `profile` · `api` (raw passthrough)

Options-specific:
```bash
alpaca option contracts --underlying-symbol AAPL
alpaca option get --symbol-or-id AAPL250620C00200000
alpaca option exercise --symbol-or-id <contract>
alpaca option do-not-exercise --symbol-or-id <contract>
alpaca data option chain --underlying-symbol AAPL
alpaca data option snapshot --symbol AAPL250620C00200000
alpaca data option latest-quotes --symbol AAPL250620C00200000
```

**Escape hatch — raw API passthrough.** The CLI has no first-class multi-leg submit flag documented,
but it can post any payload:
```bash
echo '{"order_class":"mleg","qty":"1","type":"limit","limit_price":"1.00",
       "time_in_force":"day","legs":[…]}' | alpaca api POST /v2/orders
```
This is how you place spreads through the CLI while still satisfying the CLI requirement.

✅ **Verified working 2026-09-01.** A two-leg vertical and a four-leg structure were both accepted
(`status: accepted`, `order_class: mleg`, legs intact). The hybrid design below is viable as written.
Note the CLI writes error JSON to **stderr**, so capture `2>&1` when parsing failures.

## SDKs

| SDK | Repo |
|---|---|
| Python `alpaca-py` | <https://github.com/alpacahq/alpaca-py> |
| JS/TS | <https://github.com/alpacahq/alpaca-trade-api-js> |
| SDKs & OpenAPI specs | <https://docs.alpaca.markets/us/docs/sdks-and-tools> |

Use `alpaca-py` for the analytical layer — chain parsing, IV rank, spread construction, position
sizing. It is not a substitute for MCP/CLI under the rules.

## alpaca-skills

- Repo: <https://github.com/alpacahq/alpaca-skills>
- Install: `npx skills add alpacahq/alpaca-skills` (`--list`, `--skill <name>`)
- Claude Code path: `~/.claude/skills/`

Trading-API skills relevant here: `alpaca-trading-backtest`, `alpaca-trading-paper-trading`,
`alpaca-trading-paper-trading-cli`, `alpaca-trading-paper-trading-mcp`. The remaining ten are
Broker-API skills — irrelevant to this hackathon.

---

### What this page implies for design

1. **Run two MCP configurations, or one read-only MCP plus a CLI writer.** `ALPACA_TOOLSETS` makes
   "the model cannot place an order" a structural fact rather than a promise.
2. **Use both required surfaces**, not one — it scores directly against "Technology Implementation"
   and costs almost nothing.
3. `--dry-run`, `--schema`, `--jq` and exit codes make the CLI a genuinely good execution substrate.
   Do not reimplement retry logic it already has.
4. Pin to MCP **V2** semantics; ignore pre-V2 tutorials.
