# MCP configuration

The hackathon requires the Alpaca **MCP server or CLI**. Bookbound uses both, split by
capability:

- **MCP — inspection only.** `ALPACA_TOOLSETS` deliberately omits `trading`, so the
  model-facing server exposes no order-placing tool at all. The model is not *trusted*
  not to trade; it is *unable* to.
- **CLI — execution.** All orders go through `alpaca api POST /v2/orders` with
  `order_class: mleg`, behind the deterministic risk engine.

Copy `alpaca-readonly.json` into your client (`~/.cursor/mcp.json`,
`claude_desktop_config.json`, or `.vscode/mcp.json`), or register it with Claude Code:

```bash
claude mcp add alpaca-read --scope project --transport stdio \
  uvx alpaca-mcp-server \
  --env ALPACA_API_KEY="$ALPACA_API_KEY" \
  --env ALPACA_SECRET_KEY="$ALPACA_SECRET_KEY" \
  --env ALPACA_PAPER_TRADE=true \
  --env ALPACA_TOOLSETS=account,assets,stock-data,options-data,news
```

Verify the restriction actually holds by asking the model to place an order — it should
report having no tool for it.
