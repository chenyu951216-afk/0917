"""R4 regressions: neutral/local routing, invalidated trends, history and DC."""
import copy
import json
import math
import tempfile
import time
import unittest
from unittest.mock import patch

from eth.strategy import DEFAULTS, VERSION, evaluate, closed_candles, history_error
from eth.levels import structure_levels, external_swings, project_frame, PROFILES
from eth.structure_state import analyze_structure, short_term_trend, _crossing, structural_bias
from eth.setups import route_setups
from eth.planning import plan_for, main_zone
from eth.presentation import payload
from eth.runtime import event, transition
from eth.providers import Providers, CANDLE_COUNTS, BITQUERY
from eth.store import Store
from eth.notify import Discord
from tests.test_eth import bars, zone, levels, snapshot, at, Response, Vault
from whale.db import DB


def pivot(p, role, t, frame='4h'):
    return dict(zone(p, frame), origin_role=role, role=role, pivot_at=t,
                confirmed_at=t+2*14400, tier='external', atr_at_origin=2,
                extreme_price=p, structural_low=p-1, structural_high=p+1)


def falling_frame():
    return {'frame':'4h','atr':2, 'zones':[pivot(110,'resistance',14400), pivot(90,'support',3*14400),
                                          pivot(100,'resistance',13*14400),pivot(85,'support',14*14400)]}


def simple_bars(values, start=15*14400):
    return [dict(t=start+i*1800,close_time=start+(i+1)*1800,o=v-.2,c=v,h=v+.3,l=v-.3,v=1000) for i,v in enumerate(values)]


def routed(side='long', macro='neutral', raw=None, local='long', code='range', bias='neutral', broken=False, valid=True):
    s={'levels':levels(), 'regime_detail':{'code':code}, 'bias':bias, 'quote':{}, 'evidence':{},
       'structure_states':{'4h':{'raw_bias':raw or macro,'effective_bias':macro,'state':'broken' if broken else 'intact'}},
       'short_term':{'direction':local}, 'plans':{}}
    for d in ['long','short']:
        p=plan_for(d,s['levels'],4000,.01,1000)
        if not valid:
            for e in p['entries']: e['enabled']=False;e['reasons']=['不合格']
            p['valid']=False;p['reasons']=['沒有合格筆數']
        s['plans'][d]=p
    b={'5m':[dict(c=3990)]}
    route_setups(s,b,DEFAULTS,[])
    return s


class StructureStateTests(unittest.TestCase):
    def test_raw_lower_high_and_lower_low(self):
        self.assertEqual(structural_bias(falling_frame()),'short')
    def test_two_closed_bars_break_diagonal_before_horizontal_high(self):
        f=falling_frame();bs=simple_bars([96]*8+[98.4,98.5])
        result=analyze_structure(f,bs)
        self.assertEqual(result['state'],'broken')
        self.assertEqual(result['effective_bias'],'neutral')
        self.assertEqual(result['break_direction'],'long')
        self.assertEqual(result['break_evidence']['kind'],'trendline')
        self.assertFalse(result['reversal_confirmed'])
    def test_symmetric_uptrend_break(self):
        f=falling_frame()
        for p in f['zones']:
            p.update(price=200-p['price'],extreme_price=200-p['extreme_price'],
                     origin_role='support' if p['origin_role']=='resistance' else 'resistance')
            p['role']=p['origin_role'];p['low']=p['price']-2;p['high']=p['price']+2
            p['structural_low']=p['price']-1;p['structural_high']=p['price']+1
        r=analyze_structure(f,simple_bars([104]*8+[101.6,101.5]))
        self.assertEqual(r['state'],'broken');self.assertEqual(r['break_direction'],'short')
    def test_one_close_is_not_confirmed(self):
        r=_crossing(simple_bars([90,91,101]),lambda _:100,1,0,.1)
        self.assertFalse(r['confirmed']);self.assertTrue(r['candidate'])
    def test_wick_is_not_close_break(self):
        bs=simple_bars([99,99]);bs[-1]['h']=105
        self.assertFalse(_crossing(bs,lambda _:100,1,0,.1)['confirmed'])
    def test_missing_bar_resets_consecutive_confirmation(self):
        bs=simple_bars([101,102]);bs[1]['close_time']+=1800
        self.assertFalse(_crossing(bs,lambda _:100,1,0,.1)['confirmed'])
    def test_one_recross_does_not_restore_old_veto(self):
        self.assertTrue(_crossing(simple_bars([101,102,99]),lambda _:100,1,0,.1)['confirmed'])
    def test_two_recrosses_fail_break(self):
        r=_crossing(simple_bars([101,102,99,98]),lambda _:100,1,0,.1)
        self.assertFalse(r['confirmed']);self.assertTrue(r['failed'])
    def test_unconfirmed_future_anchors_not_used(self):
        f=falling_frame()
        for p in f['zones']: p['confirmed_at']=99999999
        r=analyze_structure(f,simple_bars([98,100,102]))
        self.assertFalse(r['lines']);self.assertNotEqual(r['state'],'broken')
    def test_prefix_break_time_stable(self):
        b=simple_bars([99,101,102,103])
        a=_crossing(b[:3],lambda _:100,1,0,.1);c=_crossing(b,lambda _:100,1,0,.1)
        self.assertEqual(a['confirmed_at'],c['confirmed_at'])
    def test_clear_local_trend_even_when_macro_neutral(self):
        b=bars(1800,count=120,step=2)
        self.assertEqual(short_term_trend(b,{'effective_bias':'neutral'})['direction'],'long')


class SustainedStructureTests(unittest.TestCase):
    def test_30m_uses_swing_model_and_time_context(self):
        r=structure_levels(bars(1800,count=720), '30m',4000,.01)
        ext=[z for z in r['zones'] if z['tier']=='external']
        self.assertTrue(ext)
        for z in ext:
            self.assertGreaterEqual(z['confirmation_right_bars'],3)
            self.assertGreaterEqual(z['context_bars'],6)
            self.assertGreaterEqual(z['departure_atr'],1.3)
        for z in r['display'].values():
            if z:self.assertEqual(z['tier'],'external')
    def test_no_internal_fallback_for_30m(self):
        z=dict(zone(3980,'30m'),tier='internal',role='support')
        r=project_frame({'frame':'30m','zones':[z]},4000)
        self.assertIsNone(r['display']['support']);self.assertIsNone(main_zone(r,'support'))
    def test_prefix_causality_30m_and_4h(self):
        for f,sec in [('30m',1800),('4h',14400)]:
            b=bars(sec,count=200);full=structure_levels(b,f,4000,.01)
            before=structure_levels(b[:150],f,4000,.01)
            byid={z['id']:z for z in full['zones']}
            for z in before['zones']:
                self.assertIn(z['id'],byid)
                for k in ('low','high','structural_low','structural_high','confirmed_at'):
                    self.assertEqual(z[k],byid[z['id']][k])
    def test_historical_origin_width_not_current_atr(self):
        b=bars(1800,count=200);base=structure_levels(b,'30m',4000,.01)
        later=copy.deepcopy(b)+[dict(b[-1],t=b[-1]['t']+1800,close_time=b[-1]['close_time']+1800,h=9000,l=100,c=4000)]
        new=structure_levels(later,'30m',4000,.01);zs={z['id']:z for z in new['zones']}
        for z in base['zones']:self.assertEqual(z['structural_low'],zs[z['id']]['structural_low'])
    def test_planned_width_is_bounded(self):
        for f,sec in [('30m',1800),('4h',14400)]:
            r=structure_levels(bars(sec,count=240),f,4000,.01)
            for z in r['zones']:self.assertLessEqual(z['high']-z['low'],z['width_cap']+1e-6)
    def test_current_quote_never_changes_raw_structural_bias(self):
        b=bars(1800,count=240)
        a=structure_levels(b,'30m',3950,.01);c=structure_levels(b,'30m',4050,.01)
        self.assertEqual(structural_bias(a),structural_bias(c))
    def test_fast_impulse_does_not_freeze_swing_machine(self):
        b=bars(1800,count=200)
        pivots,_=external_swings(b,1.3,3,6)
        self.assertGreater(len(pivots),4)


class RoutingRevisionTests(unittest.TestCase):
    def test_neutral_does_not_block_local_long(self):
        s=routed();self.assertTrue(s['eligible']);self.assertEqual(s['trade_side'],'long')
        self.assertEqual(s['plans']['long']['allowed_stages'],[1])
    def test_neutral_does_not_block_local_short(self):
        s=routed(local='short');s['plans']['short']['entries'][0]['low']=3990;s['plans']['short']['entries'][0]['high']=3995
        route_setups(s,{'5m':[{'c':3990}]},DEFAULTS,[])
        self.assertTrue(s['eligible']);self.assertEqual(s['trade_side'],'short')
    def test_composite_neutral_has_independent_short_mode(self):
        s=routed(macro='short',local='long',bias='neutral',code='trend')
        self.assertEqual(s['plans']['long']['setup_mode'],'short_trend');self.assertTrue(s['eligible'])
    def test_broken_macro_no_old_short_veto(self):
        s=routed(macro='neutral',raw='short',local='long',broken=True,bias='short',code='transition')
        self.assertTrue(s['eligible']);self.assertEqual(s['plans']['long']['setup_mode'],'broken_trend_retest')
    def test_intact_opposing_macro_and_composite_still_needs_evidence(self):
        s=routed(macro='short',local='long',bias='short',code='trend')
        self.assertFalse(s['plans']['long']['setup_state']=='armed')
    def test_short_mode_never_enables_second_leg(self):
        s=routed();p=s['plans']['long']
        self.assertTrue(p['entries'][0]['route_enabled']);self.assertFalse(p['entries'][1]['route_enabled'])
        self.assertEqual(p['entries'][0]['quantity_fraction'],.5)
    def test_no_999_sentinel_without_usable_legs(self):
        s=routed(valid=False)
        self.assertFalse(s['eligible'])
        for p in s['plans'].values():self.assertIsNone(p['distance_atr'])
        self.assertNotIn('999',json.dumps(s['reasons'],ensure_ascii=False))
    def test_shock_is_still_a_hard_stop(self):
        s=routed(broken=True,code='shock');self.assertFalse(s['eligible'])
    def test_safety_cannot_be_overridden_by_mode(self):
        s=routed();route_setups(s,{'5m':[{'c':3990}]},DEFAULTS,['報價缺失'])
        self.assertFalse(s['eligible']);self.assertIn('報價缺失',s['plans']['long']['setup_reasons'])
    def test_transition_with_no_short_trend_does_not_force_entry(self):
        s=routed(local='neutral',broken=True,code='transition');self.assertFalse(s['eligible'])
    def test_no_geometry_does_not_claim_distance(self):
        s=routed(valid=False)
        self.assertTrue(all(x['distance_atr'] is None for x in s['gate_diagnostics'].values()))
    def test_diagnostic_names_actual_direction_source(self):
        s=routed(broken=True,raw='short')
        d=s['gate_diagnostics']['long']
        self.assertEqual(d['raw_4h'],'short');self.assertEqual(d['effective_4h'],'neutral');self.assertEqual(d['local_30m'],'long')


class DisplayRevisionTests(unittest.TestCase):
    def setUp(self):
        self.now=time.time();self.s=snapshot(self.now);self.s['quote']={'last':4000,'observed_at':self.now}
        self.p=self.s['plans']['long']
    def text(self,kind,**kw):return json.dumps(payload(self.s,kind,plan=self.p,now=self.now,**kw),ensure_ascii=False)
    def test_compact_strategy_card(self):
        t=self.text('signal')
        for word in ('LONG','Entry','Stop Loss','Take Profits','4,000.00 USDT'):self.assertIn(word,t)
        for word in ('來源狀態','持續波段｜各一組','方向效率','上游'):self.assertNotIn(word,t)
    def test_hourly_keeps_detail(self):
        t=self.text('hourly');self.assertIn('來源狀態',t);self.assertIn('模式診斷',t);self.assertIn('支撐',t)
    def test_entry_card_shows_only_affected_leg(self):
        t=self.text('entry_confirmed',stage=1)
        self.assertIn('第1筆',t);self.assertNotIn('第2筆',t)
    def test_percentages_and_stops_present(self):
        t=self.text('signal');self.assertIn('風險距離',t);self.assertIn('平本筆',t);self.assertIn('50%',t)
    def test_no_mentions_allowed(self):self.assertEqual(payload(self.s,'signal',plan=self.p)['allowed_mentions']['parse'],[])
    def test_all_event_types_fit_discord_limits(self):
        from eth.presentation import LABELS
        for k in LABELS:
            p=payload(self.s,k,plan=self.p,now=self.now,reason='測試'*2000)
            embed=p['embeds'][0]
            total=sum(len(f['name'])+len(f['value']) for f in embed['fields'])+sum(len(embed.get(x,'')) for x in ('title','description'))+len(embed['footer']['text'])
            self.assertLess(total,6000);self.assertLessEqual(len(embed['fields']),25)
            self.assertTrue(all(len(f['value'])<=1024 for f in embed['fields']))
    def test_no_old_price_called_live(self):
        self.s['quote']['observed_at']=self.now-200
        self.assertNotIn('4,000.00 USDT',self.text('signal'))
    def test_event_context_is_immutable(self):
        e=event(self.s,'signal','id',plan=self.p,now=self.now)
        before=e['payload']['_context']['plan']['entries'][0]['stop']
        self.p['entries'][0]['stop']=1
        self.assertEqual(e['payload']['_context']['plan']['entries'][0]['stop'],before)
    def test_event_ids_preserve_routing(self):
        e=event(self.s,'structure_break','break',plan=self.p,now=self.now)
        self.assertEqual(e['priority'],100);self.assertEqual(e['route'],'strategy')


class StoreDeliveryRevisionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.db=DB(self.tmp.name);self.store=Store(self.db)
        self.store.save('discord',{'enabled':True,'hourly':'123456789012345','strategy':'123456789012346'})
    def tearDown(self):self.db.conn().close();self.tmp.cleanup()
    def test_one_leg_stop_does_not_retire_other_leg_entry(self):
        n=time.time();s=snapshot(n);p=s['plans']['long'];p['id']='plan'
        self.store.enqueue(event(s,'entry_confirmed','leg1',plan=p,stage=1,now=n),n)
        self.store.enqueue(event(s,'entry_confirmed','leg2',plan=p,stage=2,now=n),n)
        self.store.publish(s,p,[event(s,'leg_stop','stop',plan=p,stage=1,now=n)],n)
        self.assertEqual(self.db.row("SELECT status FROM eth_events WHERE id='leg1'")['status'],'expired')
        self.assertEqual(self.db.row("SELECT status FROM eth_events WHERE id='leg2'")['status'],'pending')
    def test_sending_uses_latest_price(self):
        n=time.time();s=snapshot(n);self.store.enqueue(event(s,'hourly','ev',now=n),n);sent=[]
        Discord(self.store,Vault(),lambda *a,**k:(sent.append(k['json']) or Response(200,{'id':'ok'})),
                quote_getter=lambda:{'ok':True,'last':4001,'observed_at':time.time()}).deliver_one()
        self.assertIn('4,001.00 USDT',json.dumps(sent,ensure_ascii=False))
    def test_structure_alert_survives_quote_failure(self):
        n=time.time();s=snapshot(n);self.store.enqueue(event(s,'structure_break','ev',now=n),n)
        Discord(self.store,Vault(),lambda *a,**k:Response(200,{'id':'ok'}),quote_getter=lambda:{'ok':False}).deliver_one()
        self.assertEqual(self.store.events()[0]['status'],'sent')
    def test_changed_route_detailed_test_report(self):
        n=time.time();s=snapshot(n);e=event(s,'test','ev',now=n);e['route']='hourly';self.store.enqueue(e,n);sent=[]
        Discord(self.store,Vault(),lambda *a,**k:(sent.append(k['json']) or Response(200,{'id':'ok'}))).deliver_one()
        self.assertIn('來源狀態',json.dumps(sent,ensure_ascii=False))
    def test_cancelled_active_plan_cannot_send_old_entry(self):
        n=time.time();s=snapshot(n);p=s['plans']['long'];p['id']='old'
        self.store.enqueue(event(s,'signal','ev',plan=p,now=n),n)
        self.store.save('snapshot',{'as_of':n+1});self.store.save('active',None);sent=[]
        Discord(self.store,Vault(),lambda *a,**k:(sent.append(1) or Response(200)),quote_getter=lambda:{'ok':True,'last':4000,'observed_at':time.time()}).deliver_one()
        self.assertFalse(sent);self.assertEqual(self.store.events()[0]['status'],'expired')
    def test_migration_preserves_login_and_keys(self):
        self.db.set_setting('admin_password_hash','same');self.store.save('config',{'min_fitness':72})
        Store(self.db)
        self.assertEqual(self.db.get_setting('admin_password_hash'),'same');self.assertEqual(self.store.state('config')['min_fitness'],72)


class HistoricalDataTests(unittest.TestCase):
    def test_selected_candle_counts(self):self.assertEqual(CANDLE_COUNTS,{'5m':360,'30m':720,'4h':540})
    def test_request_counts_include_one_open_candle(self):
        with tempfile.TemporaryDirectory() as d:
            p=Providers(DB(d),Vault());calls=[]
            def gate(endpoint,**kw):
                calls.append(kw.get('params',{}));return {'ok':True,'at':time.time(),'data':{} if 'contracts/' in endpoint or 'order_book' in endpoint else []}
            p.gate=gate;p.market()
            limits={x['interval']:x['limit'] for x in calls if 'interval' in x}
            self.assertEqual(limits,{'5m':361,'30m':721,'4h':541})
    def test_realtime_api_fix_is_retained(self):self.assertIn('dataset: realtime',BITQUERY);self.assertNotIn('dataset: combined',BITQUERY)
    def test_history_gap_older_than_recent_80_detected(self):
        b=bars(1800,count=240);b.pop(12)
        self.assertIn('缺根',history_error(b,1800,at('2026-09-17T12:00:15')))
    def test_full_history_analysis_produces_modes(self):
        n=at('2026-09-17T12:00:15');b={f:bars(sec,n,CANDLE_COUNTS[f]) for f,sec in [('5m',300),('30m',1800),('4h',14400)]}
        s=evaluate({'bars':b,'tick':.01,'quote':{'last':4000,'bid':3999.9,'ask':4000.1,'observed_at':n}}, {},[],n)
        self.assertIn('structure_states',s);self.assertEqual(s['history_counts']['30m'],720)
        self.assertIsNotNone(s['direction_score'])
    def test_open_or_future_last_bar_blocks_evaluation(self):
        n=at('2026-09-17T12:00:15');b={f:bars(sec,n,CANDLE_COUNTS[f]) for f,sec in [('5m',300),('30m',1800),('4h',14400)]}
        b['30m'][-1]['close_time']+=1800
        s=evaluate({'bars':b,'quote':{},'tick':.01},{},[],n)
        self.assertFalse(s['eligible']);self.assertFalse(s['entry_checks_ok'])

if __name__=='__main__':unittest.main()
