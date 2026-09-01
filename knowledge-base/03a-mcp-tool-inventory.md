# 03a — Alpaca MCP Server: Full Tool Inventory (V2)

Source: <https://docs.alpaca.markets/us/docs/alpaca-mcp-server> (retrieved 2026-09-01).
The docs page enumerates **65 tools**; the GitHub README describes "90+ tools" across the toolsets —
treat 65 as the documented, stable set and verify anything beyond it against your installed version
with your MCP client's tool list.

Grouped by `ALPACA_TOOLSETS` value. ⭐ marks tools an options agent will actually use.

## `account` — 6
| Tool | Does |
|---|---|
| ⭐ `get_account_info` | balances, buying power, margin, status |
| `get_account_config` | trading restrictions, margin settings |
| `update_account_config` | modify account configuration |
| ⭐ `get_portfolio_history` | equity and P/L over time — **this is your judged equity curve** |
| `get_account_activities` | fills, dividends, transfers |
| `get_account_activities_by_type` | filtered by activity type |

## `trading` — 9 orders + 6 positions
| Tool | Does |
|---|---|
| `place_stock_order` | market, limit, stop, stop-limit, trailing stop, brackets |
| `place_crypto_order` | market, limit, stop-limit |
| ⭐ `place_option_order` | **single-leg or multi-leg strategies** |
| ⭐ `get_orders` | filterable order list |
| `get_order_by_id` | one order |
| ⭐ `get_order_by_client_id` | idempotency lookup |
| `replace_order_by_id` | amend an open order |
| `cancel_order_by_id` | cancel one |
| ⭐ `cancel_all_orders` | **kill switch, part 1** |
| ⭐ `get_all_positions` | current book |
| `get_open_position` | one position |
| ⭐ `close_position` | full or partial close |
| ⭐ `close_all_positions` | **kill switch, part 2** |
| `exercise_options_position` | exercise a held contract |
| `do_not_exercise_options_position` | DNE instruction |

## `assets` — 8
`get_all_assets` · `get_asset` · ⭐`get_option_contracts` · `get_option_contract` ·
⭐`get_calendar` · ⭐`get_clock` · `get_corporate_action_announcements` ·
`get_corporate_action_announcement`

## `stock-data` — 9
⭐`get_stock_bars` · `get_stock_quotes` · `get_stock_trades` · `get_stock_latest_bar` ·
⭐`get_stock_latest_quote` · `get_stock_latest_trade` · ⭐`get_stock_snapshot` ·
⭐`get_most_active_stocks` · ⭐`get_market_movers`

## `options-data` — 7 — **the core of an options agent**
| Tool | Does |
|---|---|
| `get_option_bars` | historical OHLCV (available since Feb 2024) |
| `get_option_trades` | historical trades |
| `get_option_latest_trade` | latest trade |
| ⭐ `get_option_latest_quote` | latest bid/ask — price your spread from this |
| ⭐ `get_option_snapshot` | **snapshot including Greeks and IV** |
| ⭐ `get_option_chain` | **full chain for an underlying, with Greeks and IV** |
| `get_option_exchange_codes` | exchange code → name |

## `crypto-data` — 8
`get_crypto_bars` · `get_crypto_quotes` · `get_crypto_trades` · `get_crypto_latest_bar` ·
`get_crypto_latest_quote` · `get_crypto_latest_trade` · `get_crypto_snapshot` ·
`get_crypto_latest_orderbook`

Crypto trades 24/7 — tempting when equity markets are shut. But the hackathon **requires options in
every strategy**, and there are no crypto options on Alpaca. Crypto can only ever be a side dish.

## `watchlists` — 7
`create_watchlist` · `get_watchlists` · `get_watchlist_by_id` · `update_watchlist_by_id` ·
`delete_watchlist_by_id` · `add_asset_to_watchlist_by_id` · `remove_asset_from_watchlist_by_id`

## `news` — 1
⭐ `get_news` — articles for stocks and crypto. The cheapest catalyst signal available on the free
tier, and the input several competing submissions are built on.

## `corporate-actions` — 1
`get_corporate_actions` — from the Market Data API.

## `fixed-income-data` — 1
`get_fixed_income_latest_quotes` — by ISIN.

## `index-data` — 2
`get_index_latest_values` · `get_index_values` — useful for VIX-style regime context **if** your plan
covers the index. Verify availability on the free tier before designing around it.

---

## Suggested toolset split

```bash
# Analyst / reasoning agent — cannot trade, by construction
ALPACA_TOOLSETS="account,assets,stock-data,options-data,news,index-data"

# Executor — only if you route execution through MCP rather than the CLI
ALPACA_TOOLSETS="trading,account,assets"
```

## Safety notes from the docs, verbatim in spirit

- **"Never paste API keys into chat."** Configure them in the MCP client's `env` block or an OS
  secret store.
- Orders execute **directly** against Alpaca's Trading API. There is no confirmation step.
- The server defaults to **paper** (`ALPACA_PAPER_TRADE=true`). Only `false` + live keys touch real
  capital.
- Alpaca enforces per-account rate limits; high-frequency querying will throttle.
- Some real-time data requires an Algo Trader Plus subscription.
- Alpaca's own disclaimer: insights from the MCP server and connected agents are **"for educational
  and informational purposes only and should not be taken as investment advice."**

## Connection check
Restart the MCP client and ask: *"What is my Alpaca account balance and buying power?"* A correct
account summary confirms the wiring.
