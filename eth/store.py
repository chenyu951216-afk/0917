"""Durable ETH state, migrations, outbox and single-leader scheduling leases."""
from __future__ import annotations

import hashlib
import json
import os
import time

from .strategy import DEFAULTS, VERSION

ALLOWED_SECRETS = {"nansen", "birdeye", "bitquery", "gate", "gateio", "discord"}


def dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


class Store:
    def __init__(self, db):
        self.db = db
        with db.tx() as c:
            c.executescript('''
              CREATE TABLE IF NOT EXISTS eth_events(
                id TEXT PRIMARY KEY, created REAL NOT NULL, route TEXT NOT NULL,
                kind TEXT NOT NULL, body TEXT NOT NULL, priority INTEGER NOT NULL,
                expires REAL NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0, next_try REAL NOT NULL DEFAULT 0,
                claimed REAL, sent REAL, error TEXT, message_id TEXT
              );
              CREATE INDEX IF NOT EXISTS eth_outbox_due ON eth_events(status,next_try,priority);
              CREATE TABLE IF NOT EXISTS eth_lease(name TEXT PRIMARY KEY,owner TEXT NOT NULL,expires REAL NOT NULL);
              CREATE TABLE IF NOT EXISTS eth_observations(
                id TEXT PRIMARY KEY, at REAL NOT NULL,provider TEXT NOT NULL,value REAL NOT NULL
              );
              CREATE INDEX IF NOT EXISTS eth_observations_at ON eth_observations(at);
            ''')
        with db.tx() as c:
            columns = {r[1] for r in c.execute('PRAGMA table_info(eth_events)')}
            for key, typ in (('plan_id', 'TEXT'), ('stage', 'INTEGER')):
                if key not in columns:
                    c.execute(f'ALTER TABLE eth_events ADD COLUMN {key} {typ}')
        self.migrate()
        self.migrate_r4()

    def migrate(self):
        if self.db.get_setting("eth:migrated"):
            return
        # All historical analyses/positions remain in SQLite, but obsolete API
        # credentials, plans, scheduling caches and queued altcoin notifications do not.
        legacy_routes = self.db.get_setting("discord_routes_v25", {}) or {}
        channels = []
        if isinstance(legacy_routes, dict):
            for values in legacy_routes.values():
                if isinstance(values, str):
                    values = [values]
                for value in values or []:
                    value = str(value)
                    if value.isdigit() and value not in channels:
                        channels.append(value)
        anchor = str(self.db.get_setting("discord_channel_id_v24") or os.environ.get("DISCORD_CHANNEL_ID") or (channels[0] if channels else ""))
        with self.db.tx() as c:
            placeholders = ','.join('?' for _ in ALLOWED_SECRETS)
            removed = c.execute(f"DELETE FROM secrets WHERE provider NOT IN ({placeholders})", tuple(ALLOWED_SECRETS)).rowcount
            forbidden = ("coinglass", "arkham", "helius", "goplus", "dexscreener", "dextools", "moralis", "alchemy", "santiment", "glassnode", "etherscan", "coingecko")
            for row in c.execute("SELECT key FROM settings WHERE key NOT LIKE 'eth:%'").fetchall():
                key = row[0]
                if any(p in key.lower() for p in forbidden):
                    c.execute("DELETE FROM settings WHERE key=?", (key,))
            if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='discord_signal_outbox'").fetchone():
                c.execute("UPDATE discord_signal_outbox SET status='failed',last_error='ETH migration: legacy scanner retired' WHERE status IN ('pending','retry','sending')")
            # Old non-ETH watch entries remain as records, not active subscriptions.
            c.execute("UPDATE watchlist SET enabled=0 WHERE UPPER(COALESCE(symbol,'')) NOT IN ('ETH','ETH_USDT','ETHUSDT','WETH')")
            values = {"eth:config": DEFAULTS, "eth:discord": {"enabled": True, "anchor": anchor,
                       "hourly": "", "strategy": anchor, "auto_create": True},
                      "eth:migrated": {"version": VERSION, "at": time.time(), "removed_secret_rows": removed},
                      "scheduler_enabled": False}
            for key, value in values.items():
                c.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (key, dump(value)))
            c.execute("UPDATE settings SET value='false' WHERE key='scheduler_enabled'")

    def migrate_r4(self):
        if self.db.get_setting('eth:structure_release') == VERSION:
            return
        with self.db.tx() as c:
            c.execute("UPDATE eth_events SET status='expired',error='R4模型更新：舊入場通知停止沿用' WHERE kind IN ('signal','resume','entry_confirmed') AND status IN ('pending','retry','blocked')")
            c.execute("DELETE FROM settings WHERE key='eth:main_slot'")
            c.execute("INSERT INTO settings(key,value) VALUES('eth:structure_release',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (dump(VERSION),))

    def leader(self, owner, now):
        with self.db.tx() as c:
            cur = c.execute('''INSERT INTO eth_lease(name,owner,expires) VALUES('runtime',?,?)
                  ON CONFLICT(name) DO UPDATE SET owner=excluded.owner,expires=excluded.expires
                  WHERE eth_lease.expires<? OR eth_lease.owner=?''', (owner, now+90, now, owner))
            return cur.rowcount == 1

    def state(self, key, default=None):
        return self.db.get_setting("eth:" + key, default)

    def save(self, key, value):
        self.db.set_setting("eth:" + key, value)

    def observe(self, provider, value, at):
        if value is None or not at:
            return
        # Duplicate fetches and cached responses have the same observation identity.
        ident = hashlib.sha256(f"{provider}:{int(at)}:{value}".encode()).hexdigest()
        with self.db.tx() as c:
            c.execute("INSERT OR IGNORE INTO eth_observations(id,at,provider,value) VALUES(?,?,?,?)", (ident, at, provider, value))
            c.execute("DELETE FROM eth_observations WHERE at<?", (time.time()-30*86400,))

    def history(self, now):
        return self.db.rows("SELECT at,provider,value FROM eth_observations WHERE at<=? ORDER BY at DESC LIMIT 500", (now,))[::-1]

    @staticmethod
    def insert_event(c, event, now):
        c.execute("""INSERT OR IGNORE INTO eth_events(id,created,route,kind,body,priority,expires,next_try,plan_id,stage)
                     VALUES(?,?,?,?,?,?,?,?,?,?)""", (event['id'], now, event['route'], event['kind'], dump(event['payload']),
                                                    event.get('priority',50), event.get('expires',now+900), now,
                                                    event.get('plan_id'), event.get('stage')))

    def publish(self, snapshot, active, events, now):
        """State and notifications are atomic; one leg stop does not cancel another leg."""
        with self.db.tx() as c:
            for e in events:
                if e['kind'] not in {'reversal','invalidation','opposition','expired','recalibration','leg_stop'}:
                    continue
                query = "UPDATE eth_events SET status='expired',error='新風控通知取代舊入場' WHERE kind IN ('signal','resume','entry_confirmed') AND status IN ('pending','retry','blocked')"
                args = []
                if e.get('plan_id'):
                    query += ' AND (plan_id=? OR plan_id IS NULL)'
                    args.append(e['plan_id'])
                if e.get('stage') is not None:
                    query += ' AND (stage=? OR stage IS NULL)'
                    args.append(e['stage'])
                c.execute(query, args)
            for key, value in (('snapshot',snapshot), ('active',active)):
                c.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=CURRENT_TIMESTAMP", ('eth:'+key,dump(value)))
            for e in events:
                self.insert_event(c,e,now)

    def enqueue(self, event, now=None):
        with self.db.tx() as c:
            self.insert_event(c, event, now or time.time())

    def claim(self, now):
        with self.db.tx() as c:
            c.execute("UPDATE eth_events SET status='expired',error='通知有效期已過，不補發過期入場指令' WHERE expires<? AND status NOT IN ('sent','expired')", (now,))
            c.execute("UPDATE eth_events SET status='retry' WHERE status='sending' AND claimed<?", (now-90,))
            row = c.execute("SELECT * FROM eth_events WHERE status IN ('pending','retry','blocked') AND next_try<=? ORDER BY priority DESC,created LIMIT 1", (now,)).fetchone()
            if not row:
                return None
            changed = c.execute("UPDATE eth_events SET status='sending',claimed=?,attempts=attempts+1 WHERE id=? AND status IN ('pending','retry','blocked')", (now, row["id"]))
            return dict(row) if changed.rowcount else None

    def delivery(self, ident, *, ok=False, error=None, retry=30, message_id=None, blocked=False):
        now = time.time()
        with self.db.tx() as c:
            c.execute("UPDATE eth_events SET status=?,sent=?,next_try=?,error=?,message_id=? WHERE id=?", (
                "sent" if ok else "blocked" if blocked else "retry", now if ok else None,
                now+retry, error, message_id, ident))

    def events(self, limit=40):
        return self.db.rows("SELECT id,created,route,kind,status,attempts,sent,error FROM eth_events ORDER BY created DESC LIMIT ?", (limit,))

    def cleanup(self, now):
        with self.db.tx() as c:
            c.execute("DELETE FROM eth_events WHERE created<? AND status IN ('sent','expired')", (now-90*86400,))
            c.execute("DELETE FROM settings WHERE key LIKE 'eth:cache:%' AND updated_at<datetime('now','-2 days')")
