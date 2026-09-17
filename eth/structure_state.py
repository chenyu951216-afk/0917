"""Separate historical swing direction, intact/invalidated trend and local trend.

Trendlines use two already-confirmed swing anchors, never live bars or moving
regression endpoints. A break requires two later CLOSED verification bars.
Breaking an old line suspends its veto; it is not proof of a complete reversal.
"""
from math import isfinite

LABELS = {'long': '偏多', 'short': '偏空', 'neutral': '中性／未定向'}


def major_pivots(frame, before=float('inf')):
    unique = {}
    for z in frame.get('zones', []):
        if z.get('tier') != 'external' or z.get('confirmed_at', float('inf')) > before:
            continue
        key = (z.get('origin_role'), z.get('pivot_at'))
        unique[key] = z
    return sorted(unique.values(), key=lambda z: z.get('pivot_at', 0))


def extreme(z):
    return z.get('extreme_price', z['price'])


def structural_bias(frame):
    pivots = major_pivots(frame, frame.get('last_close') or float('inf'))
    values = {r: [p for p in pivots if p.get('origin_role') == r][-2:] for r in ('support', 'resistance')}
    if any(len(v) < 2 for v in values.values()):
        return 'neutral'
    tolerance = .10*max(frame.get('atr', 0), 1e-9)
    changes = [extreme(v[-1])-extreme(v[-2]) for v in values.values()]
    return 'long' if all(d > tolerance for d in changes) else 'short' if all(d < -tolerance for d in changes) else 'neutral'


def _crossing(bars, boundary, sign, known_at, buffer):
    """Record a stable confirmation timestamp; revoke after two closed recrosses."""
    count = failure = 0
    confirmed_at = None
    retested = False
    last_break = None
    previous_time = None
    for b in bars:
        if b['close_time'] <= known_at:
            continue
        if previous_time is not None and b['close_time']-previous_time != 1800:
            count = failure = 0
        previous_time = b['close_time']
        level = boundary(b['close_time'])
        distance = sign*(b['c']-level)
        if distance > buffer:
            count += 1
            failure = 0
            if count == 2 and confirmed_at is None:
                confirmed_at = b['close_time']
                last_break = confirmed_at
            if confirmed_at and b['close_time'] > confirmed_at:
                touched = b['l'] <= level+buffer if sign > 0 else b['h'] >= level-buffer
                retested = retested or touched
        elif distance < -buffer:
            count = 0
            failure += 1
            if failure >= 2:
                confirmed_at, retested = None, False
        else:
            count = 0
            failure = 0
    current = bars[-1] if bars else None
    remains = confirmed_at is not None
    return {'confirmed': remains, 'confirmed_at': confirmed_at if remains else None,
            'candidate': count == 1 and not remains, 'retested': retested if remains else False,
            'last_break_at': last_break, 'failed': bool(last_break and not remains)}


def analyze_structure(frame, verification_bars):
    raw = structural_bias(frame)
    name = frame.get('frame', '4h')
    seconds = 14400 if name == '4h' else 1800
    pivots = major_pivots(frame)
    result = {'frame': name, 'raw_bias': raw, 'effective_bias': raw, 'state': 'intact' if raw != 'neutral' else 'neutral',
              'break_direction': None, 'break_confirmed_at': None, 'break_id': None,
              'lines': [], 'evidence': [], 'verification_frame': '30m',
              'reversal_confirmed': False, 'veto_active': raw != 'neutral'}
    if len(verification_bars) < 2:
        return {**result, 'state': 'insufficient', 'effective_bias': 'neutral', 'veto_active': False,
                'evidence': ['驗證K線不足，不憑缺值宣稱突破']}
    last_time = verification_bars[-1]['close_time']
    pivots = [p for p in pivots if p.get('confirmed_at', float('inf')) <= last_time]
    by_role = {role: [p for p in pivots if p['origin_role'] == role][-2:] for role in ('support', 'resistance')}
    scale = max(frame.get('atr', 0), 1e-9)
    breaks = []
    for role, pair in by_role.items():
        if len(pair) < 2:
            continue
        a, b = pair
        sign = 1 if role == 'resistance' else -1
        span = b['pivot_at']-a['pivot_at']
        delta = extreme(b)-extreme(a)
        # Descending highs or ascending lows only; a single slope is not a trend.
        if span < seconds*3 or sign*delta >= -.10*scale:
            continue
        slope = delta/span
        known = max(a['confirmed_at'], b['confirmed_at'])
        # Do not extrapolate an ancient diagonal indefinitely into current price.
        age = last_time-b['pivot_at']
        horizon = min(30*seconds, max(8*seconds, 3*span))
        if age > horizon:
            result['evidence'].append('舊斜線超過有效投影時窗，不能持續攔截短線')
            continue
        boundary = lambda t, b=b, slope=slope: extreme(b)+slope*(t-b['pivot_at'])
        buffer = max(.12*max(a.get('atr_at_origin', scale), b.get('atr_at_origin', scale)), .0001*extreme(b))
        # A line visibly violated BEFORE it was known is not a valid intact line.
        middle = [x for x in verification_bars if a['pivot_at'] < x['close_time'] <= b['pivot_at']]
        if sum(sign*(x['c']-boundary(x['close_time'])) > buffer for x in middle) > 1:
            continue
        check = _crossing(verification_bars, boundary, sign, known, buffer)
        line = {'kind': 'descending_resistance' if sign > 0 else 'ascending_support',
                'anchors': [{'time': p['pivot_at'], 'price': extreme(p), 'known_at': p['confirmed_at']} for p in pair],
                'known_at': known, 'price_now': boundary(last_time), 'slope': slope,
                'buffer': buffer, 'verification_at': last_time, **check}
        result['lines'].append(line)
        if check['confirmed']:
            breaks.append({'side': 'long' if sign > 0 else 'short', 'at': check['confirmed_at'],
                           'kind': 'trendline', 'anchor': b['pivot_at'], 'level': boundary(last_time), 'retested': check['retested']})
        elif check['candidate']:
            result['evidence'].append('斜線暫時穿越，只有一根確認；不宣稱趨勢反转')
    if raw in {'long', 'short'}:
        role = 'support' if raw == 'long' else 'resistance'
        options = by_role[role]
        if options:
            z = options[-1]
            level = z.get('structural_low', z['low']) if raw == 'long' else z.get('structural_high', z['high'])
            sign = -1 if raw == 'long' else 1
            buffer = max(.10*z.get('atr_at_origin', scale), .0001*level)
            check = _crossing(verification_bars, lambda t: level, sign, z['confirmed_at'], buffer)
            result['protected_level'] = {'price': level, 'known_at': z['confirmed_at'], 'role': role, **check}
            if check['confirmed']:
                breaks.append({'side': 'long' if sign > 0 else 'short', 'at': check['confirmed_at'],
                               'kind': 'protected_swing', 'anchor': z['pivot_at'], 'level': level, 'retested': check['retested']})
    opposite_breaks = [b for b in breaks if raw == 'neutral' or b['side'] != raw]
    if opposite_breaks:
        latest = max(opposite_breaks, key=lambda b: b['at'])
        result.update(effective_bias='neutral', state='broken', break_direction=latest['side'],
                      break_confirmed_at=latest['at'], break_id=f"{name}:{latest['kind']}:{latest['anchor']}:{latest['at']}",
                      veto_active=False, break_evidence=latest,
                      evidence=result['evidence']+['兩根已收30分鐘K確認越過舊斜線／保護波段；解除舊方向否決，可能轉折或震盪，尚非完整反轉'])
    elif raw != 'neutral':
        result['evidence'].append('主要高低點仍同向；沒有已確認的反向結構破壞')
    else:
        result['evidence'].append('主要高低點混合／確認不足；中性不否決合格30分鐘短線趨勢')
    result['summary'] = f"{name}原波段{LABELS[raw]}｜"+({'broken': '舊趨勢結構已破壞', 'intact': '結構仍有效', 'neutral': '中性'}.get(result['state'], result['state']))
    return result


def short_term_trend(bars, frame_state):
    """Sustained 30m swing direction or corroborated multi-hour momentum, not one bar."""
    from .strategy import atr, ema
    if len(bars) < 40:
        return {'direction': 'neutral', 'source': '資料不足', 'evidence': []}
    closes = [b['c'] for b in bars]
    a = atr(bars)
    if a <= 0 or not isfinite(a):
        return {'direction': 'neutral', 'source': '無有效波動', 'evidence': []}
    sep = (ema(closes, 9)-ema(closes, 21))/a
    slope = (ema(closes, 21)-ema(closes[:-6], 21))/a
    path = sum(abs(b-a_) for a_, b in zip(closes[-9:], closes[-8:]))
    efficiency = abs(closes[-1]-closes[-9])/max(path, 1e-9)
    momentum = 'long' if sep > .12 and slope > .12 and closes[-1] > ema(closes, 21) and efficiency >= .30 else 'short' if sep < -.12 and slope < -.12 and closes[-1] < ema(closes, 21) and efficiency >= .30 else 'neutral'
    structural = frame_state.get('effective_bias', 'neutral')
    direction = structural if structural in {'long', 'short'} and (momentum == structural or momentum == 'neutral') else momentum
    return {'direction': direction, 'source': '30m持續波段' if direction == structural and direction != 'neutral' else '30m多小時動能確認',
            'efficiency': round(efficiency, 3), 'ema_separation_atr': round(sep, 3), 'slope_atr': round(slope, 3),
            'evidence': [f"最近4小時方向效率 {efficiency:.2f}；均線斜率 {slope:.2f} ATR",
                         '方向只是模式依據；仍需持續波段進場区與收K觸發']}
