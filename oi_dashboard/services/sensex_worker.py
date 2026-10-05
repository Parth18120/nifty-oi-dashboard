"""Background worker that refreshes Sensex chain analytics."""

import threading

from oi_dashboard.analytics.realized_vol import get_best_sigma
from oi_dashboard.core import state


def sensex_refresh_loop():
    import time as _t

    from oi_dashboard.analytics import sensex_analysis
    from oi_dashboard.feeds import sensex_feed
    while True:
        try:
            raw = sensex_feed.get_sensex_data()
            if raw:
                nifty_sigma = get_best_sigma()
                enriched = sensex_analysis.compute_sensex_overpricing(
                    raw, nifty_sigma_fallback=nifty_sigma
                )
                top = sensex_analysis.get_top_overpriced(enriched, top_n=6)
                stored_chain = enriched['rows']
                print(f'[SENSEX CHAIN STORED] chain_rows in state: {len(stored_chain)}')
                with state.sensex_lock:
                    state.sensex_state['data'] = {
                        'spot':         raw['spot'],
                        'expiry':       raw['expiry'],
                        'dte':          raw['dte'],
                        'sigma_used':   enriched['sigma_used'],    # real %, e.g. 13.9
                        'sigma_source': enriched['sigma_source'],
                        'ce_top':       top['ce_top'],
                        'pe_top':       top['pe_top'],
                        'chain_rows':   stored_chain,              # full chain for toggle table
                    }
                    state.sensex_state['last_update'] = _t.time()
                    state.sensex_state['error'] = None
            else:
                with state.sensex_lock:
                    state.sensex_state['error'] = 'fetch returned None'
        except Exception as _e:
            with state.sensex_lock:
                state.sensex_state['error'] = str(_e)
            print(f'[SENSEX BG] error: {_e}')
        _t.sleep(15)


def start_sensex_thread():
    t = threading.Thread(target=sensex_refresh_loop, daemon=True, name="sensex-bg")
    t.start()
    print("[SENSEX] background thread started (15s refresh)")
