"""Causal swing structure on BOTH timeframes; live quotes only project visibility.

A swing is known at confirmed_at, never at pivot_at. Unfinished swings stay
provisional. Internal two-bar reactions are obstacles, not entry structures.
"""
from statistics import median

PROFILES = {
    '30m': {'multiplier': 1.3, 'min_right': 3, 'min_leg_bars': 6, 'requested_bars': 720},
    '4h': {'multiplier': 1.5, 'min_right': 2, 'min_leg_bars': 3, 'requested_bars': 540},
}


def role_after_break(bars, zone):
    role, pending, count, breakout_at = zone['origin_role'], None, 0, None
    status = 'confirmed_pivot'
    for b in bars:
        if b['close_time'] <= zone['confirmed_at']:
            continue
        above = b['c'] > zone.get('structural_high', zone['high'])
        below = b['c'] < zone.get('structural_low', zone['low'])
        if pending:
            beyond = above if pending == 'support' else below
            rejected = below if pending == 'support' else above
            if rejected:
                pending, count, breakout_at, status = None, 0, None, 'failed_break'
            elif beyond:
                touched = b['l'] <= zone['high'] if pending == 'support' else b['h'] >= zone['low']
                if count >= 2 and touched:
                    role, pending, count, status = pending, None, 0, 'retested_flip'
                else:
                    count += 1
            continue
        if (role == 'resistance' and above) or (role == 'support' and below):
            pending = 'support' if role == 'resistance' else 'resistance'
            count, breakout_at, status = 1, b['close_time'], 'awaiting_retest'
    return {**zone, 'role': role, 'pending_role': pending,
            'status': 'awaiting_retest' if pending else status, 'breakout_at': breakout_at}


def external_swings(bars, multiplier=1.5, min_right=2, min_leg_bars=0):
    """Online directional change, with volatility frozen at each extreme.

    min_leg_bars is the minimum elapsed context since the previous origin. A
    fast impulse must not freeze the swing state forever merely because its
    extreme appeared early. Confirmation still needs min_right later bars.
    """
    from .strategy import atr
    if len(bars) < 18:
        return [], None
    swings, mode, low_i, high_i = [], None, 14, 14
    low_atr = high_atr = max(atr(bars[:15]), 1e-9)
    for j in range(15, len(bars)):
        b = bars[j]
        local_atr = max(atr(bars[max(0, j-20):j+1]), 1e-9)
        if mode != 'up' and b['l'] < bars[low_i]['l']:
            low_i, low_atr = j, local_atr
        if mode != 'down' and b['h'] > bars[high_i]['h']:
            high_i, high_atr = j, local_atr
        last_index = swings[-1]['index'] if swings else 0
        low_reversal = (mode != 'up' and j-low_i >= min_right and
                        j-last_index >= min_leg_bars and b['c']-bars[low_i]['l'] >= multiplier*low_atr)
        high_reversal = (mode != 'down' and j-high_i >= min_right and
                         j-last_index >= min_leg_bars and bars[high_i]['h']-b['c'] >= multiplier*high_atr)
        if low_reversal and high_reversal:
            low_reversal, high_reversal = low_i > high_i, high_i > low_i
        if not (low_reversal or high_reversal):
            continue
        i, role, a = (low_i, 'support', low_atr) if low_reversal else (high_i, 'resistance', high_atr)
        extreme = bars[i]['l' if low_reversal else 'h']
        prior = next((p for p in reversed(swings) if p['origin_role'] != role), None)
        bos = bool(prior and (b['c'] > prior['price'] if low_reversal else b['c'] < prior['price']))
        swings.append({'index': i, 'price': extreme, 'extreme_price': extreme, 'origin_role': role,
                       'pivot_at': bars[i]['close_time'], 'confirmed_at': b['close_time'],
                       'confirm_index': j, 'atr_at_origin': a,
                       'departure_atr': abs(b['c']-extreme)/a, 'break_of_structure': bos,
                       'confirmation_right_bars': j-i, 'leg_span_bars': i-last_index, 'context_bars': j-last_index,
                       'tier': 'external'})
        if low_reversal:
            mode = 'up'
            high_i = max(range(i+1, j+1), key=lambda k: bars[k]['h'])
            high_atr = max(atr(bars[max(0, high_i-20):high_i+1]), 1e-9)
        else:
            mode = 'down'
            low_i = min(range(i+1, j+1), key=lambda k: bars[k]['l'])
            low_atr = max(atr(bars[max(0, low_i-20):low_i+1]), 1e-9)
    idx = high_i if mode == 'up' else low_i
    return swings, {'origin_role': 'resistance' if mode == 'up' else 'support',
                    'pivot_at': bars[idx]['close_time'], 'price': bars[idx]['h' if mode == 'up' else 'l'],
                    'confirmed': False, 'reason': '波段尚未完成反向位移／持續時間確認，不提前當成支撐壓力'}


def local_swings(bars):
    """Retain small internal reactions as TP obstacles, not main-zone fallbacks."""
    from .strategy import atr
    out = []
    for i in range(14, len(bars)-2):
        b, left, right = bars[i], bars[i-2:i], bars[i+1:i+3]
        a = max(atr(bars[max(0, i-20):i+1]), 1e-9)
        for role, key in (('support', 'l'), ('resistance', 'h')):
            extreme = (all(b[key] < x[key] for x in left) and all(b[key] <= x[key] for x in right)) if role == 'support' else (all(b[key] > x[key] for x in left) and all(b[key] >= x[key] for x in right))
            departure = max(x['c']-b['l'] for x in right) if role == 'support' else max(b['h']-x['c'] for x in right)
            if extreme and departure >= .65*a:
                out.append({'index': i, 'price': b[key], 'extreme_price': b[key], 'origin_role': role,
                            'pivot_at': b['close_time'], 'confirmed_at': right[-1]['close_time'],
                            'confirm_index': i+2, 'atr_at_origin': a, 'departure_atr': departure/a,
                            'break_of_structure': False, 'confirmation_right_bars': 2, 'tier': 'internal'})
    return out


def make_zone(bars, pivot, frame, tick):
    from .strategy import round_tick
    i, j, role = pivot['index'], pivot['confirm_index'], pivot['origin_role']
    b, a = bars[i], pivot['atr_at_origin']
    base = [b]
    for k in range(i+1, min(j, i+3)+1):
        x = bars[k]
        near = x['l'] <= b['l']+.3*a if role == 'support' else x['h'] >= b['h']-.3*a
        if not near:
            break
        base.append(x)
    pad = max(2*tick, .04*a)
    if role == 'support':
        distal = min(x['l'] for x in base)
        proximal = min(min(x['o'], x['c']) for x in base)
        structural_low, structural_high = distal-pad, max(proximal, distal)+pad
    else:
        distal = max(x['h'] for x in base)
        proximal = max(max(x['o'], x['c']) for x in base)
        structural_low, structural_high = min(proximal, distal)-pad, distal+pad
    cap = max(6*tick, min(a*(.75 if frame == '4h' else .60), pivot['price']*(.006 if frame == '4h' else .0035)))
    low, high = structural_low, structural_high
    refined = high-low > cap
    if refined:
        width = max(4*tick, min(cap-2*tick, .30*a))
        low, high = (distal-pad, distal-pad+width) if role == 'support' else (distal+pad-width, distal+pad)
    low, high = round_tick(low, tick, -1), round_tick(high, tick, 1)
    z = {**pivot, 'id': f"{frame}:{pivot['tier']}:{role}:{pivot['pivot_at']}",
         'price': round_tick((low+high)/2, tick), 'low': low, 'high': high, 'frame': frame,
         'structural_low': round_tick(structural_low, tick, -1),
         'structural_high': round_tick(structural_high, tick, 1),
         'invalidation_boundary': round_tick(distal-pad if role == 'support' else distal+pad, tick, -1 if role == 'support' else 1),
         'width_cap': cap+2*tick, 'base_bars': len(base), 'refined_core': refined,
         'touches': 1, 'prominence': pivot['departure_atr']*a,
         'label': frame+'持續波段底／頂' if pivot['tier'] == 'external' else '內部反應／途中障礙'}
    z = role_after_break(bars, z)
    last_test = j
    for k in range(j+1, len(bars)):
        x = bars[k]
        if x['l'] <= high and x['h'] >= low and k-last_test >= 2:
            z['touches'] += 1
            last_test = k
    z['strength'] = round(min(95, 35+min(30, pivot['departure_atr']*10)+min(15, (z['touches']-1)*3)+(10 if pivot['break_of_structure'] else 0)))
    z['selection_reason'] = ('持續波段起點' if pivot['tier'] == 'external' else '內部反應') + f"；離開 {pivot['departure_atr']:.2f} ATR；{pivot['confirmation_right_bars']} 根後確認"
    return z


def project_frame(frame, reference):
    out = dict(frame)
    zones = frame.get('zones')
    if zones is None:
        zones = [{**z, 'role': role} for role in ('support', 'resistance') for z in frame.get(role, [])]
    if reference is None or reference <= 0:
        return {**out, 'support': [], 'resistance': [], 'display': {}, 'testing': [], 'reference_price': None,
                'notes': ['缺少有效報價，不將舊價位列為現價支撐壓力']}
    visible, testing, notes = {'support': [], 'resistance': []}, [], []
    for z in zones:
        if z.get('pending_role'):
            continue
        if z['low'] <= reference <= z['high']:
            testing.append(z)
        elif z['role'] == 'support' and z['high'] < reference:
            visible['support'].append(z)
        elif z['role'] == 'resistance' and z['low'] > reference:
            visible['resistance'].append(z)
    display = {}
    hierarchical = frame.get('frame') in PROFILES
    for role, items in visible.items():
        nearest = sorted(items, key=lambda z: z['price'], reverse=role == 'support')
        if hierarchical:
            candidates = [z for z in nearest if z.get('tier') == 'external']
            # Most recent sustained origin, not the nearest incidental tiny dip.
            selected = max(candidates, key=lambda z: (z.get('pivot_at', 0), z.get('strength', 0)), default=None)
        else:  # old snapshots/tests have no tier metadata
            selected = nearest[0] if nearest else None
        display[role] = selected
        out[role] = ([selected]+[z for z in nearest if z is not selected]) if selected else nearest
    if testing:
        notes.append('現價正在測試結構；正在測試的區域另列，不冒稱上方壓力或下方支撐')
    if hierarchical and any(not display[r] for r in display):
        notes.append('未確認波段不填入主結構；內部小轉折只保留為途中障礙')
    out.update(zones=zones, display=display, testing=testing, reference_price=reference, notes=notes,
               selection_policy='30m／4h各自取持續波段；保留內部障礙，不以最近單根高低點代替')
    return out


def project_levels(levels, reference):
    result = {f: project_frame(levels.get(f, {}), reference) for f in ('30m', '4h')}
    for role in ('support', 'resistance'):
        a, b = (result[f]['display'].get(role) for f in ('30m', '4h'))
        if a and b and min(a['high'], b['high']) >= max(a['low'], b['low']):
            for f in result:
                result[f]['notes'].insert(0, '30分鐘與4小時'+('支撐' if role == 'support' else '壓力')+'共振；不人為拉開')
    return result


def structure_levels(bars, frame, price, tick):
    from .strategy import atr
    if not bars:
        return project_frame({'zones': [], 'frame': frame, 'atr': 0, 'last_close': None}, price)
    profile = PROFILES.get(frame, {'multiplier': 1.3, 'min_right': 2, 'min_leg_bars': 0, 'requested_bars': len(bars)})
    external, pending = external_swings(bars, profile['multiplier'], profile['min_right'], profile['min_leg_bars'])
    # Keep the earlier internal observation even after a later major confirmation.
    # It remains an obstacle, never a promoted/backdated entry structure.
    pivots = external+local_swings(bars)
    zones = [make_zone(bars, p, frame, tick) for p in pivots]
    return project_frame({'zones': zones, 'frame': frame, 'atr': atr(bars), 'last_close': bars[-1]['close_time'],
                          'max_zone_width': max((z['width_cap'] for z in zones), default=0),
                          'provisional': pending, 'external_swing_count': len(external),
                          'history': {'actual_bars': len(bars), 'requested_bars': profile['requested_bars'],
                                      'from': bars[0]['t'], 'to': bars[-1]['close_time']},
                          'profile': profile, 'structure_model': 'sustained-swing-v4'}, price)
