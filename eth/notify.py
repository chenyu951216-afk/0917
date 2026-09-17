"""Two explicit Discord destinations, bounded retry and transparent alert payloads."""
from __future__ import annotations

import hashlib
import json
import os
import time
import threading

import requests

from .strategy import local_time, num

API = "https://discord.com/api/v10"
NAMES = {"hourly": "eth-整點多空", "strategy": "eth-策略訊號"}
LABELS = {"hourly": "整點偏向報告", "signal": "策略計畫", "reversal": "反方向訊號／撤銷原計畫",
          "invalidation": "結構失效／風控警示", "opposition": "短線反向警戒", "expired": "計畫到期", "resume": "第二筆條件重新確認", "test": "通知測試"}


from .presentation import price, level_text, payload


class Discord:
    def __init__(self, store, secrets_store, transport=None, *, quote_getter=None):
        self.store, self.secrets = store, secrets_store
        self.transport = transport or requests.request
        self.channel_lock = threading.RLock()
        self.quote_getter = quote_getter

    def token(self):
        return self.secrets.get("discord", "bot_token") or os.environ.get("DISCORD_BOT_TOKEN", "")

    def call(self, method, path, body=None):
        if not path.startswith(("/channels/", "/guilds/")):
            raise ValueError("unsupported Discord path")
        token = self.token()
        if not token:
            raise ValueError("尚無 Discord Bot Token")
        return self.transport(method, API + path, headers={"Authorization": "Bot " + token,
                              "Content-Type": "application/json"}, json=body,
                              timeout=(4, 16), allow_redirects=False)

    def ensure_channels(self):
        with self.channel_lock:
            return self._ensure_channels()

    def _ensure_channels(self):
        cfg = self.store.state("discord", {}) or {}
        if cfg.get("hourly") and cfg.get("strategy"):
            if cfg["hourly"] == cfg["strategy"]:
                raise ValueError("整點與策略必須是兩個不同頻道")
            return cfg
        if not cfg.get("auto_create", True):
            return cfg
        anchor = cfg.get("anchor") or cfg.get("strategy") or cfg.get("hourly")
        if not self.token() or not anchor:
            raise ValueError("請設定 Bot Token 與一個原有頻道 ID，才能辨識你的伺服器並建立雙頻道")
        r = self.call("GET", "/channels/" + str(anchor))
        if r.status_code != 200:
            raise ValueError(f"原有頻道讀取失敗 HTTP {r.status_code}")
        original = r.json()
        guild = str(original.get("guild_id") or "")
        if not guild.isdigit():
            raise ValueError("原有頻道不屬於伺服器")
        r = self.call("GET", f"/guilds/{guild}/channels")
        if r.status_code != 200:
            raise ValueError(f"頻道清單讀取失敗 HTTP {r.status_code}")
        channels = r.json()
        for route, name in NAMES.items():
            if cfg.get(route):
                continue
            # Do not reuse a matching name from another category or with different
            # privacy permissions. Never broaden who can see the trading alerts.
            existing = next((c for c in channels if c.get("name") == name and c.get("type") == 0
                             and c.get("parent_id") == original.get("parent_id")
                             and c.get("permission_overwrites", []) == original.get("permission_overwrites", [])), None)
            if existing is None:
                r = self.call("POST", f"/guilds/{guild}/channels", {
                    "name": name, "type": 0, "parent_id": original.get("parent_id"),
                    "permission_overwrites": original.get("permission_overwrites", []),
                    "topic": "ETH 專屬｜" + ("每整點多空、盤勢、支撐壓力" if route == "hourly" else "策略進出場、反向訊號與結構失效"),
                })
                if r.status_code not in (200, 201):
                    self.store.save("discord", cfg)
                    raise ValueError(f"建立 {name} 失敗 HTTP {r.status_code}；Bot 需管理頻道權限，也可在網頁指定兩個現有頻道")
                existing = r.json()
            cfg[route] = str(existing["id"])
            cfg["guild"] = guild
            self.store.save("discord", cfg)  # persist each successful creation
        return cfg

    def deliver_one(self):
        cfg = self.store.state('discord', {}) or {}
        if not cfg.get('enabled', True):
            return False
        event = self.store.claim(time.time())
        if not event:
            return False
        destination = cfg.get(event['route'])
        if not destination or not self.token():
            self.store.delivery(event['id'], blocked=True, retry=120, error='缺少對應Discord頻道／Bot Token，未假裝送達')
            return True
        body = json.loads(event['body'])
        context = body.pop('_context', None)
        if not context and self.quote_getter:
            context = {'snapshot': self.store.state('snapshot', {}) or {}, 'plan': None, 'reason': '通知格式更新，使用最新分析快照。'}
        if context:
            snapshot = dict(context.get('snapshot') or {})
            latest = self.store.state('snapshot', {}) or {}
            if event['kind'] in {'hourly', 'test'} and latest.get('as_of', 0) > snapshot.get('as_of', 0):
                snapshot = latest
            quote = None
            if self.quote_getter:
                try:
                    quote = self.quote_getter()
                except Exception:
                    quote = {'ok': False}
                snapshot = {**snapshot, 'delivery_quote': quote or {'ok': False}}
            plan, stage = context.get('plan'), context.get('stage')
            if event['kind'] in {'signal', 'resume', 'entry_confirmed'} and self.quote_getter:
                fresh = quote and quote.get('ok') and (num(quote.get('last')) or 0) > 0 and -5 <= time.time()-quote.get('observed_at', 0) <= 30
                if not fresh:
                    self.store.delivery(event['id'], retry=30, error='進場通知等待有效Gate現價；風控不受此限制')
                    return True
                sign = 1 if (plan or {}).get('side') == 'long' else -1
                legs = [e for e in (plan or {}).get('entries', []) if e.get('enabled', True) and e.get('route_enabled', True) and e.get('signal_state') not in {'cancelled', 'missed'}]
                if event['kind'] == 'entry_confirmed':
                    legs = [e for e in legs if e.get('signal_state') == 'entry_confirmed' and (stage is None or e['stage'] == stage)]
                # Recheck the actual active plan, not just its now-obsolete queued copy.
                actual = self.store.state('active')
                if plan and plan.get('id') and latest.get('as_of', 0) >= event['created']:
                    if not actual or actual.get('id') != plan['id']:
                        legs = []
                    else:
                        active_by_stage = {e['stage']: e for e in actual.get('entries', [])}
                        legs = [e for e in legs if active_by_stage.get(e['stage'], {}).get('signal_state') not in {'cancelled', 'missed'}]
                def geometry(e):
                    return sign*(quote['last']-e['stop']) > 0 and bool(e.get('targets')) and sign*(quote['last']-e['targets'][0]['price']) < 0
                if plan and plan.get('risk_model') == 'independent_structure_legs':
                    viable = any(geometry(e) for e in legs)
                else:
                    viable = not plan or (sign*(quote['last']-plan['stop']) > 0 and
                                          (not plan.get('targets') or sign*(quote['last']-plan['targets'][0]['price']) < 0))
                if viable and event['kind'] == 'entry_confirmed' and plan:
                    from .execution import _permitted
                    cost = plan.get('cost_pct', .16)/100
                    def executable(e):
                        if not geometry(e):
                            return False
                        if latest.get('as_of', 0) >= snapshot.get('as_of', 0) and not _permitted(latest, plan['side'], e['stage']):
                            return False
                        last = quote['last']
                        risk = sign*(last-e['stop'])+last*cost
                        reward = sum(t['quantity_fraction']*sign*(t['price']-last) for t in e.get('targets', []))-last*cost
                        near = -(e['high']-e['low']) <= sign*(last-e['price']) <= e.get('max_chase', 0)
                        return near and risk > 0 and reward/risk >= plan.get('min_net_rr', 1.0)
                    viable = any(executable(e) for e in legs)
                if not viable:
                    self._expire(event, plan, stage)
                    return True
            body = payload(snapshot, event['kind'], plan=plan, reason=context.get('reason', ''), now=time.time(), stage=stage,
                           detailed=event['route'] == 'hourly')
        body.update(nonce=hashlib.sha256(event['id'].encode()).hexdigest()[:24], enforce_nonce=True)
        try:
            r = self.call('POST', f"/channels/{destination}/messages", body)
            if 200 <= r.status_code < 300:
                self.store.delivery(event['id'], ok=True, message_id=str(r.json().get('id', '')))
            elif r.status_code == 429:
                try:
                    retry = float(r.json().get('retry_after', 30))
                except (ValueError, TypeError):
                    retry = 30
                self.store.delivery(event['id'], retry=max(1, retry), error='Discord限流，依retry_after重試')
            else:
                blocked = r.status_code in (401, 403, 404)
                self.store.delivery(event['id'], blocked=blocked, retry=300 if blocked else 30,
                                    error=f'Discord HTTP {r.status_code}；檢查Bot查看／傳送／嵌入權限')
        except Exception as exc:
            self.store.delivery(event['id'], retry=min(300, 10*2**min(event['attempts'], 4)), error='Discord '+type(exc).__name__)
        return True

    def _expire(self, event, plan, stage):
        from .store import dump
        with self.store.db.tx() as c:
            c.execute("UPDATE eth_events SET status='expired',error='發送前計畫已撤銷、超過SL／TP、追價或成本後R不足；不補發入場' WHERE id=?", (event['id'],))
            if not plan or not plan.get('id'):
                return
            row = c.execute("SELECT value FROM settings WHERE key='eth:active'").fetchone()
            actual = json.loads(row[0]) if row else None
            if actual and actual.get('id') == plan['id']:
                for e in actual.get('entries', []):
                    if (stage is None or e['stage'] == stage) and e.get('signal_state') not in {'cancelled', 'missed'}:
                        e['signal_state'] = 'missed'
                        e['delivery_note'] = '發送時已不可執行，未通知進場'
                c.execute("UPDATE settings SET value=? WHERE key='eth:active'", (dump(actual),))
