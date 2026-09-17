"""Strict allowlist. No discovery/screener, no paid fallback and no trading calls."""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests

from .strategy import SYMBOL, closed_candles, num
from .http_errors import diagnose, sanitize

CANDLE_COUNTS = {"5m": 360, "30m": 720, "4h": 540}

WETH = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
STABLES = ["0xA0b86991c6218b36c1d19d4a2e9eb0ce3606eb48", "0xdAC17F958D2ee523a2206206994597C13D831ec7"]
ALLOWED = {
    "gate": {"api.gateio.ws", "fx-api.gateio.ws"},
    "nansen": {"api.nansen.ai"},
    "birdeye": {"public-api.birdeye.so"},
    "bitquery": {"streaming.bitquery.io"},
}
PAID = ("nansen", "birdeye", "bitquery")
# EVM DEXTrades is POOL-relative: pool Sell=WETH means trader BUYS WETH.
# Sum the stablecoin leg, not both legs; do not count a transfer as a swap.
BITQUERY = '''query EthFlow($since: DateTime!, $till: DateTime!, $weth: String!, $stables: [String!]!) {
  EVM(network: eth, dataset: realtime) {
    trader_buys: DEXTrades(limit: {count: 1}, where: {Block: {Time: {since: $since, till: $till}}, TransactionStatus: {Success: true}, Trade: {Sell: {Currency: {SmartContract: {is: $weth}}}, Buy: {Currency: {SmartContract: {in: $stables}}}}}) {
      amount: sum(of: Trade_Buy_Amount)
      count
      Block { Time(maximum: Block_Time) }
    }
    trader_sells: DEXTrades(limit: {count: 1}, where: {Block: {Time: {since: $since, till: $till}}, TransactionStatus: {Success: true}, Trade: {Buy: {Currency: {SmartContract: {is: $weth}}}, Sell: {Currency: {SmartContract: {in: $stables}}}}}) {
      amount: sum(of: Trade_Sell_Amount)
      count
      Block { Time(maximum: Block_Time) }
    }
  }
}'''


def first(data):
    return data[0] if isinstance(data, list) and data and isinstance(data[0], dict) else data if isinstance(data, dict) else {}


def imbalance(buy, sell):
    buy, sell = num(buy), num(sell)
    return (buy - sell) / (buy + sell) if buy is not None and sell is not None and min(buy, sell) >= 0 and buy + sell > 0 else None


def timestamp(value):
    if num(value) is not None:
        return float(value)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError, OverflowError):
        return None


def parse_feed(provider, response, now):
    observed = response.get("at", 0)
    result = {"provider": provider, "ok": False, "observed_at": observed,
              "cached": bool(response.get("cached")), "stale": False,
              "error": response.get("error"), "diagnostic": response.get("diagnostic"),
              "dataset": "realtime" if provider == "bitquery" else None, "scope": "Ethereum 主網 WETH；不是全部 ETH 市場"}
    if not response.get("ok"):
        return result
    raw = response.get("data") or {}
    if not isinstance(raw, dict):
        return {**result, "error": "API 回應不是物件，未計分"}
    if provider == "nansen":
        item = first(raw.get("data"))
        net = num(item.get("smart_trader_net_flow_usd"))
        if net is None:
            net = num(item.get("top_pnl_net_flow_usd"))
        result.update(smart_net_usd=net, exchange_net_usd=num(item.get("exchange_net_flow_usd")),
                      whale_net_usd=num(item.get("whale_net_flow_usd")),
                      wallets=num(item.get("smart_trader_wallet_count")),
                      window="6h", upstream_time=None,
                      note="標籤地址持有流向，不等於已成交買賣；上游快取約 10–30 分鐘")
        result["ok"] = net is not None
        result["stale"] = now - observed > 3700
    elif provider == "birdeye":
        item = first(raw.get("data"))
        buy, sell = num(item.get("vBuy1hUSD")), num(item.get("vSell1hUSD"))
        latest = timestamp(item.get("lastTradeUnixTime"))
        result.update(buy_usd=buy, sell_usd=sell, imbalance=imbalance(buy, sell),
                      liquidity_usd=num(item.get("liquidity")), upstream_time=latest, window="1h",
                      note="DEX 滾動成交；與 Bitquery 歸為同一組證據，避免重複加分")
        result["ok"] = raw.get("success") is not False and result["imbalance"] is not None
        result["stale"] = now - observed > 1000 or latest is None or not -30 <= now - latest <= 1200
    else:
        evm = (raw.get("data") or {}).get("EVM") or {}
        buys, sells = first(evm.get("trader_buys")), first(evm.get("trader_sells"))
        b, s = num(buys.get("amount")), num(sells.get("amount"))
        if b is None and num(buys.get("count")) == 0:
            b = 0.0
        if s is None and num(sells.get("count")) == 0:
            s = 0.0
        stamps = [timestamp((x.get("Block") or {}).get("Time")) for x in (buys, sells)]
        stamps = [x for x in stamps if x is not None]
        latest = max(stamps) if stamps else None
        result.update(buy_stable_units=b, sell_stable_units=s, imbalance=imbalance(b, s),
                      upstream_time=latest, window="已結束的 1h 視窗",
                      note="WETH↔USDC/USDT 已成功兌換；穩定幣名目非精確美元估值，不涵蓋所有 ETH 路由")
        result["ok"] = not raw.get("errors") and result["imbalance"] is not None
        result["stale"] = now - observed > 1000 or latest is None or not -30 <= now - latest <= 1200
    if not result["ok"] and not result.get("error"):
        result["error"] = "HTTP 成功，但所需方向欄位缺失／視窗無有效成交"
    if result["stale"]:
        result["error"] = "來源時間過期或缺失，不參與方向計分"
    return result


class Providers:
    def __init__(self, db, secrets_store, transport=None):
        self.db, self.secrets = db, secrets_store
        self.transport = transport or requests.request
        self._locks = {}
        self._lock_guard = threading.Lock()

    def credential(self, provider):
        key = "access_token" if provider == "bitquery" else "api_key"
        value = self.secrets.get(provider, key) or os.environ.get(provider.upper() + "_" + key.upper(), "")
        if provider == "bitquery" and str(value).lower().startswith("bearer "):
            value = str(value)[7:].strip()
        return value

    def request(self, provider, endpoint, method, url, *, ttl=0, force=False, **kwargs):
        parsed = urlparse(url)
        if provider not in ALLOWED or parsed.scheme != "https" or parsed.hostname not in ALLOWED[provider]:
            raise ValueError("資料來源不在 ETH 允許清單")
        if provider == "gate" and method != "GET":
            raise ValueError("禁止交易所寫入操作")
        ident = hashlib.sha256(json.dumps([endpoint, kwargs.get("params"), kwargs.get("json")], sort_keys=True).encode()).hexdigest()[:20]
        cache_key = f"eth:cache:r2:{provider}:{ident}"
        with self._lock_guard:
            lock = self._locks.setdefault((provider, endpoint), threading.RLock())
        with lock:
            now = time.time()
            cached = self.db.get_setting(cache_key, {}) or {}
            if not force and cached.get("ok") and 0 <= now-cached.get("at",0) < ttl:
                return {**cached, "cached":True}
            backoff_key = f"eth:backoff:r2:{provider}:{endpoint}"
            blocked = self.db.get_setting(backoff_key,{}) or {}
            if now < blocked.get("until",0):
                return {"ok":False,"at":now,"error":blocked.get("error","來源退避中"),
                        "diagnostic":blocked.get("diagnostic"),"retry_at":blocked["until"]}
            status, credit, remaining, diagnostic, request_id = None, None, None, None, None
            headers = {"Accept":"application/json","User-Agent":"ETH-Strategy/4.0", **kwargs.pop("headers",{})}
            secret_values = [v for k,v in headers.items() if k.lower() in {"authorization","apikey","x-api-key"}]
            secret_values += [v.split(' ',1)[-1] for v in secret_values]
            start = time.monotonic()
            try:
                r = self.transport(method,url,timeout=(4,12),allow_redirects=False,headers=headers,**kwargs)
                status = r.status_code
                request_id = sanitize(r.headers.get("X-Request-ID") or r.headers.get("CF-Ray") or '', secret_values)
                credit = num(r.headers.get("X-Nansen-Credits-Used"))
                remaining = num(r.headers.get("X-Nansen-Credits-Remaining"))
                try:
                    data = r.json()
                except (ValueError, TypeError):
                    data = None
                failed = not 200 <= status < 300 or data is None or (isinstance(data,dict) and (data.get('errors') or data.get('success') is False))
                if failed:
                    diagnostic = diagnose(status,data,getattr(r,'text',''),secret_values)
                    diagnostic.update(request_id=request_id, endpoint=endpoint,
                                      dataset='realtime' if provider=='bitquery' else None)
                    message = f"HTTP {status} [{diagnostic['code']}]；{diagnostic['hint']}"
                    retry = num(r.headers.get('Retry-After')) or (900 if status in (401,402,403) else 90)
                    result = {"ok":False,"at":time.time(),"error":message,"diagnostic":diagnostic}
                    self.db.set_setting(backoff_key,{"until":now+max(1,retry),"error":message,"diagnostic":diagnostic})
                else:
                    result = {"ok":True,"data":data,"at":time.time()}
                    self.db.set_setting(cache_key,result)
            except Exception as exc:
                result = {"ok":False,"at":time.time(),"error":type(exc).__name__}
                self.db.set_setting(backoff_key,{"until":now+60,"error":result['error']})
            self.db.set_setting(f"eth:source:{provider}:{endpoint}",{
                "ok":result["ok"],"at":result["at"],"status":status,"error":result.get('error'),
                "diagnostic":diagnostic,"credits_used":credit,"credits_remaining":remaining,
                "latency_ms":round((time.monotonic()-start)*1000)})
            return result

    def gate(self, endpoint, *, ttl=15, params=None):
        return self.request("gate", endpoint, "GET", "https://api.gateio.ws/api/v4" + endpoint,
                            ttl=ttl, params=params or {})

    def live_quote(self):
        response = self.gate("/futures/usdt/tickers", ttl=3, params={"contract": SYMBOL})
        rows = response.get("data") if response.get("ok") else []
        item = next((r for r in rows if isinstance(r,dict) and r.get("contract")==SYMBOL),{}) if isinstance(rows,list) else {}
        last = num(item.get("last"))
        return {"last":last,"mark":num(item.get("mark_price")),"index":num(item.get("index_price")),
                "observed_at":response.get("at",0),"ok":bool(response.get("ok") and last and last>0),
                "error":response.get("error"),"venue":"Gate ETH_USDT USDT 永續"}

    def market(self, now=None):
        now = now or time.time()
        jobs = {
            "5m": ("/futures/usdt/candlesticks", 15, {"contract": SYMBOL, "interval": "5m", "limit": 361}),
            "30m": ("/futures/usdt/candlesticks", 15, {"contract": SYMBOL, "interval": "30m", "limit": 721}),
            "4h": ("/futures/usdt/candlesticks", 15, {"contract": SYMBOL, "interval": "4h", "limit": 541}),
            "ticker": ("/futures/usdt/tickers", 10, {"contract": SYMBOL}),
            "book": ("/futures/usdt/order_book", 10, {"contract": SYMBOL, "limit": 5}),
            "contract": ("/futures/usdt/contracts/" + SYMBOL, 86400, {}),
        }
        # Six bounded public GETs; duplicate identical requests share a per-request lock.
        with ThreadPoolExecutor(max_workers=6) as pool:
            pending = {k: pool.submit(self.gate, endpoint, ttl=ttl, params=params)
                       for k, (endpoint, ttl, params) in jobs.items()}
            values = {k: f.result() for k, f in pending.items()}
        tickers = values["ticker"].get("data") if values["ticker"].get("ok") else []
        item = next((x for x in tickers if isinstance(x, dict) and x.get("contract") == SYMBOL), {}) if isinstance(tickers, list) else {}
        orderbook = values["book"].get("data") or {}
        if not isinstance(orderbook, dict):
            orderbook = {}
        bids = [num(x.get("p")) for x in orderbook.get("bids", []) if isinstance(x, dict)]
        asks = [num(x.get("p")) for x in orderbook.get("asks", []) if isinstance(x, dict)]
        bids, asks = [x for x in bids if x and x > 0], [x for x in asks if x and x > 0]
        contract = values["contract"].get("data") if values["contract"].get("ok") else {}
        contract = contract if isinstance(contract, dict) else {}
        quote = {"last": num(item.get("last")), "mark": num(item.get("mark_price")),
                 "index": num(item.get("index_price")), "funding_rate": num(item.get("funding_rate")),
                 "oi_contracts": num(item.get("total_size")), "funding_interval": num(contract.get("funding_interval")),
                 "bid": max(bids) if bids else None, "ask": min(asks) if asks else None,
                 "observed_at": min(values["ticker"].get("at", 0), values["book"].get("at", 0)),
                 "venue": "Gate ETH_USDT USDT 永續（last／mark／index 分開）"}
        return {"bars": {f: closed_candles(values[f].get("data"), seconds, now)[-CANDLE_COUNTS[f]:]
                         for f, seconds in (("5m", 300), ("30m", 1800), ("4h", 14400))},
                "quote": quote, "tick": num(contract.get("order_price_round")),
                "errors": {k: v.get("error") for k, v in values.items() if not v.get("ok")}}

    def feed(self, provider, *, force=False, now=None):
        if provider not in PAID:
            raise ValueError("只允許 Nansen、Birdeye、Bitquery")
        now = now or time.time()
        key = self.credential(provider)
        if not key:
            return {"provider": provider, "ok": False, "stale": False,
                    "error": "未配置可解密的 API 憑證；網站會員不代表此 API 已獲授權", "observed_at": 0}
        if provider == "nansen":
            cfg = self.db.get_setting("eth:config", {}) or {}
            response = self.request(provider, "flow-intelligence", "POST", "https://api.nansen.ai/api/v1/tgm/flow-intelligence",
                                    ttl=cfg.get("nansen_ttl", 1800), force=force, headers={"apikey": key},
                                    json={"chain": "ethereum", "token_address": WETH, "timeframe": "6h"})
        elif provider == "birdeye":
            response = self.request(provider, "token-overview", "GET", "https://public-api.birdeye.so/defi/token_overview",
                                    ttl=285, force=force, headers={"X-API-KEY": key, "x-chain": "ethereum"},
                                    params={"address": WETH})
        else:
            end = (int(now) - 12) // 300 * 300
            stamp = lambda x: datetime.fromtimestamp(x, timezone.utc).isoformat().replace("+00:00", "Z")
            response = self.request(provider, "eth-dex-realtime-v2", "POST", "https://streaming.bitquery.io/graphql",
                                    ttl=285, force=force, headers={"Authorization": "Bearer " + key},
                                    json={"query": BITQUERY, "variables": {"since": stamp(end-3600), "till": stamp(end),
                                                                          "weth": WETH.lower(), "stables": [x.lower() for x in STABLES]}})
        result = parse_feed(provider, response, now)
        self.db.set_setting("eth:feed:" + provider, result)
        return result

    def feeds(self, *, refresh=True):
        now = time.time()
        if refresh:
            with ThreadPoolExecutor(max_workers=3) as pool:
                futures = {p: pool.submit(self.feed, p) for p in PAID}
                return {p: f.result() for p, f in futures.items()}
        result = {}
        for p in PAID:
            value = self.db.get_setting("eth:feed:" + p, {}) or {"ok": False, "error": "尚未取得來源"}
            age = now - value.get("observed_at", 0)
            result[p] = {**value, "stale": bool(value.get("stale")) or age > (3700 if p == "nansen" else 1000)}
        return result
