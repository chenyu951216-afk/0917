"""Mode routing without a blanket neutral/transition veto. Never force a trade."""
from .structure_state import structural_bias, LABELS

MODE_LABELS = {'trend_retest': '順主要結構回測', 'short_trend': '中性盤短線趨勢',
               'broken_trend_retest': '舊趨勢失效後短線回測', 'range_edge': '區間邊界拒絕'}


def route_setups(snapshot, bars, cfg, safety_reasons):
    from .strategy import num
    frame = snapshot['levels']['4h']
    detail = snapshot.get('regime_detail', {})
    code = detail.get('code')
    states = snapshot.get('structure_states', {})
    context = states.get('4h', {}).get('effective_bias', structural_bias(frame))
    raw = states.get('4h', {}).get('raw_bias', context)
    broken = states.get('4h', {}).get('state') == 'broken'
    local = snapshot.get('short_term', {}).get('direction', 'neutral')
    last = bars['5m'][-1]['c']
    a = snapshot['levels']['30m'].get('atr', 0)
    composite = snapshot.get('bias', 'neutral')
    candidates, diagnostics = [], {}
    snapshot['entry_checks_by_side'] = {}
    for side, plan in snapshot['plans'].items():
        reasons, mode, score = list(safety_reasons), None, 0
        # Raw geometric validity must not be changed by transient routing decisions.
        geometric = [e for e in plan['entries'] if e.get('enabled', True)]
        allowed_stages = {1, 2}
        has_short_structure = any(e['stage'] == 1 for e in geometric)
        if code == 'shock':
            reasons.append('急變盤暫停新入場，風控與反向預警照常通知')
        elif local == side and (context == 'neutral' or broken or composite == 'neutral'):
            # Only the independently stopped 30m leg, not an unconfirmed HTF reversal.
            mode = 'broken_trend_retest' if broken else 'short_trend'
            score, allowed_stages = (72 if broken else 68), {1}
            if not has_short_structure:
                reasons.append('短線趨勢成立，但尚無通過風險條件的30分鐘波段進場')
        elif context == side and local in {side, 'neutral'} and code in {'trend', 'pullback', 'retest', 'accumulation', 'distribution'}:
            mode, score = 'trend_retest', 70
        elif context == 'neutral' and composite == side and code in {'accumulation', 'distribution'}:
            mode, score = 'trend_retest', 68
        elif code in {'range', 'wide_range', 'compression'}:
            support = (frame.get('display') or {}).get('support')
            resistance = (frame.get('display') or {}).get('resistance')
            if support and resistance and support['high'] < resistance['low']:
                position = (last-support['high'])/(resistance['low']-support['high'])
                edge = position <= .35 if side == 'long' else position >= .65
                if edge and context in {'neutral', side}:
                    mode, score = 'range_edge', 65
                else:
                    reasons.append('未在本方向區間邊界；也未形成可用的30分鐘短線趨勢')
            else:
                reasons.append('震盪邊界不完整，且沒有合格30分鐘短線趨勢')
        elif code in {'transition', 'breakout', 'false_break'}:
            # An intact break can be traded only through an established local setup,
            # not simply because the broad regime label says "transition".
            if local == side and context in {'neutral', side}:
                mode, score, allowed_stages = 'broken_trend_retest', 68, {1}
            else:
                reasons.append('转折／突破觀察中；等待30分鐘方向與波段回測，不直接追價')
        else:
            reasons.append(f"4h有效方向{LABELS.get(context, context)}、30m短線{LABELS.get(local, local)}；本方向尚無適用模式")
        enabled = [e for e in geometric if e['stage'] in allowed_stages]
        distances = [max(e['low']-last, last-e['high'], 0)/max(a if e['stage'] == 1 else frame.get('atr', a), 1e-9) for e in enabled]
        nearest = min(distances) if distances else None
        for e in plan['entries']:
            e['route_enabled'] = bool(mode and e.get('enabled', True) and e['stage'] in allowed_stages)
            e['route_reason'] = '' if e['route_enabled'] else ('此模式只允許30分鐘獨立短線，不把可能反轉當成4h反轉' if e['stage'] not in allowed_stages else '本筆風險／模式條件未通過')
        if mode:
            flow = snapshot.get('evidence', {}).get('dex_group')
            score += 5 if flow is not None and (1 if side == 'long' else -1)*flow > .1 else 0
            score += 5 if nearest is not None and nearest <= .5 else 0
            if score < cfg['min_fitness']:
                reasons.append(f"本模式契合度 {score} < {cfg['min_fitness']}")
            if nearest is None:
                reasons.append('目前沒有可用進場筆數；距離不適用')
            elif nearest > (1.5 if mode == 'range_edge' else 3):
                reasons.append(f"距可用進場區 {nearest:.2f} ATR，先等待靠近，不追價")
        if not plan['valid']:
            reasons.extend(plan['reasons'])
        funding = num(snapshot.get('quote', {}).get('funding_rate'))
        if funding is not None and (funding > .001 if side == 'long' else funding < -.001):
            reasons.append('本方向資金費率過熱')
        reasons = list(dict.fromkeys(reasons))
        plan.update(setup_mode=mode, setup_label=MODE_LABELS.get(mode, '無適用模式'), setup_fitness=score,
                    setup_reasons=reasons, setup_state='armed' if not reasons else 'blocked',
                    allowed_stages=sorted(allowed_stages) if mode else [],
                    distance_atr=round(nearest, 2) if nearest is not None else None)
        diagnostics[side] = {'geometry': plan['valid'], 'enabled_stages': [e['stage'] for e in enabled],
                             'allowed_stages': plan['allowed_stages'], 'mode': mode, 'fitness': score,
                             'distance_atr': plan['distance_atr'], 'blockers': reasons,
                             'raw_4h': raw, 'effective_4h': context, 'local_30m': local,
                             'composite': composite, 'direction_source': MODE_LABELS.get(mode, '未選定')}
        # Used to revalidate already-published plans, including fast checks.
        snapshot['entry_checks_by_side'][side] = not reasons
        if not reasons:
            candidates.append((nearest, side, plan))
    candidates.sort(key=lambda x: x[0])
    chosen = candidates[0] if candidates else None
    snapshot.update(structural_bias=context, gate_diagnostics=diagnostics,
                    trade_side=chosen[1] if chosen else None, eligible=bool(chosen))
    if chosen:
        _, side, plan = chosen
        snapshot['fitness'], snapshot['reasons'] = plan['setup_fitness'], []
        label = plan['setup_label']
        detail.update(fitness=plan['setup_fitness'], allow_new=True, strategy=label,
                      components=[{'label': label+'基本條件、距離與資金流確認', 'points': plan['setup_fitness']}],
                      action=f"啟用{label}的{'多' if side == 'long' else '空'}方條件計畫；等待觸區與已收5分鐘K拒絕，不是立即市價單。")
    else:
        snapshot['reasons'] = [f"{'多' if side == 'long' else '空'}方："+'；'.join(d['blockers'][:3]) for side, d in diagnostics.items()]
    snapshot['regime_detail'] = detail
