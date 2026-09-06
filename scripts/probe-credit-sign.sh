#!/usr/bin/env bash
# Resolve what `limit_price` MEANS for a net-CREDIT mleg structure.
#
# WHY THIS IS DELICATE: for a credit spread there is no single "safe" positive
# limit. If limit_price means "minimum credit I will accept", then 0.01 fills
# instantly. If it means "maximum debit I will pay", then 9.00 fills instantly.
# Either way one of the probes would open a real position.
#
# SAFETY MODEL: run only while the market is CLOSED, so nothing can fill, and
# cancel every probe immediately. The script REFUSES to run if the market is open.
#
# THE DISCRIMINATOR: max possible credit is bounded by the spread width (5.00).
# A limit of 9.00 is an impossible credit but a perfectly ordinary debit, so how
# the API treats it tells us which one it thinks we mean.
set -uo pipefail
cd "$(dirname "$0")/.."
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
set -a && . ./.env && set +a
export ALPACA_API_KEY ALPACA_SECRET_KEY

SHORT=SPY260918C00770000   # sell the near strike
LONG=SPY260918C00775000    # buy the far strike -> net CREDIT, width 5.00

# ---- hard guard ----------------------------------------------------------
IS_OPEN=$(alpaca clock --jq '.is_open' 2>/dev/null)
if [ "$IS_OPEN" != "false" ]; then
  echo "REFUSING TO RUN: market is open (is_open=$IS_OPEN)."
  echo "These probes are only safe while nothing can fill."
  exit 1
fi
echo "market closed - probes cannot fill"

cleanup () {
  echo; echo "--- cleanup ---"
  alpaca order cancel-all >/dev/null 2>&1
  sleep 1
  echo "  open orders : $(alpaca order list --jq 'length' 2>&1)"
  echo "  positions   : $(alpaca position list --jq 'length' 2>&1)"
  echo "  equity      : $(alpaca account get --jq '.equity' 2>&1)"
}
trap cleanup EXIT

probe () {
  local label="$1" price="$2"
  echo
  echo "----- $label : limit_price=$price -----"
  printf '%s' "{\"order_class\":\"mleg\",\"qty\":\"1\",\"type\":\"limit\",
    \"limit_price\":\"$price\",\"time_in_force\":\"day\",
    \"legs\":[{\"symbol\":\"$SHORT\",\"ratio_qty\":\"1\",\"side\":\"sell\",\"position_intent\":\"sell_to_open\"},
             {\"symbol\":\"$LONG\",\"ratio_qty\":\"1\",\"side\":\"buy\",\"position_intent\":\"buy_to_open\"}]}" \
  | alpaca api POST /v2/orders 2>&1 | python3 -c "
import sys,json
raw=sys.stdin.read()
try: d=json.loads(raw)
except Exception: print('  RAW:',raw[:300]); sys.exit()
if d.get('error') or (d.get('code') and not d.get('id')):
    print('  REJECTED:',json.dumps({k:d[k] for k in ('code','error') if k in d}))
else:
    print('  ACCEPTED  status=',d.get('status'),' limit_price=',repr(d.get('limit_price')))
    print('            order_class=',d.get('order_class'),' type=',d.get('type'))
"
  # cancel straight away; never leave a credit probe queued into the open
  alpaca order cancel-all >/dev/null 2>&1
}

probe "A plausible credit (2.00, real credit ~2.16)" "2.00"
probe "B negative (-2.00)"                            "-2.00"
probe "C above max possible credit (9.00 > width 5)"  "9.00"
probe "D near zero (0.01)"                            "0.01"
