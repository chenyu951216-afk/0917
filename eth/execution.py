"""Closed-bar signal lifecycle; each leg has independent risk. No exchange orders."""
from copy import deepcopy


def entry_confirmation(entry, side, bars, created_at, last_checked=0):
    sign = 1 if side == 'long' else -1
    known = max(created_at, entry.get('confirmed_at', 0))
    for i in range(1, len(bars)):
        prev, b = bars[i-1], bars[i]
        if b['close_time'] <= max(known, last_checked):
            continue
        # A touch may precede the rejection by up to two closed 5m bars.
        recent = [x for x in bars[max(0, i-2):i+1] if x['close_time'] > known]
        touch = any(x['l'] <= entry['high'] and x['h'] >= entry['low'] for x in recent)
        recovery = b['c'] >= entry['price'] if sign == 1 else b['c'] <= entry['price']
        rejection = sign*(b['c']-b['o']) > 0 and sign*(b['c']-prev['c']) > 0
        intact = all(x['l'] > entry['stop'] if sign == 1 else x['h'] < entry['stop'] for x in recent)
        if touch and recovery and rejection and intact:
            if 'max_chase' in entry and sign*(b['c']-entry['price']) > entry['max_chase']:
                continue
            return {'close_time': b['close_time'], 'price': b['c'], 'bar_low': b['l'], 'bar_high': b['h']}
    return None


def _permitted(snapshot, side, stage):
    if not snapshot.get('entry_checks_ok', True):
        return False
    flags = snapshot.get('entry_checks_by_side')
    if flags is not None and not flags.get(side, False):
        return False
    diag = snapshot.get('gate_diagnostics', {}).get(side)
    return diag is None or stage in diag.get('allowed_stages', diag.get('enabled_stages', []))


def transition(previous, active, snapshot, bars5, now, main, event):
    from .strategy import VERSION, history_error
    prev, active, events = previous or {}, deepcopy(active), []
    valid_bars = not history_error(bars5, 300, now, 30)
    bar = bars5[-1]['close_time'] if valid_bars else snapshot.get('closed_at', {}).get('5m', 0)
    bias, last_bias = snapshot.get('bias'), prev.get('last_direction')
    if bias in {'long', 'short'} and last_bias in {'long', 'short'} and bias != last_bias:
        events.append(event(snapshot, 'bias_change', f"bias:{last_bias}:{bias}:{bar}", plan=active,
                            reason='綜合偏向轉變；不等於已成交反手。每筆原止損不移遠。', now=now))
    for name, state in snapshot.get('structure_states', {}).items():
        older = prev.get('structure_states', {}).get(name, {})
        if state.get('state') == 'broken' and state.get('break_id') != older.get('break_id'):
            events.append(event(snapshot, 'structure_break', 'break:'+state['break_id'], plan=active,
                                reason=name+'舊趨勢已由兩根收K確認破壞；可能轉折或震盪，解除舊方向否決但不直接追價。', now=now))
    if active and active.get('structure_version') != VERSION:
        events.append(event(snapshot, 'recalibration', f"recalibrate:{active['id']}:{VERSION}", plan=active,
                            reason='波段／路由模型更新；舊未成交計畫撤銷重算。實際持倉原保護止損不移遠。', now=now))
        active = None
    if active and now >= active['expires_at']:
        events.append(event(snapshot, 'expired', f"expired:{active['id']}", plan=active,
                            reason='計畫到期，不補發過期入場；已有實倉請依各筆原止損管理。', now=now))
        active = None
    if active and valid_bars:
        sign = 1 if active['side'] == 'long' else -1
        opposite = 'short' if active['side'] == 'long' else 'long'
        other = snapshot.get('plans', {}).get(opposite, {})
        opposite_trigger = None
        if other.get('setup_state') == 'armed':
            for e in other.get('entries', []):
                if e.get('enabled') and e.get('route_enabled', True) and _permitted(snapshot, opposite, e['stage']):
                    # Newly computed opposite levels must have existed before this K.
                    opposite_trigger = entry_confirmation(e, opposite, bars5, max(active['created_at'], e.get('confirmed_at', 0)), bar-300)
                    if opposite_trigger:
                        break
        if opposite_trigger:
            events.append(event(snapshot, 'reversal', f"reverse:{active['id']}:{bar}", plan=active,
                                reason='反方向波段計畫與已收5分鐘K拒絕均確認；原方向撤銷，未代平倉或反手。', now=now))
            active = None
        else:
            macro = snapshot.get('structure_states', {}).get('4h', {})
            for e in active['entries']:
                if not e.get('enabled') or e.get('signal_state') in {'cancelled', 'missed'}:
                    continue
                fresh = [b for b in bars5 if b['close_time'] > max(active['created_at'], e.get('last_checked', 0))]
                broken = next((b for b in fresh if sign*(b['c']-e['stop']) <= 0), None)
                if broken:
                    e['signal_state'] = 'cancelled'
                    events.append(event(snapshot, 'leg_stop', f"stop:{active['id']}:{e['stage']}", plan=active,
                                        reason=f"第{e['stage']}筆已收K越過本筆SL {e['stop']:.2f}；撤銷本筆，另一筆不自動攤平。", now=now, stage=e['stage']))
                elif e.get('signal_state') == 'waiting':
                    adverse_break = macro.get('state') == 'broken' and macro.get('break_direction') == opposite
                    if adverse_break and active.get('setup_mode') == 'trend_retest':
                        e['signal_state'] = 'cancelled'
                        events.append(event(snapshot, 'opposition', f"cancel-break:{active['id']}:{e['stage']}:{macro['break_id']}", plan=active,
                                            reason=f"舊主要趨勢已失效；撤銷第{e['stage']}筆尚未觸發計畫，不再沿用舊方向。", now=now, stage=e['stage']))
                    elif _permitted(snapshot, active['side'], e['stage']) and e.get('route_enabled', True):
                        confirmed = entry_confirmation(e, active['side'], bars5, active['created_at'], e.get('last_checked', 0))
                        if confirmed:
                            if confirmed['close_time'] < bar:
                                e['signal_state'] = 'missed'
                            else:
                                e['signal_state'], e['trigger'] = 'entry_confirmed', confirmed
                                events.append(event(snapshot, 'entry_confirmed', f"entry:{active['id']}:{e['stage']}:{bar}", plan=active,
                                                    reason=f"第{e['stage']}筆觸區後已收K拒絕確認；確認價 {confirmed['price']:.2f}。訊號不是成交回報，禁止超範圍追價。", now=now, stage=e['stage']))
                e['last_checked'] = bar
            if all(not e.get('enabled') or not e.get('route_enabled', True) or e.get('signal_state') in {'cancelled', 'missed'} for e in active['entries']):
                active = None
    side = snapshot.get('trade_side')
    if main and snapshot.get('eligible') and not active and side in {'long', 'short'}:
        plan, source = snapshot['plans'][side], snapshot['closed_at']['30m']
        ident = f"ETH:{VERSION}:{side}:{source}"
        if ident != prev.get('last_plan_id'):
            active = deepcopy(plan)
            active.update(id=ident, created_at=now, published_bar=source, engine='r4')
            events.append(event(snapshot, 'signal', 'signal:'+ident, plan=active,
                                reason='條件計畫成立；等待各筆觸區與收K確認，尚非立即市價單。', now=now))
    snapshot['last_direction'] = bias if bias in {'long', 'short'} else last_bias
    snapshot['last_plan_id'] = active['id'] if active else prev.get('last_plan_id')
    return active, events
