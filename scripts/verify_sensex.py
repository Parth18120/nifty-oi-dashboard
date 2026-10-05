from config import ACCESS_TOKEN, CLIENT_ID
from dhanhq import DhanContext, dhanhq

dhan = dhanhq(DhanContext(CLIENT_ID, ACCESS_TOKEN))
SENSEX_SCRIP_ID = 51   # BSE Sensex

# Test 1: Expiry list
try:
    r = dhan.expiry_list(
        under_security_id=SENSEX_SCRIP_ID,
        under_exchange_segment='IDX_I'
    )
    print(f'Expiries: {r}')
except Exception as e:
    print(f'FAIL Expiry: {e}')
    exit(1)

# Expiry list is nested: r['data']['data']
expiries = r.get('data', {}).get('data', [])
if not expiries:
    print('No expiries returned. Wrong Scrip ID?')
    exit(1)
print(f'First expiry: {expiries[0]}')

# Test 2: Chain fetch
import json

try:
    chain = dhan.option_chain(
        under_security_id=SENSEX_SCRIP_ID,
        under_exchange_segment='IDX_I',
        expiry=expiries[0]
    )
    print(f'Chain type: {type(chain)}')
    print(f'Chain raw: {str(chain)[:800]}')
    if isinstance(chain, dict):
        print(f'Chain status: {chain.get("status")}')
        keys = list(chain.keys())
        print(f'Top-level keys: {keys}')
        data = chain.get('data', {})
        if isinstance(data, dict):
            print(f'data keys: {list(data.keys())}')
except Exception as e:
    print(f'FAIL Chain: {e}')
    import traceback; traceback.print_exc()
    exit(1)

# Test 3: Spot fetch
try:
    spot = dhan.quote_data({'IDX_I': [SENSEX_SCRIP_ID]})
    print(f'Spot: {spot}')
except Exception as e:
    print(f'FAIL Spot: {e}')
