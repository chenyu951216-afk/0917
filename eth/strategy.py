"""ETH-only, point-in-time strategy. No network calls and no order execution.

Scores are transparent heuristics, NOT probabilities or backtest results.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP
from statistics import median

TZ = timezone(timedelta(hours=8))
SYMBOL = "ETH_USDT"
VERSION = "eth-20260917-r4"
GRACE = 12  # exchange candle publication allowance, after the candle close
DEFAULTS = {
    "min_fitness": 60,
    "min_net_rr": 1.0,
    "fee_per_side": 0.0005,
    "slippage_roundtrip": 0.0004,
    "funding_reserve": 0.0002,
    "plan_lifetime_hours": 12,
    "nansen_ttl": 1800,
    "enabled": True,
}


def num(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError, OverflowError):
        return None


def clip(value, lo=-1.0, hi=1.0):
    return max(lo, min(hi, value))


def local_time(stamp):
    return datetime.fromtimestamp(stamp, TZ).isoformat(timespec="seconds")


def interval_at(stamp):
    """Weekday-anchored sessions; Friday evening includes Saturday 00:00-04:00."""
    d = datetime.fromtimestamp(stamp, TZ)
    minute = d.hour * 60 + d.minute
    daytime = d.weekday() < 5 and 600 <= minute < 900
    evening = d.weekday() < 5 and minute >= 1080
    overnight = (d - timedelta(days=1)).weekday() < 5 and minute < 240
    return 300 if daytime or evening or overnight else 900


def scan_slot(stamp):
    effective = int(stamp) - GRACE
    step = interval_at(effective)
    return effective // step * step


def next_scan(stamp):
    candidate = (int(stamp) // 60 + 1) * 60
    # The schedule boundaries align with five-minute UTC boundaries.
    if int(stamp) % 60 < GRACE:
        candidate -= 60
    for _ in range(32):
        if candidate + GRACE > stamp and candidate % interval_at(candidate) == 0:
            return candidate + GRACE
        candidate += 60
    raise RuntimeError("invalid schedule")


def closed_candles(rows, seconds, now):
    """Accept Gate futures object candles only; reject NaN, malformed and open bars."""
    out = {}
    for raw in rows if isinstance(rows, list) else []:
        if not isinstance(raw, dict):
            continue
        values = {k: num(raw.get(k)) for k in ("t", "o", "h", "l", "c", "v")}
        if any(values[k] is None for k in ("t", "o", "h", "l", "c")):
            continue
        t = int(values["t"])
        if t != values["t"] or t % seconds or t + seconds > now - GRACE:
            continue
        if min(values[k] for k in ("o", "h", "l", "c")) <= 0:
            continue
        if not values["l"] <= min(values["o"], values["c"]) <= max(values["o"], values["c"]) <= values["h"]:
            continue
        if values["v"] is not None and values["v"] < 0:
            continue
        values.update(t=t, close_time=t + seconds, v=values["v"] or 0.0)
        out[t] = values
    return [out[t] for t in sorted(out)]


def history_error(bars, seconds, now, minimum=80):
    if len(bars) < minimum:
        return f"已收 K 不足 {minimum} 根"
    expected = (int(now) - GRACE) // seconds * seconds
    if bars[-1]["close_time"] != expected:
        return "最新已收 K 缺失／過期"
    recent = bars
    if any(b["t"] - a["t"] != seconds for a, b in zip(recent, recent[1:])):
        return "K 線中間缺根"
    return None


def ema(values, length):
    if not values:
        return 0.0
    value = values[0]
    alpha = 2.0 / (length + 1)
    for item in values[1:]:
        value += alpha * (item - value)
    return value


def true_ranges(bars):
    return [max(b["h"] - b["l"], abs(b["h"] - a["c"]), abs(b["l"] - a["c"]))
            for a, b in zip(bars, bars[1:])]


def atr(bars, length=14):
    values = true_ranges(bars)[-length:]
    return sum(values) / len(values) if values else 0.0


def round_tick(value, tick, direction=0):
    tick = Decimal(str(tick))
    if tick <= 0:
        raise ValueError("missing price tick")
    rounding = ROUND_FLOOR if direction < 0 else ROUND_CEILING if direction > 0 else ROUND_HALF_UP
    return float((Decimal(str(value)) / tick).to_integral_value(rounding=rounding) * tick)


from .levels import structure_levels, project_levels
from .planning import plan_for
from .regime import assess_regime
from .setups import route_setups
from .structure_state import analyze_structure, short_term_trend


def robust_direction(value, history):
    if value is None:
        return None, False
    clean = [v for x in history if (v := num(x)) is not None]
    if len(clean) < 12:
        return (0.2 if value > 0 else -0.2 if value < 0 else 0.0), False
    scale = median(abs(v) for v in clean[-96:])
    if scale <= 0:
        return (0.2 if value > 0 else -0.2 if value < 0 else 0.0), False
    return clip(value / (3 * scale)), True


def evaluate(market, feeds, history, now, cfg=None):
    cfg = {**DEFAULTS, **(cfg or {})}
    bars = market['bars']
    minimums = {'5m': 80, '30m': 240, '4h': 180}
    errors = [f"{frame}：{error}" for frame, seconds in (('5m', 300), ('30m', 1800), ('4h', 14400))
              if (error := history_error(bars.get(frame, []), seconds, now, minimums[frame]))]
    out = {'version': VERSION, 'symbol': SYMBOL, 'as_of': now, 'bias': 'neutral',
           'bias_label': '資料不足／觀望', 'fitness': None, 'eligible': False,
           'reasons': errors, 'quote': market.get('quote', {}), 'feeds': feeds,
           'levels': {}, 'plans': {}, 'regime': '資料不足', 'direction_score': None,
           'entry_checks_ok': False, 'relative_volume': None, 'data_requirements': minimums,
           'history_counts': {f: len(v) for f, v in bars.items()}}
    if errors:
        return out
    b5, b30, b4 = bars['5m'], bars['30m'], bars['4h']
    live = num(out['quote'].get('last'))
    fresh_quote = live is not None and live > 0 and -5 <= now-out['quote'].get('observed_at', 0) <= 90
    price = live if fresh_quote else b30[-1]['c']
    out['level_reference'] = {'price': price, 'kind': 'Gate last' if fresh_quote else '已收30分鐘收盤（非現價）'}
    tick = num(market.get('tick'))
    levels = {f: structure_levels(bars[f], f, price, tick or .01) for f in ('30m', '4h')}
    out['levels'] = levels = project_levels(levels, price)
    out['structure_charts'] = {f: [{k: b[k] for k in ('t', 'close_time', 'o', 'h', 'l', 'c')}
                                 for b in bars[f][-120:]] for f in ('30m', '4h')}
    out['structure_chart'] = out['structure_charts']['4h']
    a30, a4, a5 = atr(b30), atr(b4), atr(b5[:-1])
    if min(a30, a4, a5) <= 0:
        out['reasons'].append('ATR無效／行情停止更新')
        return out
    states = {f: analyze_structure(levels[f], b30) for f in ('30m', '4h')}
    out['structure_states'] = states
    out['short_term'] = short_term_trend(b30, states['30m'])
    c30, c4 = [b['c'] for b in b30], [b['c'] for b in b4]
    trend4 = clip((ema(c4, 20)-ema(c4, 50))/a4)
    trend30 = clip((ema(c30, 9)-ema(c30, 21))/a30)
    base_volume = median(b['v'] for b in b30[-41:-1])
    relative_volume = b30[-1]['v']/base_volume if base_volume > 0 else None
    valid = {k: v for k, v in feeds.items() if v.get('ok') and not v.get('stale')}
    nflow = valid.get('nansen', {}).get('smart_net_usd')
    ndir, warmed = robust_direction(nflow, [x['value'] for x in history if x.get('provider') == 'nansen'])
    dex_values = [v.get('imbalance') for k, v in valid.items() if k in {'birdeye', 'bitquery'} and num(v.get('imbalance')) is not None]
    dex = sum(dex_values)/len(dex_values) if dex_values else None
    flow = .7*(dex or 0)+.3*(ndir or 0)
    directional = clip(.46*trend4+.19*trend30+.27*(dex or 0)+.08*(ndir or 0))
    bias = 'long' if directional >= .20 else 'short' if directional <= -.20 else 'neutral'
    observations = [x for x in history if x.get('provider') == 'dex' and now-14400 <= x['at'] <= now]
    sign = 1 if flow > 0 else -1
    sustained = len(observations) >= 3 and observations[-1]['at']-observations[0]['at'] >= 3600 and all(sign*x['value'] > .08 for x in observations[-3:])
    assessment = assess_regime(bars, trend4, trend30, directional, flow, ndir, dex, sustained, relative_volume)
    assessment['structure_state'] = states['4h']['state']
    if states['4h']['state'] == 'broken' and assessment['code'] != 'shock':
        assessment['reasons'] += states['4h']['evidence']
        assessment['label'] += '／舊主要趨勢已失效'
    safety = []
    if not fresh_quote:
        safety.append('Gate即時報價缺失／過期')
    bid, ask = num(out['quote'].get('bid')), num(out['quote'].get('ask'))
    if bid is None or ask is None or not 0 < bid <= ask or (ask-bid)/((ask+bid)/2) > .002:
        safety.append('Gate買賣報價缺失／異常價差')
    if tick is None or tick <= 0:
        safety.append('Gate價格最小單位尚未取得')
    else:
        out['plans'] = {side: plan_for(side, levels, price, tick, now, cfg) for side in ('long', 'short')}
    if not cfg.get('enabled', True):
        safety.append('新策略已暫停；整點與既有計畫風控仍啟用')
    out.update(bias=bias, bias_label={'long': '偏多', 'short': '偏空', 'neutral': '中性／觀望'}[bias],
               regime=assessment['label'], regime_detail=assessment, fitness=assessment['fitness'],
               direction_score=round(directional, 4), relative_volume=relative_volume,
               coverage=round(50+50*len(valid)/3),
               evidence={'trend_4h': trend4, 'trend_30m': trend30, 'dex_group': dex,
                         'nansen_flow_hint': ndir, 'nansen_warmed': warmed, 'sustained_flow': sustained},
               closed_at={f: bars[f][-1]['close_time'] for f in bars},
               basis='30m持續波段720根／4h主要波段540根；資料不足不補造K線；方向與模式分開',
               limitations='WETH是部分市場代理；規則契合分不是勝率；斜線突破不等於完整反轉',
               safety_reasons=safety, entry_checks_ok=not safety and assessment['code'] != 'shock')
    if out['plans']:
        route_setups(out, bars, cfg, safety)
    else:
        out['reasons'] = safety or ['尚無完整候選結構']
    return out


def risk_event(plan, bars5, now):
    """Only newly CLOSED bars after publication may invalidate a published plan."""
    if not plan or history_error(bars5, 300, now, 30):
        return None
    relevant = [b for b in bars5 if b["close_time"] > plan["created_at"]]
    if not relevant:
        return None
    side = plan["side"]
    last = relevant[-1]
    sign = 1 if side == "long" else -1
    if sign * (last["c"] - plan["stop"]) < 0:
        return {"kind": "invalidation", "reason": "已收 5 分鐘 K 越過原 4 小時結構失效線", "bar": last["close_time"]}
    # Two closed five-minute bars through the first-entry structural zone + momentum.
    if len(relevant) >= 2:
        boundary = plan["entries"][0]["low" if side == "long" else "high"]
        closes = [b["c"] for b in bars5]
        opposite_momentum = sign * (ema(closes, 5) - ema(closes, 20)) < 0
        if opposite_momentum and all(sign * (b["c"] - boundary) < 0 for b in relevant[-2:]):
            return {"kind": "opposition", "reason": "短線反向警戒：兩根已收 5 分鐘 K 穿越首筆區；不等於 4 小時方向反轉，第二筆須重新確認", "bar": last["close_time"]}
    return None
