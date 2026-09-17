"""Compact strategy cards; complete explanations belong to hourly reports."""
import time
from .levels import project_levels
from .structure_state import LABELS as DIRECTIONS

LABELS = {'hourly': '整點盤勢報告', 'signal': '條件計畫／等待觸發',
          'entry_confirmed': '已收K入場確認（非成交回報）', 'reversal': '反向確認／原方向撤銷',
          'bias_change': '方向轉變預警', 'structure_break': '主要趨勢結構破壞',
          'leg_stop': '單筆結構失效', 'invalidation': '結構失效', 'opposition': '反向風控預警',
          'expired': '計畫到期', 'recalibration': '舊計畫撤銷／重新計算', 'resume': '條件重新確認', 'test': '通知測試'}


def price(v):
    from .strategy import num
    v = num(v)
    return f'{v:,.2f}' if v is not None else '—'


def level_text(frame):
    lines = []
    for role, label in (('support', '支撐'), ('resistance', '壓力')):
        display = frame.get('display')
        z = display.get(role) if display is not None else next(iter(frame.get(role, [])), None)
        lines.append(label+'：'+(f"{price(z['low'])}–{price(z['high'])}" if z else '尚無已確認主結構'))
        if z and z.get('selection_reason'):
            lines.append(z['selection_reason'])
    if frame.get('notes'):
        lines.append(frame['notes'][0])
    return '\n'.join(lines)[:1000]


def _quote(snapshot, now, detailed):
    from .strategy import num, local_time
    q = snapshot.get('delivery_quote') or snapshot.get('quote', {})
    last = num(q.get('last'))
    fresh = last is not None and last > 0 and q.get('ok', True) and -5 <= now-q.get('observed_at', 0) <= 30
    if not fresh:
        return {'name': 'Gate 現價', 'value': '現價暫時取得失敗；風控照常通知，不以舊價替代。'}, None
    text = f"**{price(last)} USDT**（last）｜{local_time(q['observed_at'])[11:19]} UTC+8"
    if detailed:
        text += f"\n標記 mark {price(q.get('mark'))}｜指數 index {price(q.get('index'))}"
    return {'name': 'Gate 最新價格', 'value': text}, last


def payload(snapshot, kind, *, plan=None, reason='', now=None, stage=None, detailed=None):
    from .strategy import VERSION, local_time
    now = now or time.time()
    detailed = kind == 'hourly' if detailed is None else detailed
    age = now-snapshot.get('as_of', 0)
    stale = age > 1800 or not snapshot.get('levels')
    side = (plan or {}).get('side') or snapshot.get('trade_side') or snapshot.get('bias')
    side_title = 'LONG 做多' if side == 'long' else 'SHORT 做空' if side == 'short' else '中性觀察'
    color = 0x42C886 if side == 'long' else 0xED6078 if side == 'short' else 0x6EA3DD
    title = 'ETH/USDT — '+(LABELS.get(kind, kind) if detailed else side_title+'｜'+LABELS.get(kind, kind))
    fitness = (plan or {}).get('setup_fitness', snapshot.get('fitness'))
    mode = (plan or {}).get('setup_label') or (plan or {}).get('setup_mode') or ''
    description = (mode+'｜' if mode else '')+f"契合度 {'—' if fitness is None else str(fitness)+'/100'}（不是勝率）"
    if stale:
        description += '｜⚠ 分析快照過期'
    fields = []
    quote_field, live = _quote(snapshot, now, detailed)
    if detailed:
        fields += [{'name': '方向偏向／盤勢', 'value': snapshot.get('bias_label', '未知')+'｜'+snapshot.get('regime', '等待分析')},
                   {'name': '策略契合度與處理', 'value': snapshot.get('regime_detail', {}).get('action', '等待有效輸入')}]
        states = snapshot.get('structure_states', {})
        for name in ('4h', '30m'):
            state = states.get(name, {})
            if state:
                fields.append({'name': name+'結構狀態', 'value': state.get('summary', '')+'\n'+'\n'.join(state.get('evidence', [])[:2])})
        local = snapshot.get('short_term', {})
        fields.append({'name': '30分鐘短線方向', 'value': DIRECTIONS.get(local.get('direction'), '尚無')+'｜'+local.get('source', '')+'\n'+'\n'.join(local.get('evidence', []))})
        fields.append(quote_field)
        levels = project_levels(snapshot.get('levels', {}), live) if live else {}
        for f in ('30m', '4h'):
            fields.append({'name': f+'持續波段｜各一組支撐／壓力', 'value': level_text(levels.get(f, {}))})
        diagnostics = snapshot.get('gate_diagnostics', {})
        for s in ('long', 'short'):
            d = diagnostics.get(s, {})
            distance = d.get('distance_atr')
            text = f"模式：{d.get('direction_source') or d.get('mode') or '尚無'}｜契合分 {d.get('fitness', '—')}\n距離："+(f'{distance:.2f} ATR' if isinstance(distance, (float, int)) else '不適用（無合格筆數）')
            text += '\n'+('；'.join(d.get('blockers', [])) or '沒有模式阻擋，仍等收K觸發')
            fields.append({'name': ('多' if s == 'long' else '空')+'方模式診斷', 'value': text})
        feeds = snapshot.get('feeds', {})
        fields.append({'name': '來源狀態', 'value': '\n'.join(p+'：'+('有效' if feeds.get(p, {}).get('ok') and not feeds.get(p, {}).get('stale') else '缺失／未使用') for p in ('nansen','birdeye','bitquery'))+'\nWETH為部分市場代理；兩個DEX來源不重複加分。'})
        fields.append({'name': '歷史K線', 'value': '\n'.join(f"{f}：實際 {snapshot.get('history_counts', {}).get(f, '—')} 根已收K" for f in ('30m', '4h'))})
    else:
        fields.append(quote_field)
    if plan and not detailed:
        entries = plan.get('entries', [])
        if stage is not None:
            entries = [e for e in entries if e['stage'] == stage]
        elif kind in {'signal', 'entry_confirmed', 'resume', 'test'}:
            entries = [e for e in entries if e.get('enabled', True) and e.get('route_enabled', True)]
        for e in entries[:2]:
            anchor = e.get('trigger', {}).get('price', e['price'])
            sign = 1 if side == 'long' else -1
            stop = e.get('stop', plan.get('stop'))
            slpct = abs(anchor-stop)/anchor*100 if anchor and stop else 0
            fields.append({'name': f"Entry｜第{e['stage']}筆 {e['frame']}",
                           'value': f"`{price(e['low'])} ～ {price(e['high'])}`\n"+('已收K確認' if e.get('signal_state') == 'entry_confirmed' else '已撤銷' if e.get('signal_state') == 'cancelled' else '等待觸區＋收K')+f"｜最多預定數量 {e.get('quantity_fraction', .5):.0%}"})
            fields.append({'name': f"Stop Loss｜第{e['stage']}筆（本筆SL）", 'value': f"`{price(stop)}`（風險距離 {slpct:.2f}%）"})
            targets = e.get('targets', plan.get('targets', []))
            text = '\n'.join(f"**TP{i}** `{price(t['price'])}`（{sign*(t['price']-anchor)/anchor*100:+.2f}%）｜平本筆 {t['quantity_fraction']:.0%}" for i,t in enumerate(targets,1))
            fields.append({'name': f"Take Profits｜第{e['stage']}筆", 'value': text or '尚無有效目標，不進場'})
            rr = e.get('net_rr')
            if rr is not None:
                fields.append({'name': f"Risk｜第{e['stage']}筆", 'value': f"計畫成本後 {rr:.2f}R｜不放大另一筆額度"})
        fields.append({'name': '有效期／成本', 'value': f"至 {local_time(plan.get('expires_at', now))[:19].replace('T',' ')} UTC+8｜成本假設 {plan.get('cost_pct', .16):.3f}%"})
    if reason and (not detailed or kind != 'hourly'):
        fields.append({'name': '狀態／必要處理', 'value': reason})
    fields.append({'name': '時間 UTC+8', 'value': local_time(now)[:19].replace('T',' ')+(f"｜分析 {local_time(snapshot['as_of'])[11:19]}" if snapshot.get('as_of') else '')})
    # Bound the whole embed, not only individual field strings.
    clean, budget = [], 5400-len(title)-len(description)
    for field in fields:
        name = str(field['name'])[:200]
        value = str(field['value'])[:min(1000, max(1, budget-len(name)))] or '—'
        clean.append({'name': name, 'value': value, 'inline': False})
        budget -= len(name)+len(value)
        if budget < 100 or len(clean) >= 24:
            break
    return {'allowed_mentions': {'parse': []}, 'embeds': [{'title': title[:256], 'color': color,
            'description': description[:1000], 'fields': clean,
            'footer': {'text': VERSION+'｜只做訊號，不是成交回報；沒有自動下單／平倉；不保證收益。'}}]}
