"""Independent structure-risk legs. Risk geometry and mode permissions are separate."""
from copy import deepcopy


def main_zone(frame, role):
    display = frame.get('display')
    selected = display.get(role) if display is not None else next(iter(frame.get(role, [])), None)
    testing = [z for z in frame.get('testing', []) if z.get('role') == role and not z.get('pending_role')]
    if frame.get('frame') in {'4h', '30m'}:
        testing = [z for z in testing if z.get('tier') == 'external']
        if selected and selected.get('tier') != 'external':
            selected = None
    return max(testing, key=lambda z: z.get('pivot_at', 0)) if testing else selected


def opposing_targets(levels, side, entry, tick):
    sign = 1 if side == 'long' else -1
    role = 'resistance' if sign == 1 else 'support'
    obstacles = []
    for frame in levels.values():
        zones = frame.get('zones')
        if zones is None:
            zones = [{**z, 'role': role} for z in frame.get(role, [])]
        for z in zones:
            if z.get('role') != role or z.get('pending_role'):
                continue
            p = z['low'] if sign == 1 else z['high']
            if sign*(p-entry) > 2*tick:
                obstacles.append(deepcopy(z))
    obstacles.sort(key=lambda z: sign*(z['low'] if sign == 1 else z['high']))
    targets = []
    for z in obstacles:
        p = z['low'] if sign == 1 else z['high']
        if targets and min(z['high'], targets[-1]['zone']['high']) >= max(z['low'], targets[-1]['zone']['low'])-2*tick:
            old = targets[-1]
            old['frames'] = sorted(set(old['frames']+[z['frame']]))
            old['frame'] = '/'.join(old['frames'])
            old['zone']['low'] = min(old['zone']['low'], z['low'])
            old['zone']['high'] = max(old['zone']['high'], z['high'])
            continue
        targets.append({'price': p, 'frame': z['frame'], 'frames': [z['frame']], 'zone': z})
    targets = targets[:3]
    for t, w in zip(targets, {1: [1.0], 2: [.6, .4], 3: [.5, .3, .2]}.get(len(targets), [])):
        t['quantity_fraction'] = w
    return targets


def plan_for(side, levels, reference, tick, now, cfg=None):
    from .strategy import DEFAULTS, VERSION, round_tick
    cfg = {**DEFAULTS, **(cfg or {})}
    sign = 1 if side == 'long' else -1
    role = 'support' if sign == 1 else 'resistance'
    m, h = levels['30m'], levels['4h']
    first, second = main_zone(m, role), main_zone(h, role)
    result = {'side': side, 'valid': False, 'reasons': [], 'entries': [], 'targets': [],
              'structure_version': VERSION, 'created_at': now, 'reference_price': reference,
              'expires_at': now+cfg['plan_lifetime_hours']*3600,
              'risk_model': 'independent_structure_legs', 'min_net_rr': cfg['min_net_rr'],
              'sizing': '每筆最多預定ETH數量50%；一筆不合格不放大另一筆；各自結構止損，不自動攤平'}
    if second and first:
        deeper = second['high'] < first['low'] if sign == 1 else second['low'] > first['high']
        if not deeper:
            second = None
            result['stage2_note'] = '4h與30m重疊／不更深，第二筆不重複計倉；首筆獨立評估'
    if not first:
        result['stage1_note'] = '沒有已確認30m持續波段結構'
    if not second:
        result.setdefault('stage2_note', '沒有獨立有效4h主要波段；不以小轉折冒充')
    cost = 2*cfg['fee_per_side']+cfg['slippage_roundtrip']+cfg['funding_reserve']
    result['cost_pct'] = 100*cost
    for stage, z, frame in ((1, first, m), (2, second, h)):
        if not z:
            continue
        buffer = max(frame.get('atr', 0)*.12, 3*tick)
        boundary = z.get('structural_low', z['low']) if sign == 1 else z.get('structural_high', z['high'])
        stop = round_tick(boundary-sign*buffer, tick, -sign)
        entry = z['high'] if sign == 1 else z['low']
        targets = opposing_targets(levels, side, entry, tick)
        risk = sign*(entry-stop)+entry*cost
        reward = sum(t['quantity_fraction']*sign*(t['price']-entry) for t in targets)-entry*cost
        rr = reward/risk if risk > 0 else -1
        reasons = []
        if stop <= 0 or sign*(entry-stop) <= 0:
            reasons.append('結構止損順序無效')
        if not targets:
            reasons.append('沒有順向已確認目標，不製造固定倍數價位')
        if targets and sign*(targets[0]['price']-entry) <= entry*cost:
            reasons.append('最近反向結構前的空間不足以支付成本')
        if rr < cfg['min_net_rr']:
            reasons.append(f"本筆成本後淨 R {rr:.2f} < {cfg['min_net_rr']:.2f}")
        result['entries'].append({**deepcopy(z), 'stage': stage, 'price': entry, 'stop': stop,
                                  'stop_buffer': buffer, 'targets': targets, 'net_rr': rr,
                                  'risk_pct': 100*abs(entry-stop)/entry,
                                  'quantity_fraction': .5, 'max_chase': frame.get('atr', 0)*.25,
                                  'enabled': not reasons, 'reasons': reasons, 'signal_state': 'waiting',
                                  'trigger_rule': '觸及持續波段核心區後，以已收5分鐘K拒絕確認；不是碰價即成交'})
    enabled = [e for e in result['entries'] if e['enabled']]
    result['valid'] = bool(enabled)
    if not enabled:
        result['reasons'] = [f"第{e['stage']}筆："+'；'.join(e['reasons']) for e in result['entries']] or ['沒有有效持續波段進場結構']
    if result['entries']:
        fallback = enabled[0] if enabled else result['entries'][0]
        e1 = next((e for e in result['entries'] if e['stage'] == 1), fallback)
        e2 = next((e for e in result['entries'] if e['stage'] == 2), fallback)
        group = enabled or result['entries']
        risk = sum((abs(e['price']-e['stop'])+e['price']*cost)*e['quantity_fraction'] for e in group)
        reward = sum(e['net_rr']*(abs(e['price']-e['stop'])+e['price']*cost)*e['quantity_fraction'] for e in group)
        result.update(stop=e2['stop'], stop_zone=second or first, stop_buffer=e2['stop_buffer'],
                      targets=fallback['targets'], blended_entry=sum(e['price'] for e in group)/len(group),
                      net_rr_first=e1['net_rr'], net_rr_blended=reward/risk if risk > 0 else -1,
                      risk_pct_first=e1['risk_pct'], risk_pct_second=e2['risk_pct'],
                      risk_pct_blended=sum(e['risk_pct'] for e in group)/len(group),
                      close_rule='每筆各自結構失效退出；確認反向則撤銷原方向。只通知，未代下單。')
    return result
