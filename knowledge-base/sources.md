# Sources

Every URL consulted, what it answers, and its retrieval status. All retrieved **2026-09-01**.

**Access legend:** ✅ fetched cleanly · 🌐 browser-only (bot-blocked, HTTP 403 to plain fetchers) ·
⚠️ partially extracted

## Hackathon

| URL | Answers | Access |
|---|---|---|
| <https://lablab.ai/ai-hackathons/alpaca-ai-trading-agents-hackathon> | Rules, dates, prizes, judging, submissions, teams, speakers | 🌐 |
| <https://lablab.ai/ai-hackathons/alpaca-ai-trading-agents-hackathon/live> | Live event view | 🌐 |
| <https://storage.googleapis.com/lablab-static-eu/share/Hackathon-Setup-Guide-ALPACA26.pdf> | **Featherless** setup + credit redemption, 8 pages | ⚠️ image-based; text extraction failed, viewer would not render. **Read page 2 manually.** |
| <https://lablab.ai/tech/featherless> | Featherless tech page + setup guide link | 🌐 |

## Alpaca — entry points

| URL | Answers | Access |
|---|---|---|
| **<https://docs.alpaca.markets/us/llms.txt>** | **Complete doc index — every page, grouped. Append `.md` to any doc URL for clean markdown.** | ✅ |
| <https://docs.alpaca.markets/us/docs/getting-started> | Platform introduction | ✅ |
| <https://alpaca.markets/> | Company site | ✅ |
| <https://github.com/alpacahq> | All repos | ✅ |

> The `llms.txt` + `.md` trick is the single most useful discovery in this research pass. Prefer it
> over scraping HTML for anything else you need.

## Trading & platform

| URL | Answers | Access |
|---|---|---|
| <https://docs.alpaca.markets/us/docs/getting-started-with-trading-api> | Base URLs, X-Request-ID | ✅ (thin) |
| <https://docs.alpaca.markets/us/docs/paper-trading> | Account creation, $100k default, new-account flow, account ID location, **simulation limitations incl. random partial fills** | ✅ |
| <https://docs.alpaca.markets/us/docs/websocket-streaming.md> | `trade_updates` order lifecycle stream | not fetched |
| <https://docs.alpaca.markets/us/docs/orders-at-alpaca.md> | Order placement general | not fetched |

## Options

| URL | Answers | Access |
|---|---|---|
| <https://docs.alpaca.markets/us/docs/options-trading> | **Levels 1/2/3; options enabled by default on paper**; order validations; auto-exercise; paper NTA lag | ✅ |
| <https://docs.alpaca.markets/us/docs/options-level-3-trading> | **MLEG shape, legs[], ratio_qty GCD, no equity legs, all-legs-covered, universal spread rule margin** | ✅ |
| <https://docs.alpaca.markets/us/docs/options-orders> | OCC symbol format, buying-power arithmetic | ✅ (partial) |
| <https://docs.alpaca.markets/us/reference/optionsnapshots> | **Snapshot schema — greeks + impliedVolatility required; feed enum** | ✅ |
| <https://docs.alpaca.markets/us/reference/optionchain> | **Chain endpoint path + all filters + schema** | ✅ |

## Market data

| URL | Answers | Access |
|---|---|---|
| <https://docs.alpaca.markets/us/docs/about-market-data-api> | **Basic vs Algo Trader Plus: IEX, 15-min, 200 req/min, 30 WS, indicative** | ✅ |
| <https://docs.alpaca.markets/us/docs/historical-option-data> | **indicative vs opra definitions; history since Feb 2024** | ✅ |
| <https://docs.alpaca.markets/us/docs/real-time-option-data> | **Options WS URLs, msgpack-only, no `*` quotes** | ✅ |
| <https://docs.alpaca.markets/us/docs/getting-started-with-alpaca-market-data> | Data API intro | linked, not fetched |

## Agent tooling

| URL | Answers | Access |
|---|---|---|
| <https://docs.alpaca.markets/us/docs/alpaca-mcp-server> | **65-tool inventory, toolsets, env vars, per-client config, V1→V2 break** | ✅ |
| <https://github.com/alpacahq/alpaca-mcp-server> | FastMCP/OpenAPI architecture, MIT, transports, "90+ tools" | ✅ |
| <https://docs.alpaca.markets/us/docs/alpacas-cli> | **Full command tree, agent-oriented design, exit codes, retries, `--dry-run`, `--schema`, raw `api` passthrough** | ✅ |
| <https://github.com/alpacahq/cli> | CLI source | linked |
| <https://github.com/alpacahq/alpaca-skills> | Skill list, install via `npx skills add` | ✅ |
| <https://github.com/alpacahq/alpaca-py> | Python SDK | linked |
| <https://github.com/alpacahq/alpaca-trade-api-js> | JS SDK | linked |
| <https://docs.alpaca.markets/us/docs/sdks-and-tools> | SDK + OpenAPI spec index | linked |
| <https://alpaca.markets/blog/alpaca-launches-mcp-server-v2/> | V2 launch notes | not fetched |

## Architecture reference

| URL | Answers | Access |
|---|---|---|
| <https://alpaca.markets/learn/building-a-multi-agent-ai-trading-system-on-alpaca> | **Alpaca's own 5-agent + Critic + Risk Guard pipeline, code, limits, 32% approval rate** | ✅ |

## Partner

| URL | Answers | Access |
|---|---|---|
| <https://featherless.ai/docs/getting-started> | **OpenAI-compatible base URL, auth, models, Python example** | ✅ |
| <https://featherless.ai/> | Pricing, model catalogue | 🌐 |

## Community

| Channel | URL |
|---|---|
| Alpaca Slack | <https://alpaca.markets/slack> |
| Alpaca Forum | <https://forum.alpaca.markets/> |
| lablab.ai Discord | via the hackathon page |
| Alpaca support | support@alpaca.markets |

---

## Retrieval notes

- **lablab.ai returns HTTP 403 to plain HTTP fetchers.** Page content in this KB was read through a
  real browser session. Re-verify rules in a browser before relying on them.
- **The setup-guide PDF is image-based** (35 embedded images, no extractable text operators). Chrome's
  PDF viewer would not render into a screenshot either. Page 2 must be read by a human.
- Several `docs.alpaca.markets` pages returned thin content through the markdown converter —
  `getting-started-with-trading-api` and `options-orders` in particular. Where a fact mattered, it was
  sourced from the API reference instead.
- Everything in this KB is a **2026-09-01 snapshot**. Alpaca ships changes; re-verify the three
  highest-stakes claims (free-tier Greeks, options-on-paper default, MLEG equity-leg rejection)
  against your live account before betting the submission on them.
