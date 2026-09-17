"""Describe closed-bar market shape; individual setup routing is separate."""
from statistics import median, pstdev


def features(bars):
    from .strategy import atr, ema, true_ranges
    b5, b30, b4 = (bars[f] for f in ('5m', '30m', '4h'))
    c30, c4 = [b['c'] for b in b30], [b['c'] for b in b4]
    a = atr(b30[:-1])
    old = b30[-22:-2]
    high, low = max(b['h'] for b in old), min(b['l'] for b in old)
    last, prev = b30[-1], b30[-2]
    pad = a*.10
    bw = lambda xs: 4*pstdev(xs)/max(sum(xs)/len(xs), 1e-9)
    prior_widths = [bw(c30[i-20:i]) for i in range(40, len(c30), 4)]
    width_ratio = bw(c30[-20:])/max(median(prior_widths), 1e-9) if prior_widths else 1
    vol_base = median(true_ranges(b30)[-80:-14])
    atr_ratio = atr(b30)/vol_base if vol_base > 0 else 1
    eff = lambda xs: abs(xs[-1]-xs[0])/max(sum(abs(y-x) for x,y in zip(xs,xs[1:])), 1e-9)
    breakout = 'long' if last['c'] > high+pad else 'short' if last['c'] < low-pad else None
    failed_up = (last['h'] > high+pad or prev['c'] > high+pad) and last['c'] < high-pad
    failed_down = (last['l'] < low-pad or prev['c'] < low-pad) and last['c'] > low+pad
    retest = None
    for offset in range(2, 6):
        i = len(b30)-offset
        window = b30[i-20:i]
        top, bottom = max(b['h'] for b in window), min(b['l'] for b in window)
        following = b30[i+1:]
        if b30[i]['c'] > top+pad and all(b['c'] >= top-pad for b in following) and last['l'] <= top+pad and last['c'] > top:
            retest = 'long'
        if b30[i]['c'] < bottom-pad and all(b['c'] <= bottom+pad for b in following) and last['h'] >= bottom-pad and last['c'] < bottom:
            retest = 'short'
    return {'efficiency_4h': eff(c4[-21:]), 'efficiency_30m': eff(c30[-21:]),
            'volatility_ratio': atr_ratio, 'bandwidth_ratio': width_ratio,
            'range_width_atr': (high-low)/max(a,1e-9), 'breakout': breakout,
            'false_break': failed_up or failed_down, 'retest': retest,
            'close_vs_4h_slow': c4[-1]-ema(c4,50),
            'shock': true_ranges(b5)[-1] > 3*atr(b5[:-1]) or true_ranges(b30)[-1] > 2.8*a}


def classify(f, trend4, trend30, flow, ndir, dex, sustained):
    trend = abs(trend4) >= .30 and f['efficiency_4h'] >= .18
    mixed = trend and trend4*trend30 < -.12 and trend4*f['close_vs_4h_slow'] < 0
    if f['shock']:
        return 'shock','急變／高波動',18,False,'暫停新入場；反向／結構失效警示照常，不猜新聞成因。'
    if f['false_break']:
        return 'false_break','假突破／掃過區間後收回',32,False,'不追掃線；合格短線模式另行檢查，不把本分類當全面禁令。'
    if f['retest']:
        return 'retest','突破後回測' if f['retest']=='long' else '跌破後反測',76,True,'檢查持續波段回測與各筆獨立風險。'
    if mixed:
        return 'transition','多週期衝突／轉折過渡',42,False,'先檢查舊主要趨勢是否失效；短線模式独立评估，不直接禁止。'
    if f['breakout']:
        return 'breakout','向上突破／擴張' if f['breakout']=='long' else '向下跌破／擴張',52,False,'不直接追價；已具短線波段與回測則另行評估。'
    if not trend and sustained and ndir is not None and dex is not None and ndir*dex > 0:
        return ('accumulation','區間疑似吸籌',74,True,'連續資金流偏多，仍須支撐與合理盈虧比。') if flow>0 else ('distribution','區間疑似派發',74,True,'連續資金流偏空，仍須壓力與合理盈虧比。')
    if trend:
        pullback = trend4*trend30 < -.03
        label = ('多頭趨勢中的回調' if pullback else '多頭趨勢延續') if trend4>0 else ('空頭趨勢中的反彈' if pullback else '空頭趨勢延續')
        return 'pullback' if pullback else 'trend',label,72 if pullback else 68,True,'主要結構／30分鐘短線分開判斷；每筆獨立進場與止損。'
    if f['bandwidth_ratio'] < .72 and f['volatility_ratio'] < 1.0:
        return 'compression','窄幅震盪／波動壓縮',40,False,'檢查30分鐘短線趨勢或邊界，沒有則等待，不把中性當永久禁令。'
    if f['volatility_ratio'] > 1.35 or f['range_width_atr'] > 7:
        return 'wide_range','寬幅震盪／雙向拉扯',34,False,'邊界拒絕與短線趨勢分開評估；中間無結構時不開單。'
    return 'range','一般震盪／區間整理',38,False,'檢查30分鐘獨立趨勢／區間邊界模式；偏向中性本身不擋短線。'


def assess_regime(bars, trend4, trend30, direction, flow, ndir, dex, sustained, relative_volume):
    f = features(bars)
    code,label,base,allow,action = classify(f,trend4,trend30,flow,ndir,dex,sustained)
    components = [{'label':'盤勢與主要結構策略基本相容性','points':base}]
    if code != 'shock':
        components.append({'label':'4小時／30分鐘趨勢配合','points':8 if trend4*trend30>.05 else -5})
        components.append({'label':'方向與已取得資金流配合','points':8 if direction*flow>.04 else -8 if direction*flow<-.04 else 0})
        components.append({'label':'成交量相對ETH自身歷史','points':4 if relative_volume is not None and 1<=relative_volume<=3 else 0})
    raw = sum(x['points'] for x in components)
    score = max(0,min(68 if dex is None and ndir is None else 95,raw))
    if score != raw:
        components.append({'label':'資料缺失上限／分數邊界','points':score-raw})
    return {'code':code,'label':label,'fitness':round(score),'allow_new':allow,'action':action,
            'components':components,'metrics':f,
            'reasons':[f"4小時方向效率 {f['efficiency_4h']:.2f}；30分鐘 {f['efficiency_30m']:.2f}",
                       f"ATR相對歷史 {f['volatility_ratio']:.2f}倍；波動帶寬 {f['bandwidth_ratio']:.2f}倍",
                       '有持續流量證據' if sustained else '尚無足夠連續鏈上證據，不把橫盤叫吸籌'],
            'strategy':'ETH分層波段／短線回測／區間邊界', 'score_meaning':'規則契合分，不是勝率'}
