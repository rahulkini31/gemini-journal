F O R H A C K A T H O N B U I L D E R S

## Hackathon Setup Guide

Build with 40,000+ open-source AI models. Serverless, OpenAI-compatible, no GPUs to manage.

ALPACA26

PROMO CODE

\$25 CREDITS INCLUDED

OPENAI-COMPATIBLE


## Sign up for Feather Per-Request

- Scan the QR code or open the signup link, your promo code is already attached to it.

- The code ALPACA26 is applied automatically at checkout.

- Redeeming the code adds \$25 of request credits to your account - no model size limit and context up to 256K. If the page shows an error or the code isn't applied, reload it once.

[Redeem ALPACA26 →](https://featherless.ai/join/feather_request_pricing/ALPACA26)

After checkout the plan shows Active with a \$25.00 credit balance. Every billed request draws from it.

*S C A N H E R E*


## Create your API key

- After subscribing, click your profile in the top right and open API Keys.

- Create a new key and copy it somewhere safe, you'll need it for every request.

- Treat it like a password: keep it in an environment variable, never commit it to your repo.

\# every request carries it as a bearer token

export FEATHERLESS_API_KEY=fw-...

Authorization: Bearer \$FEATHERLESS_API_KEY

*PROFILE → API KEYS*


## Plug it into your coding agent

- Use your credits from inside Claude Code or opencode - point the agent at Featherless and pick a strong coding model like GLM 5.2.

- Claude Code: connect Featherless as an LLM gateway with your API key - guide below.

- opencode: run /connect, choose Other, set the base URL and paste your API key.

\# two values cover every agent

base URL https://api.featherless.ai/v1

model

[Claude Code gateway docs →](https://code.claude.com/docs/en/llm-gateway)

[opencode provider docs →](https://opencode.ai/docs/providers/)

[featherless.ai/models](https://featherless.ai/models)

zai-org/GLM-5.2


## Every request is itemized

Your Subscription page lists every billed request - the model, input and output tokens, and the exact cost - plus your live balance and spend for the period.

| APIrequests |   |
| --- | --- |
|   | $0.000008 |
| Owen/Qwen3-Embedding-0.68 |   |
| Owen/Qwen3-Embedding-0.68 |   |
| Owen/Qwen3-Embedding-0.68 days ago 8 |   |
| Owen/Qwen3-Embedding-0.68 8 days ago |   |
| Owen/Qwen3-Embedding-0. |   |
| Owen/Qwen3-Embedding-0.68 | 258 tok |


## Spin up an agent in one click

Your plan includes one secure agent sandbox. Open Agents > Marketplace and launch Open WebUI, SillyTavern or a coding agent - all pre-wired to Featherless inference, no setup needed.

[Video walkthroughs: youtube.com/@Isaac-featherless →](https://www.youtube.com/@Isaac-featherless)


## Troubleshooting in 10 seconds

## 401 UNAUTHENTICATED

API key not recognized. Check you copied it correctly, or create a new one in account settings.

500 INTERNAL SERVER ERROR

Request could not be processed. Check for unsupported parameters in the API documentation.

403 UNAUTHORIZED

Model is gated. Open the model's page, click “Unlock Model” and accept the license terms.

503 SERVICE UNAVAILABLE

Cold model or full capacity. Retry the request; if it persists after three attempts, ping us on Discord.


## Ready to build

01

02

03

04

## Test your setup

Create your API key and run one of the code examples

QUICK LINKS

[Redeem ALPACA26 →](https://featherless.ai/join/feather_request_pricing/ALPACA26)

## Explore models

Find the right model for your use case in the catalog

[featherless.ai/models](https://featherless.ai/models)

## Read the docs

Application guides cover common

stacks and patterns

[featherless.ai/docs](https://featherless.ai/docs)

## Start building

Ship something great, we can't wait to see it

Good luck, and have fun. We can't wait to see what you build.
