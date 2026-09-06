# Knowledge Graph

Five views over the same entity set. The machine-readable form is `graph.json` — every node here
appears there with typed edges, facts and source URLs.

**Edge legend:** solid `-->` provides/enables · dashed `-.->` requires/depends · thick `==>` blocks
or constrains.

---

## View 1 · Platform & agent surfaces

```mermaid
graph TD
  ALPACA["Alpaca<br/>programmable brokerage"]
  TAPI["Trading API<br/>paper-api.alpaca.markets"]
  MDAPI["Market Data API<br/>data.alpaca.markets"]
  BAPI["Broker API<br/>OUT OF SCOPE"]

  ALPACA --> TAPI
  ALPACA --> MDAPI
  ALPACA --> BAPI

  MCP["MCP Server<br/>65-90 tools, 9+ toolsets"]
  CLI["Alpaca CLI<br/>JSON out, exit codes, retries"]
  SDK["alpaca-py / alpaca-trade-api-js"]
  SKILLS["alpaca-skills<br/>SKILL.md for coding agents"]

  TAPI --> MCP
  TAPI --> CLI
  TAPI --> SDK
  MDAPI --> MCP
  MDAPI --> CLI
  MDAPI --> SDK
  SKILLS -.->|dev-time aid| CLI

  REQ{{"RULE: must use<br/>MCP or CLI"}}
  REQ ==> MCP
  REQ ==> CLI
  REQ -.->|does NOT satisfy| SDK

  TOOLSETS["ALPACA_TOOLSETS<br/>allow-list env var"]
  TOOLSETS -->|makes read-only<br/>structurally enforced| MCP

  PAPER["Paper environment<br/>$100k default, free"]
  TAPI --> PAPER

  classDef rule fill:#7c2d12,stroke:#ea580c,color:#fff
  classDef out fill:#374151,stroke:#6b7280,color:#9ca3af
  class REQ rule
  class BAPI out
```

---

## View 2 · Options execution path and its prerequisites

```mermaid
graph TD
  L3["Options Level 3"]
  PAPERDEF["Paper: options enabled<br/>BY DEFAULT"]
  PAPERDEF --> L3

  MLEG["order_class: mleg"]
  L3 -.->|requires| MLEG

  VERT["Vertical spreads<br/>call / put"]
  CONDOR["Iron condor"]
  ROLL["Rolls"]
  MLEG --> VERT
  MLEG --> CONDOR
  MLEG --> ROLL

  LEGS["legs[]<br/>symbol · ratio_qty · side<br/>position_intent"]
  MLEG --> LEGS
  GCD["ratio_qty GCD must be 1"]
  LEGS -.-> GCD

  NOEQ["NO equity legs"]
  COVERED["All legs covered<br/>in the same order"]
  LIMITDAY["limit + day only<br/>(documented)"]
  MLEG ==> NOEQ
  MLEG ==> COVERED
  MLEG ==> LIMITDAY

  NOFILL["Unfilled spread<br/>expires at close"]
  LIMITDAY ==> NOFILL

  MARGIN["Universal spread rule<br/>max theoretical loss,<br/>premium excluded"]
  MLEG --> MARGIN
  BASIS["cost basis =<br/>maint. margin + net premium x 100"]
  MARGIN --> BASIS

  EXP["Auto-exercise if<br/>&gt;= $0.01 ITM"]
  NTA["Paper NTAs sync<br/>NEXT DAY"]
  EXP ==> NTA

  OCC["OCC symbol<br/>AAPL231201C00195000"]
  LEGS -.-> OCC

  classDef block fill:#7f1d1d,stroke:#ef4444,color:#fff
  class NOEQ,COVERED,LIMITDAY,NOFILL,NTA block
```

---

## View 3 · Data tiers — what is actually available for free

```mermaid
graph TD
  BASIC["Basic plan<br/>FREE - assume this"]
  PLUS["Algo Trader Plus<br/>$99/mo - prize only"]

  BASIC --> IEX["Stocks: IEX only"]
  BASIC --> D15["Latest 15 min withheld"]
  BASIC --> RL200["200 req/min"]
  BASIC --> WS30["30 stock WS symbols"]
  BASIC --> IND["Options: indicative feed"]

  PLUS --> SIP["Stocks: full SIP"]
  PLUS --> OPRA["Options: OPRA"]
  PLUS --> RL10K["10,000 req/min"]

  IND --> DERIV["Quotes are DERIVED<br/>trades delayed 15 min"]
  IND --> GREEKS["greeks: delta gamma<br/>theta vega rho"]
  IND --> IV["impliedVolatility<br/>Black-Scholes"]

  GREEKS --> STRAT1["Delta-based selection"]
  IV --> STRAT2["IV rank / VRP harvesting"]
  GREEKS --> STRAT3["Portfolio Greek budgeting"]

  DERIV ==>|so| RANKONLY["Rank on Greeks,<br/>PRICE on latestQuote"]

  CHAIN["GET /v1beta1/options/<br/>snapshots/{underlying}"]
  CHAIN --> GREEKS
  CHAIN --> FILTERS["strike_price_gte/lte<br/>expiration_date*<br/>type · limit &lt;= 1000"]
  RL200 ==>|forces| FILTERS

  WSOPT["wss .../v1beta1/{feed}<br/>msgpack only, no '*' quotes"]
  IND --> WSOPT
  WSOPT ==> POLL["REST polling is<br/>the right call here"]

  classDef good fill:#14532d,stroke:#22c55e,color:#fff
  classDef warn fill:#7f1d1d,stroke:#ef4444,color:#fff
  class GREEKS,IV,STRAT1,STRAT2,STRAT3 good
  class DERIV,RANKONLY,POLL warn
```

---

## View 4 · Hackathon rules → constraints → design implications

```mermaid
graph LR
  subgraph RULES["Rules"]
    R1["Autonomous agent"]
    R2["MCP or CLI"]
    R3["Options in ALL strategies"]
    R4["Fresh paper account"]
    R5["$100,000 balance"]
    R6["Submit account ID"]
    R7["One-page write-up"]
  end

  subgraph CONS["Constraints"]
    C1["No human in the loop"]
    C2["Both surfaces, ideally"]
    C3["Level 3 spreads"]
    C4["Create account EARLY"]
    C5["3.5 market sessions"]
  end

  subgraph IMPL["Design implications"]
    I1["Deterministic gates replace<br/>the human approver"]
    I2["MCP read-only + CLI execute"]
    I3["Defined-risk MLEG"]
    I4["History must accumulate"]
    I5["P&L is NOISE -<br/>bound it, don't chase it"]
    I6["Win on the 4 artefact criteria"]
  end

  R1 --> C1 --> I1
  R2 --> C2 --> I2
  R3 --> C3 --> I3
  R4 --> C4 --> I4
  R5 --> C4
  R6 --> C4
  DEADLINE["Deadline<br/>4 Sep 15:00 UTC"] --> C5 --> I5 --> I6
  R7 --> I6

  JUDGE["Judging: P&L · Tech ·<br/>Creativity · Presentation · Social"]
  JUDGE --> I6

  classDef imp fill:#1e3a5f,stroke:#3b82f6,color:#fff
  class I1,I2,I3,I4,I5,I6 imp
```

---

## View 5 · Competitive pattern map

```mermaid
graph TD
  FIELD["~41 submissions<br/>~35 genuinely competitive"]

  P1["GOVERNANCE / AUDIT<br/>~15 projects"]
  P2["MULTI-AGENT DEBATE<br/>~6 projects"]
  P3["PREMIUM SELLING / IV<br/>~6 projects"]
  P4["NEWS / SENTIMENT<br/>~4 projects"]
  P5["RESEARCH LOOPS<br/>~3 projects"]
  P6["PORTFOLIO / HEDGING<br/>~3 projects"]
  P7["WEAK / NON-COMPLIANT<br/>~4 projects"]

  FIELD --> P1 & P2 & P3 & P4 & P5 & P6 & P7

  PATTERN["'LLM proposes,<br/>deterministic code disposes'"]
  P1 --> PATTERN
  PATTERN --> TABLESTAKES["TABLE STAKES<br/>build it, don't lead with it"]

  P5 --> OPEN1["OPEN: closed-loop<br/>strategy evolution"]
  P6 --> OPEN2["OPEN: portfolio-level<br/>Greek management"]
  GAP1["OPEN: honest feed +<br/>partial-fill modelling"]
  GAP2["OPEN: execution quality<br/>/ non-fill handling"]
  FIELD -.->|nobody claims it| GAP1
  FIELD -.->|nobody claims it| GAP2

  JUDGES["Judges include Alpaca's<br/>Trading API lead +<br/>Chief Brokerage Officer"]
  JUDGES ==>|rewards| GAP1

  classDef open fill:#14532d,stroke:#22c55e,color:#fff
  classDef sat fill:#7f1d1d,stroke:#ef4444,color:#fff
  class OPEN1,OPEN2,GAP1,GAP2 open
  class TABLESTAKES,P1,P2 sat
```

---

## How to use this graph

- **Designing a strategy?** Start at View 3 — it tells you what data you actually have.
- **Designing execution?** View 2 — it tells you what orders you can actually place.
- **Choosing a differentiator?** View 5 — it tells you what is already taken.
- **Checking eligibility?** View 4 — every rule traced to its consequence.
- **Programmatic use?** Load `graph.json`; each node carries `facts[]` and `source_urls[]`.
