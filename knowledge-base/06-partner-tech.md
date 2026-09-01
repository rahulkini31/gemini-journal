# 06 — Partner Technology

## Featherless AI — the only named technology partner

> Tech page: <https://lablab.ai/tech/featherless> (bot-blocked; open in browser)
> Site: <https://featherless.ai/> · Docs: <https://featherless.ai/docs/getting-started>
> Hackathon setup guide (PDF): <https://storage.googleapis.com/lablab-static-eu/share/Hackathon-Setup-Guide-ALPACA26.pdf>

Serverless inference for **open-source models** — you call hosted open models without provisioning
GPUs.

| | |
|---|---|
| Credits | **$25 per participant** |
| Access | **First-come, first-served** |
| Validity | Pay-per-request, active until credits run out |
| Base URL | `https://api.featherless.ai/v1` |
| Compatibility | **OpenAI-compatible** — "any client program that works with OpenAI … can be reconfigured to use featherless with little effort" |
| Auth | `Authorization: Bearer <FEATHERLESS_API_KEY>` |
| Models endpoint | `GET /v1/models` |

Model families advertised: **GLM 5.2, DeepSeek 4, Qwen 3.6, Gemma 4**, plus coding, reasoning, vision
and embedding variants. The quickstart uses `Qwen/Qwen2.5-7B-Instruct`; the hackathon guide
recommends **`zai-org/GLM-5.2`** for coding-agent use. 21,905 models were listed at verification.

```python
from openai import OpenAI

client = OpenAI(
    base_url="https://api.featherless.ai/v1",
    api_key=FEATHERLESS_API_KEY,
)

response = client.chat.completions.create(
    model="Qwen/Qwen2.5-7B-Instruct",
    messages=[
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Hello!"},
    ],
)
print(response.choices[0].message.content)
```

The docs reference **"Concurrent Unit Limits"** and an `/account/concurrency/stream` endpoint but do
not publish numeric limits. Assume low concurrency and design the agent to make **few, larger** LLM
calls rather than many small parallel ones.

### Why integrate it

> "To be eligible for partner prizes, the relevant partner technology must be integrated into a
> project submitted under the hackathon challenge."

- **1st place carries $300 in Featherless credits** on top of the $2,500.
- Being the sole named technology partner means Featherless integration is visibly aligned with what
  the organisers want to see.
- It is a drop-in `base_url` swap on an OpenAI client. The integration cost is minutes.

**A good use that isn't tokenism:** run a *cheap open model as a second opinion* against your primary
model — e.g. Qwen or DeepSeek as an adversarial checker whose disagreement forces a NO-TRADE. That
uses the partner tech for something structurally meaningful rather than bolting it on, and it fits
the deterministic-gate architecture the field has converged on.

### ✅ Redemption — resolved
Full guide converted to markdown at `Hackathon-Setup-Guide-ALPACA26.md` in this directory.

1. Open **<https://featherless.ai/join/feather_request_pricing/ALPACA26>** — the promo code
   `ALPACA26` is attached to the link and applied automatically at checkout. If the page errors or
   the code is not applied, **reload once**.
2. Redeeming adds **$25 of request credits**, with **no model size limit** and context up to
   **256K**.
3. Plan then shows *Active* with a $25.00 balance. Every billed request draws down from it.
4. Profile → **API Keys** → create a key.

The Subscription page itemises **every** billed request — model, input/output tokens, exact cost.
Useful for a live cost-per-decision figure in your write-up.

The plan also includes **one secure agent sandbox** (Agents → Marketplace: Open WebUI, SillyTavern,
or a coding agent, pre-wired to Featherless).

### ✅ Credentials verified live (2026-09-01)

| Check | Result |
|---|---|
| `GET /v1/models` | HTTP 200 — **21,905 models** |
| `POST /v1/chat/completions`, `zai-org/GLM-5.2` | correct response |
| Usage block | `{prompt_tokens, completion_tokens, cached_tokens}` |

Two notes:
- **Key format is `rc_…`, not the `fw-…` shown in the guide.** The guide is stale on that detail.
- `cached_tokens` in the usage payload implies **prompt caching**. If your agent prompts share a long
  stable prefix (instructions, schema, universe), structure them so the prefix is reused — it
  stretches the $25 considerably.

### Error codes worth handling
| Code | Meaning |
|---|---|
| **401** | key not recognised |
| **403** | **model is gated** — open the model page and click *Unlock Model* to accept the licence |
| **500** | unsupported parameter in the request |
| **503** | cold model or at capacity — **retry; escalate only after three attempts** |

`503` on a cold model is the one that matters for an autonomous agent: build retry-with-backoff
around it or a scheduled cycle will die on a cold start.

## Other technologies observed in the field

Not hackathon partners — these are simply what competing teams tagged on their submissions. Useful as
a signal of what is normal and what would stand out.

| Technology | Rough frequency across the 40 submissions |
|---|---|
| **Alpaca** | Nearly all (as expected) |
| **Anthropic Claude / Claude Code** | Very common — the single most-used AI stack |
| **Featherless** | Common — teams are claiming the partner credits |
| **AI/ML API** | Common |
| **Gemini** (2.5/3 Flash, 3 Pro, AI Studio, Antigravity) | Common |
| **Groq** | Several |
| OpenAI / ChatGPT / Codex | Several |
| Streamlit, Vercel, Next.js | Several (dashboards) |
| AgentOps | A few (observability) |
| LangGraph | A few |
| ElevenLabs, AWS, Cursor, GitHub Copilot | Occasional |

Two readings:
1. **Claude + Alpaca MCP is the default stack.** Using it is safe but not differentiating.
2. **Observability is under-used.** Only a couple of teams tagged AgentOps. For a project whose whole
   pitch is auditability, instrumentation is a cheap way to make the claim concrete.

---

### What this page implies for design

1. **Claim the Featherless credits early** — first-come, first-served, and $25 is not a large pool.
2. **Integrate Featherless for a real role**, ideally an adversarial second-opinion model, not a
   decorative call.
3. Design for **low concurrency** — batch reasoning into few calls.
4. Read page 2 of the setup PDF for redemption; it could not be extracted here.
