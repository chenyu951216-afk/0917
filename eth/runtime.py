"""Main scans 5/15m; independent closed-5m risk checks; hourly reports; durable DC."""
from __future__ import annotations

import copy
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from .notify import payload
from .strategy import DEFAULTS, GRACE, VERSION, evaluate, interval_at, next_scan, risk_event, scan_slot


def event(snapshot, kind, ident, *, plan=None, reason="", now=None, stage=None):
    now = now or time.time()
    urgent = kind in {"reversal", "invalidation", "opposition", "leg_stop", "bias_change", "structure_break"}
    # Capture immutable context. Later leg mutations cannot rewrite an earlier alert.
    slim = {k: v for k, v in snapshot.items() if k not in {"structure_chart", "structure_charts"}}
    context = copy.deepcopy({"snapshot": slim, "plan": plan, "reason": reason, "stage": stage})
    body = payload(context["snapshot"], kind, plan=context["plan"], reason=reason, now=now, stage=stage)
    body["_context"] = context
    return {"id": ident, "route": "hourly" if kind == "hourly" else "strategy", "kind": kind,
            "plan_id": (plan or {}).get("id"), "stage": stage,
            "priority": 100 if urgent else 10 if kind == "hourly" else 50,
            "expires": now+(21600 if urgent else 3600 if kind == "hourly" else 900), "payload": body}


def transition(previous, active, snapshot, bars5, now, *, main=True):
    from .execution import transition as advance
    return advance(previous, active, snapshot, bars5, now, main, event)


class Runtime:
    def __init__(self, store, providers, discord):
        self.store, self.providers, self.discord = store, providers, discord
        self.owner = uuid.uuid4().hex
        self.stop_event = threading.Event()
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="eth-analysis")
        self.future = None
        self.submit_lock = threading.Lock()
        self.threads = []
        self.is_leader = False
        self.last_heartbeat = 0
        self.last_provision = 0

    def status(self):
        now = time.time()
        return {"version": VERSION, "running": bool(self.threads and self.threads[0].is_alive()),
                "leader": self.is_leader, "busy": bool(self.future and not self.future.done()),
                "heartbeat": self.last_heartbeat, "main_interval_minutes": interval_at(now)//60,
                "next_main_scan": next_scan(now), "risk_interval_minutes": 5,
                "last_main_scan": self.store.state("last_main_scan"),
                "last_risk_scan": self.store.state("last_risk_scan"),
                "last_error": self.store.state("runtime_error"),
                "channel_error": self.store.state("channel_error")}

    def submit(self, main=True):
        with self.submit_lock:
            if self.future and not self.future.done():
                return False
            self.future = self.pool.submit(self.scan, main)
            return True

    def scan(self, main=True):
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                fm = pool.submit(self.providers.market)
                ff = pool.submit(self.providers.feeds, refresh=main)
                market, feeds = fm.result(), ff.result()
            now = time.time()
            cfg = {**DEFAULTS, **(self.store.state("config", {}) or {})}
            history = self.store.history(now)
            snapshot = evaluate(market, feeds, history, now, cfg)
            if not cfg.get("enabled", True):
                snapshot["eligible"] = False
                snapshot["reasons"].append("新策略計畫已暫停；整點報告及既有計畫風控通知仍啟用")
            prior = self.store.state("snapshot", {}) or {}
            snapshot["scan_mode"] = "完整策略掃描" if main else "已收 K 反向／結構快查"
            snapshot["full_scan_at"] = now if main else prior.get("full_scan_at")
            snapshot["market_errors"] = market.get("errors", {})
            active, events = transition(prior, self.store.state("active"), snapshot, market["bars"].get("5m", []), now, main=main)
            self.store.publish(snapshot, active, events, now)
            nansen = feeds.get("nansen", {})
            if nansen.get("ok") and not nansen.get("stale"):
                self.store.observe("nansen", nansen.get("smart_net_usd"), nansen.get("observed_at"))
            dex = snapshot.get("evidence", {}).get("dex_group")
            if dex is not None:
                stamps = [f.get("observed_at", 0) for p, f in feeds.items() if p in {"birdeye", "bitquery"} and f.get("ok") and not f.get("stale")]
                self.store.observe("dex", dex, min(stamps) if stamps else 0)
            self.store.save("last_main_scan" if main else "last_risk_scan", now)
            self.store.save("runtime_error", None)
            return snapshot
        except Exception as exc:
            self.store.save("runtime_error", {"at": time.time(), "type": type(exc).__name__, "message": "本輪失敗；保留舊快照並標示過期，下一輪重試。"})
            return None

    def hourly(self, now):
        hour = int(now)//3600*3600
        if now - hour < 45 or self.store.state("hourly_slot") == hour:
            return
        snapshot = self.store.state("snapshot", {}) or {}
        # Give the concurrently running main scan a short opportunity to finish.
        if snapshot.get("as_of", 0) < hour and now-hour < 100 and self.future and not self.future.done():
            return
        reason = "即使沒有交易訊號也固定報告。"
        if now-hour > 180:
            reason += " 本次為服務啟動／恢復後本小時補報，請以實際製表時間為準。"
        self.store.enqueue(event(snapshot, "hourly", f"hourly:{hour}", reason=reason, now=now), now)
        self.store.save("hourly_slot", hour)

    def tick(self, now=None):
        now = now or time.time()
        self.is_leader = self.store.leader(self.owner, now)
        if not self.is_leader:
            return
        self.last_heartbeat = now
        main_slot = scan_slot(now)
        risk_slot = (int(now)-GRACE)//300*300
        if self.store.state("main_slot") != main_slot:
            if self.submit(True):
                self.store.save("main_slot", main_slot)
                self.store.save("risk_slot", risk_slot)
        elif self.store.state("risk_slot") != risk_slot:
            if self.submit(False):
                self.store.save("risk_slot", risk_slot)
        self.hourly(now)
        day = int(now)//86400
        if self.store.state("clean_day") != day:
            self.store.cleanup(now)
            self.store.save("clean_day", day)

    def _schedule_loop(self):
        while not self.stop_event.is_set():
            try:
                self.tick()
            except Exception as exc:
                self.store.save("runtime_error", {"at": time.time(), "type": type(exc).__name__, "message": "排程錯誤，下次心跳重試"})
            self.stop_event.wait(3)

    def _discord_loop(self):
        while not self.stop_event.is_set():
            try:
                if self.is_leader and time.time()-self.last_heartbeat < 30:
                    if time.time()-self.last_provision > 600:
                        self.last_provision = time.time()
                        try:
                            self.discord.ensure_channels()
                            self.store.save("channel_error", None)
                        except Exception as exc:
                            self.store.save("channel_error", {"at": time.time(), "error": str(exc) if isinstance(exc, ValueError) else type(exc).__name__})
                    self.discord.deliver_one()
            except Exception as exc:
                self.store.save("delivery_worker_error", {"at": time.time(), "error": type(exc).__name__})
            self.stop_event.wait(1)

    def start(self):
        if self.threads or os.environ.get("ETH_DISABLE_RUNTIME") == "1":
            return
        for target in (self._schedule_loop, self._discord_loop):
            thread = threading.Thread(target=target, daemon=True)
            self.threads.append(thread)
            thread.start()

    def stop(self):
        self.stop_event.set()
        self.pool.shutdown(wait=False, cancel_futures=True)
