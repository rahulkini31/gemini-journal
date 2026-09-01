#!/usr/bin/env bash
# Resolves open questions Q2, Q3, Q6 from knowledge-base/09-open-questions.md.
#
# SAFETY: every order uses an unfillable price and is cancelled immediately.
# The debit spread below is worth ~$2.80; we bid $0.01. It cannot fill.
# Run from the repo root:  bash scripts/verify-mleg.sh
set -uo pipefail
cd "$(dirname "$0")/.."
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
set -a && . ./.env && set +a
export ALPACA_API_KEY ALPACA_SECRET_KEY

NEAR=SPY260918C00765000   # refresh these to live contracts before running
FAR=SPY260918C00770000
PUT_S=SPY260918P00755000
PUT_L=SPY260918P00750000

cleanup () { echo; echo "--- cancelling all open orders ---"; alpaca order cancel-all 2>&1 | head -5; alpaca order list --jq 'length'; }
trap cleanup EXIT

show () {
  python3 -c "
import sys,json
raw=sys.stdin.read()
try: d=json.loads(raw)
except Exception: print('  RAW:',raw[:400]); sys.exit()
if isinstance(d,dict) and (d.get('message') or d.get('code')):
    print('  REJECTED:',json.dumps({k:d.get(k) for k in ('code','message') if d.get(k) is not None}))
else:
    print('  ACCEPTED id=',d.get('id'),'status=',d.get('status'),'class=',d.get('order_class'),'type=',d.get('type'))
    for l in (d.get('legs') or []):
        print('    leg',l.get('symbol'),l.get('side'),'ratio=',l.get('ratio_qty'),'intent=',l.get('position_intent'))
"
}

echo "===== Q2: MLEG limit via CLI raw passthrough ====="
printf '%s' "{\"order_class\":\"mleg\",\"qty\":\"1\",\"type\":\"limit\",\"limit_price\":\"0.01\",\"time_in_force\":\"day\",
 \"legs\":[{\"symbol\":\"$NEAR\",\"ratio_qty\":\"1\",\"side\":\"buy\",\"position_intent\":\"buy_to_open\"},
          {\"symbol\":\"$FAR\",\"ratio_qty\":\"1\",\"side\":\"sell\",\"position_intent\":\"sell_to_open\"}]}" \
  | alpaca api POST /v2/orders | show

echo "===== Q3: MLEG type=market ====="
printf '%s' "{\"order_class\":\"mleg\",\"qty\":\"1\",\"type\":\"market\",\"time_in_force\":\"day\",
 \"legs\":[{\"symbol\":\"$NEAR\",\"ratio_qty\":\"1\",\"side\":\"buy\",\"position_intent\":\"buy_to_open\"},
          {\"symbol\":\"$FAR\",\"ratio_qty\":\"1\",\"side\":\"sell\",\"position_intent\":\"sell_to_open\"}]}" \
  | alpaca api POST /v2/orders | show

echo "===== Q6: four-leg iron condor, ratio 1:1:1:1 ====="
printf '%s' "{\"order_class\":\"mleg\",\"qty\":\"1\",\"type\":\"limit\",\"limit_price\":\"0.01\",\"time_in_force\":\"day\",
 \"legs\":[{\"symbol\":\"$FAR\",\"ratio_qty\":\"1\",\"side\":\"sell\",\"position_intent\":\"sell_to_open\"},
          {\"symbol\":\"$NEAR\",\"ratio_qty\":\"1\",\"side\":\"buy\",\"position_intent\":\"buy_to_open\"},
          {\"symbol\":\"$PUT_S\",\"ratio_qty\":\"1\",\"side\":\"sell\",\"position_intent\":\"sell_to_open\"},
          {\"symbol\":\"$PUT_L\",\"ratio_qty\":\"1\",\"side\":\"buy\",\"position_intent\":\"buy_to_open\"}]}" \
  | alpaca api POST /v2/orders | show
