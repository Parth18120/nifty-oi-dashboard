"""
Run once to diagnose the Sensex chain: how many strikes does Dhan return?
python diag_sensex_chain.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

import math
from datetime import datetime, timedelta, timezone
from statistics import NormalDist

from config import ACCESS_TOKEN, CLIENT_ID, STRUCTURAL_IV_PREMIUM_PP
from dhanhq import DhanContext, dhanhq

IST = timezone(timedelta(hours=5, minutes=30))
dhan = dhanhq(DhanContext(CLIENT_ID, ACCESS_TOKEN))

print("=== SENSEX CHAIN DIAGNOSTIC ===")
print()

# 1. Expiry list
e = dhan.expiry_list(under_security_id=51, under_exchange_segment='IDX_I')
print(f"Expiry API status: {e.get('status')}")
expiries = e.get('data', {}).get('data', [])
print(f"Expiries returned: {expiries}")
expiry = expiries[0] if expiries else None
if not expiry:
    print("ERROR: no expiry found"); sys.exit(1)

print()

# 2. DTE
now = datetime.now(IST)
exp_dt = datetime.strptime(expiry, '%Y-%m-%d')
display_dte = max((exp_dt.date() - now.date()).days, 0)
exp_close = exp_dt.replace(hour=15, minute=30, second=0, tzinfo=IST)
secs = (exp_close - now).total_seconds()
pricing_dte = max(secs / 86400, 0.001)
T = pricing_dte / 365
print(f"Expiry:       {expiry}")
print(f"display_dte:  {display_dte}")
print(f"pricing_dte:  {pricing_dte:.4f} days = {pricing_dte*24:.2f}h")
print(f"T (BS years): {T:.6f}")
print()

# 3. Chain
import time

time.sleep(3)
c = dhan.option_chain(under_security_id=51, under_exchange_segment='IDX_I', expiry=expiry)
print(f"Chain API status: {c.get('status')}")
inner = c.get('data', {}).get('data', {})
spot = inner.get('last_price', 0)
oc = inner.get('oc', {})

print(f"Spot:         {spot}")
print(f"OC dict size: {len(oc)} strikes")
print()

rows = []
for strike_str, sd in oc.items():
    ce = sd.get('ce', {})
    pe = sd.get('pe', {})
    rows.append({
        'strike': float(strike_str),
        'ce_ltp': ce.get('last_price', 0),
        'pe_ltp': pe.get('last_price', 0),
        'ce_oi':  ce.get('oi', 0),
        'pe_oi':  pe.get('oi', 0),
    })
rows.sort(key=lambda r: r['strike'])

print(f"[SENSEX CHAIN RAW] total strikes from Dhan: {len(rows)}")
if rows:
    print(f"[SENSEX CHAIN RANGE] min={rows[0]['strike']:.0f}, max={rows[-1]['strike']:.0f}")
print()

# 4. BS fair for each, print strikes near spot
sigma = 0.1394
N = NormalDist().cdf
def bs_call(S, K, T, r=0.065, sig=sigma):
    if T <= 0: return 0.0
    d1 = (math.log(S/K) + (r + 0.5*sig**2)*T) / (sig*math.sqrt(T))
    d2 = d1 - sig*math.sqrt(T)
    return S*N(d1) - K*math.exp(-r*T)*N(d2)

print(f"{'Strike':>7}  {'CE LTP':>7}  {'CE Fair':>7}  {'CE OVR%':>8}  {'PE LTP':>7}  {'PE Fair':>7}  {'PE OVR%':>8}  {'CE OI':>8}")
print("-" * 80)
for r in rows:
    K = r['strike']
    if abs(K - spot) > 2500:
        continue
    ce_fair = bs_call(spot, K, T) if K > spot else 0
    pe_fair = bs_call(spot, 2*spot-K, T) if K < spot else 0  # rough put via put-call
    # actual put fair:
    if K < spot:
        d1 = (math.log(spot/K) + (0.065 + 0.5*sigma**2)*T) / (sigma*math.sqrt(T)) if T>0 else 0
        d2 = d1 - sigma*math.sqrt(T) if T>0 else 0
        pe_fair = K*math.exp(-0.065*T)*N(-d2) - spot*N(-d1) if T>0 else 0

    ce_ltp = r['ce_ltp']
    pe_ltp = r['pe_ltp']
    ce_ovr = (ce_ltp - ce_fair) / ce_fair * 100 if ce_fair > 0.5 and K > spot else float('nan')
    pe_ovr = (pe_ltp - pe_fair) / pe_fair * 100 if pe_fair > 0.5 and K < spot else float('nan')

    atm = "<<" if abs(K - spot) < 150 else ""
    print(f"{K:>7.0f}  {ce_ltp:>7.2f}  {ce_fair:>7.2f}  {ce_ovr:>+8.0f}%  {pe_ltp:>7.2f}  {pe_fair:>7.2f}  {pe_ovr:>+8.0f}%  {r['ce_oi']:>8.0f} {atm}")
