"""Single production entrypoint; preserves the original encrypted /data volume."""
from __future__ import annotations

import atexit
import json
import os
import secrets
import threading
import time
from functools import wraps
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, jsonify, render_template, request, session
from werkzeug.security import check_password_hash, generate_password_hash

from whale.db import DB
from whale.secrets import SecretStore
from eth.notify import Discord
from eth.providers import PAID, Providers
from eth.runtime import Runtime, event
from eth.store import Store
from eth.strategy import DEFAULTS, VERSION, num
from eth.levels import project_levels


def create_app(data_dir=None, *, start_runtime=True, transport=None):
    directory = Path(data_dir or os.environ.get("WHALE_DATA_DIR", str(Path(__file__).resolve().parent / "data")))
    directory.mkdir(parents=True, exist_ok=True)
    db = DB(str(directory))
    vault = SecretStore(db, str(directory))
    store = Store(db)
    providers = Providers(db, vault, transport)
    discord = Discord(store, vault, transport, quote_getter=providers.live_quote)
    runtime = Runtime(store, providers, discord)
    app = Flask(__name__, template_folder="templates", static_folder=None)
    secret_file = directory / "flask_session.key"
    if not secret_file.exists():
        try:
            with secret_file.open("x") as f:
                f.write(secrets.token_urlsafe(48))
            secret_file.chmod(0o600)
        except FileExistsError:
            pass
    app.secret_key = secret_file.read_text().strip()
    app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Strict",
                      SESSION_COOKIE_SECURE=os.environ.get("HTTPS_ONLY", "0") == "1", MAX_CONTENT_LENGTH=32768)
    app.extensions["eth"] = {"db": db, "store": store, "vault": vault, "providers": providers,
                              "discord": discord, "runtime": runtime}
    setup_lock = threading.Lock()

    def auth_required(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if not session.get("auth"):
                return jsonify(ok=False, error="請先登入"), 401
            return fn(*args, **kwargs)
        return wrapper

    @app.before_request
    def protect_writes():
        if request.method in {"POST", "PUT", "DELETE", "PATCH"}:
            origin = request.headers.get("Origin")
            if origin and urlparse(origin).netloc != request.host:
                return jsonify(ok=False, error="拒絕跨站寫入"), 403
            if request.path not in {"/api/setup", "/api/login"}:
                expected = session.get("csrf")
                if not expected or not secrets.compare_digest(str(expected), request.headers.get("X-CSRF-Token", "")):
                    return jsonify(ok=False, error="驗證已過期，請重新整理網頁"), 403
            if not request.is_json:
                return jsonify(ok=False, error="請使用 JSON"), 415
            if not isinstance(request.get_json(silent=True), dict):
                return jsonify(ok=False, error="JSON 內容必須是物件"), 400

    @app.after_request
    def secure_headers(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'"
        return response

    @app.get("/")
    def index():
        return render_template("eth.html", version=VERSION)

    @app.get("/health")
    @app.get("/healthz")
    def health():
        return jsonify(ok=True, build=VERSION, runtime=runtime.status()["running"],
                       market="ETH_USDT", mode="signals-only")

    @app.get("/api/session")
    def whoami():
        if not session.get("csrf"):
            session["csrf"] = secrets.token_urlsafe(24)
        return jsonify(authenticated=bool(session.get("auth")), setup_needed=not bool(db.get_setting("admin_password_hash")), csrf=session["csrf"])

    @app.post("/api/setup")
    def setup():
        with setup_lock:
            if db.get_setting("admin_password_hash"):
                return jsonify(ok=False, error="已初始化，請使用原管理密碼登入"), 409
            body = request.get_json(silent=True) or {}
            password = str(body.get("password", ""))
            if len(password) < 8 or len(password) > 256:
                return jsonify(ok=False, error="管理密碼需 8–256 字元"), 400
            db.set_setting("admin_password_hash", generate_password_hash(password))
            session.clear()
            session.update(auth=True, csrf=secrets.token_urlsafe(24))
            return jsonify(ok=True, csrf=session["csrf"])

    @app.post("/api/login")
    def login():
        now = time.time()
        attempts = store.state("login_attempts", []) or []
        attempts = [x for x in attempts if now-x < 60]
        if len(attempts) >= 10:
            return jsonify(ok=False, error="嘗試過多，請稍後再登入"), 429
        body = request.get_json(silent=True) or {}
        password = str(body.get("password", ""))[:256]
        encoded = db.get_setting("admin_password_hash")
        if encoded and check_password_hash(encoded, password):
            session.clear()
            session.update(auth=True, csrf=secrets.token_urlsafe(24))
            store.save("login_attempts", [])
            return jsonify(ok=True, csrf=session["csrf"])
        store.save("login_attempts", attempts+[now])
        return jsonify(ok=False, error="密碼錯誤"), 401

    @app.post("/api/logout")
    def logout():
        session.clear()
        return jsonify(ok=True)

    @app.get("/api/state")
    @auth_required
    def state():
        raw_sources = db.rows("SELECT key,value FROM settings WHERE key LIKE 'eth:source:%'")
        source_status = {r["key"].removeprefix("eth:source:"): json.loads(r["value"]) for r in raw_sources}
        return jsonify(snapshot=store.state("snapshot", {}), active=store.state("active"),
                       runtime=runtime.status(), events=store.events(), sources=source_status,
                       provider_diagnostics=providers.feeds(refresh=False),
                       discord=store.state("discord", {}), migration=store.state("migrated"))

    @app.get("/api/quote")
    @auth_required
    def quote():
        q = providers.live_quote()
        now = time.time()
        fresh = bool(q.get('ok') and -5 <= now-q.get('observed_at',0) <= 30)
        snapshot = store.state('snapshot', {}) or {}
        structure_fresh = -5 <= now-snapshot.get('as_of',0) <= 1800
        return jsonify(quote=q, fresh=fresh, structure_fresh=structure_fresh,
                       levels=project_levels(snapshot.get('levels',{}), q.get('last') if fresh and structure_fresh else None),
                       structure_as_of=snapshot.get('as_of'))

    @app.get("/api/config")
    @auth_required
    def config():
        return jsonify(strategy={**DEFAULTS, **(store.state("config", {}) or {})},
                       discord=store.state("discord", {}), discord_token_configured=bool(discord.token()),
                       providers={p: {"configured": bool(providers.credential(p)),
                                      "unreadable_keys": vault.unreadable_keys(p)} for p in PAID},
                       gate_key_configured=bool(vault.get("gate", "api_key") or vault.get("gateio", "api_key")))

    @app.post("/api/config")
    @auth_required
    def update_config():
        body = request.get_json(silent=True) or {}
        proposed = body.get("strategy", {})
        if not isinstance(proposed, dict):
            return jsonify(ok=False, error="設定格式不正確"), 400
        cfg = {**DEFAULTS, **(store.state("config", {}) or {})}
        limits = {"min_fitness": (40, 90), "min_net_rr": (0.5, 5), "fee_per_side": (0, 0.01),
                  "slippage_roundtrip": (0, 0.02), "funding_reserve": (0, 0.02),
                  "plan_lifetime_hours": (1, 72), "nansen_ttl": (600, 14400)}
        for key, value in proposed.items():
            if key == "enabled":
                if not isinstance(value, bool):
                    return jsonify(ok=False, error="啟用欄位必須為布林值"), 400
                cfg[key] = value
            elif key in limits and num(value) is not None and limits[key][0] <= float(value) <= limits[key][1]:
                cfg[key] = float(value)
            else:
                return jsonify(ok=False, error=f"不支援或超出範圍的設定：{key}"), 400
        store.save("config", cfg)
        return jsonify(ok=True)

    @app.post("/api/providers/<provider>")
    @auth_required
    def configure_provider(provider):
        if provider not in (*PAID, "gate"):
            return jsonify(ok=False, error="已移除此來源，不接受舊 API 設定"), 404
        body = request.get_json(silent=True) or {}
        allowed = {"access_token", "api_key"} if provider == "bitquery" else {"api_key", "api_secret"} if provider == "gate" else {"api_key"}
        for key, value in body.items():
            if key not in allowed or not isinstance(value, str) or len(value) > 8192:
                return jsonify(ok=False, error="憑證欄位無效"), 400
        if provider == 'bitquery':
            for key, value in body.items():
                value = value.strip()
                if value.lower().startswith('authorization:'):
                    value = value.split(':',1)[1].strip()
                if value.lower().startswith('bearer '):
                    value = value[7:].strip()
                if value and ('...' in value or '…' in value or any(c.isspace() for c in value)):
                    return jsonify(ok=False, error='請貼完整Access Token，不可使用截斷文字或空白字串'), 400
                body[key] = value
        for key, value in body.items():
            if value.strip():
                vault.set(provider, key, value)
        with db.tx() as c:
            c.execute("DELETE FROM settings WHERE key LIKE ?", ("eth:backoff:" + provider + ":%",))
            c.execute("DELETE FROM settings WHERE key LIKE ?", ("eth:cache:" + provider + ":%",))
            c.execute("DELETE FROM settings WHERE key LIKE ?", ("eth:backoff:r2:" + provider + ":%",))
            c.execute("DELETE FROM settings WHERE key LIKE ?", ("eth:cache:r2:" + provider + ":%",))
        return jsonify(ok=True)

    @app.post("/api/providers/<provider>/test")
    @auth_required
    def test_provider(provider):
        if provider not in PAID:
            return jsonify(ok=False, error="僅測試保留的三個鏈上來源"), 404
        result = providers.feed(provider, force=True)
        return jsonify(ok=result.get("ok") and not result.get("stale"), result=result)

    @app.post("/api/discord/config")
    @auth_required
    def configure_discord():
        body = request.get_json(silent=True) or {}
        cfg = dict(store.state("discord", {}) or {})
        for field in ("hourly", "strategy", "anchor"):
            if field in body:
                value = str(body[field]).strip()
                if value and (not value.isdigit() or not 15 <= len(value) <= 24):
                    return jsonify(ok=False, error="頻道 ID 需為 15–24 位數字"), 400
                cfg[field] = value
        if cfg.get("hourly") and cfg.get("hourly") == cfg.get("strategy"):
            return jsonify(ok=False, error="整點與策略不能使用同一頻道"), 400
        for field in ("enabled", "auto_create"):
            if field in body:
                if not isinstance(body[field], bool):
                    return jsonify(ok=False, error="通知開關格式錯誤"), 400
                cfg[field] = body[field]
        token = body.get("bot_token")
        if token and (not isinstance(token, str) or len(token) > 2048):
            return jsonify(ok=False, error="Bot Token 格式錯誤"), 400
        if token:
            vault.set("discord", "bot_token", token)
        store.save("discord", cfg)
        store.save("channel_error", None)
        runtime.last_provision = 0
        with db.tx() as c:
            c.execute("UPDATE eth_events SET next_try=0,status='retry' WHERE status='blocked'")
        return jsonify(ok=True)

    @app.post("/api/discord/provision")
    @auth_required
    def provision():
        try:
            result = discord.ensure_channels()
            store.save("channel_error", None)
            return jsonify(ok=True, routes=result)
        except Exception as exc:
            message = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
            store.save("channel_error", {"at": time.time(), "error": message})
            return jsonify(ok=False, error=message), 400

    @app.post("/api/discord/test/<route>")
    @auth_required
    def test_discord(route):
        if route not in {"hourly", "strategy"}:
            return jsonify(ok=False, error="未知通知路由"), 404
        now = time.time()
        item = event(store.state("snapshot", {}) or {}, "test", "test:"+secrets.token_hex(8),
                     reason="使用者手動通知測試，非交易訊號。", now=now)
        item["route"] = route
        store.enqueue(item, now)
        return jsonify(ok=True, message="已加入通知佇列；請查看送達狀態，不代表已送達")

    @app.post("/api/scan")
    @auth_required
    def scan():
        accepted = runtime.submit(True)
        return jsonify(ok=True, accepted=accepted, message="已開始完整 ETH 掃描" if accepted else "已有掃描執行中，不重複耗用 API")

    @app.get("/api/history/legacy")
    @auth_required
    def legacy():
        rows = db.rows("SELECT symbol,mode,side,entry_price,leverage,enabled FROM watchlist ORDER BY id DESC LIMIT 100")
        return jsonify(rows=rows, note="舊版歷史備查；非 ETH 已停用，新系統不將歷史紀錄當成實際成交同步")

    # Retire previously cached PWA scripts rather than serving the old scanner UI.
    @app.get("/sw.js")
    @app.get("/service-worker.js")
    def retire_worker():
        script = "self.addEventListener('install',()=>self.skipWaiting());self.addEventListener('activate',e=>e.waitUntil(self.registration.unregister().then(()=>self.clients.claim())));"
        return app.response_class(script, mimetype="application/javascript")

    if start_runtime:
        runtime.start()
        atexit.register(runtime.stop)
    return app


app = create_app()
