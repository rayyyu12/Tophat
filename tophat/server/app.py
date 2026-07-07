"""FastAPI app for the TopHat dashboard.

    python -m tophat.server            # serve on http://127.0.0.1:8800 (mock broker)
    TOPHAT_BROKER=live python -m tophat.server   # live ProjectX (needs .env creds)

Create a login first:  python -m tophat.server.auth adduser you@x.com 'password'
"""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

import threading

from tophat.server import auth, service
from tophat.services import mirror_sync
from tophat.services.automation import Automation
from tophat.store import credentials as creds_store
from tophat.store import mirrors as mirrors_store
from tophat.store import single_instance, tenant
from tophat.store.config import load_settings, update_settings
from tophat.store.firms import FIRMS, FOLLOWER_FIRMS

STATIC_DIR = Path(__file__).parent / "static"
PUBLIC_PATHS = {"/login", "/api/login"}
SECURE_COOKIES = os.getenv("TOPHAT_HTTPS", "").lower() in ("1", "true", "yes")
# How often the WS pushes a snapshot to the UI. Cheap: build_snapshot is cached
# (SNAPSHOT_TTL), so a fast UI cadence does NOT mean a fast broker poll cadence.
WS_INTERVAL = float(os.getenv("TOPHAT_WS_INTERVAL", "3"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    # [debuglog] TEMPORARY verbose run log -> logs/. Start first so we capture startup.
    from tophat import debuglog
    debuglog.start()
    # Single-instance guard FIRST: refuse to start a second auto-fire loop against
    # the shared API key + data dir. Raising here aborts ASGI startup, so it works
    # however the app is launched (python tophat.py OR uvicorn ...app:app).
    app.state.instance_lock = single_instance.acquire_or_none()
    auth.seed_admin_from_env()
    # Multi-tenant stores (store/tenant.py): every user's state lives under
    # data/users/<uid>/. One-time move of pre-tenant global files into the
    # first (operator) user's dir, then seed env credentials there.
    operator_uid = tenant.local_operator_uid()
    moved = tenant.migrate_legacy_to(operator_uid)
    if moved:
        import logging
        logging.getLogger("tophat.tenant").info(
            "migrated legacy data files into users/%s: %s", operator_uid, moved)
    with tenant.as_user(operator_uid):
        creds_store.seed_credentials_from_env()
    # Broker pools are built lazily per user (a pool needs that user's keys).
    app.state.pools = {}
    app.state.pools_lock = threading.Lock()

    def pool_for(uid: int):
        with app.state.pools_lock:
            pool = app.state.pools.get(uid)
            if pool is None:
                with tenant.as_user(uid):
                    pool = service.build_broker_pool()
                app.state.pools[uid] = pool
        return pool

    app.state.pool_for = pool_for
    app.state.automation = Automation(pool_for)
    app.state.automation.start()
    try:
        yield
    finally:
        await app.state.automation.stop()
        lock = getattr(app.state, "instance_lock", None)
        if lock is not None:
            lock.release()
        debuglog.stop()   # [debuglog] flush the log on shutdown


def create_app() -> FastAPI:
    app = FastAPI(title="TopHat", docs_url=None, redoc_url=None, lifespan=lifespan)

    def current_pool():
        """The logged-in user's broker pool (middleware set the tenant)."""
        uid = tenant.get_user()
        return app.state.pool_for(uid)

    def rebuild_pool():
        """Re-read the CURRENT user's credentials and rebuild their broker pool
        (after a key change). Other users' pools are untouched."""
        uid = tenant.get_user()
        with app.state.pools_lock:
            old = app.state.pools.pop(uid, [])
        for h in old:
            try:
                if h.broker is not None and hasattr(h.broker, "close"):
                    h.broker.close()
            except Exception:
                pass
        app.state.pool_for(uid)
        service.invalidate_broker_cache()

    @app.middleware("http")
    async def auth_gate(request, call_next):
        path = request.url.path
        if path in PUBLIC_PATHS or path.startswith("/assets"):
            return await call_next(request)
        uid = auth.verify_cookie(request.cookies.get(auth.COOKIE_NAME))
        if uid is None:
            if path.startswith("/api") or path == "/ws":
                return JSONResponse({"error": "unauthorized"}, status_code=401)
            return RedirectResponse("/login")
        # Bind the request to its user: every store read/write below resolves
        # into data/users/<uid>/ (store/tenant.py). call_next's downstream task
        # inherits a copy of this context; sync endpoints get it in the threadpool.
        tenant.set_user(uid)
        return await call_next(request)

    # --- auth ---
    @app.post("/api/login")
    async def login(body: dict):
        uid = auth.verify_login(body.get("email", ""), body.get("password", ""))
        if uid is None:
            return JSONResponse({"error": "invalid credentials"}, status_code=401)
        resp = JSONResponse({"ok": True})
        resp.set_cookie(auth.COOKIE_NAME, auth.make_cookie(uid), httponly=True,
                        samesite="lax", secure=SECURE_COOKIES, max_age=auth.SESSION_TTL)
        return resp

    @app.post("/api/logout")
    def logout():
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(auth.COOKIE_NAME, path="/", samesite="lax", secure=SECURE_COOKIES)
        return resp

    @app.get("/login")
    def login_page():
        return FileResponse(STATIC_DIR / "login.html")

    # --- data / actions ---
    @app.get("/api/state")
    def get_state():
        return service.build_dashboard(current_pool())

    @app.get("/api/automation")
    def automation_status():
        s = app.state.automation.status()
        s["auto_execute"] = load_settings().auto_execute
        return s

    @app.get("/api/accounts")
    def list_accounts():
        return service.list_accounts_detail(current_pool())

    @app.post("/api/accounts/{account_id}/lifecycle")
    def patch_lifecycle(account_id: int, body: dict):
        return service.update_account_lifecycle(account_id, body, current_pool())

    # --- ProjectX API keys (encrypted at rest; one per username) ---
    @app.get("/api/credentials")
    def list_credentials():
        return creds_store.public_list()

    @app.post("/api/credentials")
    def add_credential(body: dict):
        try:
            creds_store.add_credential(
                (body or {}).get("username", ""),
                (body or {}).get("api_key", ""),
                (body or {}).get("base_url", ""))
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        rebuild_pool()
        return creds_store.public_list()

    @app.delete("/api/credentials/{username}")
    def remove_credential(username: str):
        creds_store.delete_credential(username)
        rebuild_pool()
        return creds_store.public_list()

    @app.post("/api/credentials/{username}/reveal")
    def reveal_credential(username: str):
        c = creds_store.get_credential(username)
        if c is None:
            return JSONResponse({"error": "unknown credential"}, status_code=404)
        return {"username": c.username, "api_key": c.api_key}

    @app.post("/api/accounts/{account_id}/toggle")
    def toggle(account_id: int):
        return {"account_id": account_id, "enabled": service.toggle_account(account_id)}

    @app.post("/api/accounts/{account_id}/set-enabled")
    def set_enabled(account_id: int, body: dict):
        enabled = bool((body or {}).get("enabled", True))
        return {"account_id": account_id,
                "enabled": service.set_account_enabled(account_id, enabled)}

    @app.post("/api/accounts/set-enabled")
    def set_enabled_bulk(body: dict):
        """Enable/disable a whole table at once (the per-panel master toggle)."""
        body = body or {}
        try:
            ids = [int(x) for x in body.get("ids", [])]
        except (TypeError, ValueError):
            return JSONResponse({"error": "ids must be integers"}, status_code=400)
        enabled = bool(body.get("enabled", True))
        return {"updated": service.set_accounts_enabled(ids, enabled),
                "enabled": enabled}

    @app.post("/api/accounts/{account_id}/payout-taken")
    def payout_taken(account_id: int):
        return service.mark_payout(account_id)

    # --- mirror (follower) accounts: API-less firms tracked by inference ---
    @app.get("/api/firms")
    def list_firms():
        from dataclasses import asdict as dc
        return {"firms": {k: dc(p) for k, p in FIRMS.items()},
                "follower_firms": sorted(FOLLOWER_FIRMS)}

    @app.get("/api/mirrors")
    def list_mirrors():
        from datetime import datetime
        from zoneinfo import ZoneInfo
        ms = mirrors_store.load_mirrors()
        today = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
        names = service.account_names(current_pool())
        return {"mirrors": [{**mirror_sync.public_view(m),
                             "leader_name": names.get(m.leader_id, "")}
                            for m in ms.values()],
                "hazards": mirror_sync.hazards(ms, today)}

    @app.post("/api/mirrors")
    def create_mirror(body: dict):
        body = body or {}
        try:
            m = mirrors_store.create_mirror(
                str(body.get("firm", "")),
                account_number=str(body.get("account_number", "")),
                alias=str(body.get("alias", "")),
                leader_id=(int(body["leader_id"])
                           if body.get("leader_id") not in (None, "") else None),
                multiplier=(float(body["multiplier"])
                            if body.get("multiplier") not in (None, "") else None),
                phase=str(body.get("phase", "eval")),
            )
        except (ValueError, KeyError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return mirror_sync.public_view(m)

    @app.post("/api/mirrors/{mirror_id}/update")
    def update_mirror(mirror_id: str, body: dict):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        today = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
        try:
            m = mirrors_store.patch_mirror(mirror_id, body or {}, today=today)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        if m is None:
            return JSONResponse({"error": "unknown mirror"}, status_code=404)
        return mirror_sync.public_view(m)

    @app.delete("/api/mirrors/{mirror_id}")
    def remove_mirror(mirror_id: str):
        if not mirrors_store.delete_mirror(mirror_id):
            return JSONResponse({"error": "unknown mirror"}, status_code=404)
        return {"ok": True}

    @app.post("/api/mirrors/{mirror_id}/activate-funded")
    def mirror_activate_funded(mirror_id: str, body: dict | None = None):
        leader = (body or {}).get("leader_id")
        try:
            m = mirrors_store.with_mirror(
                mirror_id, lambda x: mirror_sync.activate_funded(
                    x, leader_id=int(leader) if leader not in (None, "") else None))
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        if m is None:
            return JSONResponse({"error": "unknown mirror"}, status_code=404)
        return mirror_sync.public_view(m)

    @app.post("/api/mirrors/{mirror_id}/pair")
    def mirror_pair(mirror_id: str, body: dict):
        try:
            m = mirrors_store.with_mirror(
                mirror_id, lambda x: mirror_sync.pair_waiting(x, int(body["leader_id"])))
        except (ValueError, KeyError, TypeError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        if m is None:
            return JSONResponse({"error": "unknown mirror"}, status_code=404)
        return mirror_sync.public_view(m)

    # --- copier plan: the daily Tradecopia edit list ---
    def _today_et() -> str:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")

    @app.get("/api/copier-plan")
    def get_copier_plan():
        from tophat.services import copier_plan as cp
        plan = cp.build_today_plan(current_pool(), _today_et())
        return cp.plan_to_dict(plan)

    @app.post("/api/copier-plan/apply")
    def apply_copier_plan():
        from datetime import datetime
        from zoneinfo import ZoneInfo
        from tophat.services import copier_plan as cp
        now = datetime.now(ZoneInfo("America/New_York"))
        plan = cp.build_today_plan(current_pool(), now.strftime("%Y-%m-%d"))
        cp.apply_plan(plan, applied_at=now.strftime("%Y-%m-%d %H:%M:%S ET"))
        service.invalidate_snapshot_cache()
        return cp.plan_to_dict(plan)

    @app.post("/api/mirrors/{mirror_id}/payout-taken")
    def mirror_payout_taken(mirror_id: str):
        paid = {}

        def do(x):
            paid["amount"] = mirror_sync.mark_mirror_payout(x)

        m = mirrors_store.with_mirror(mirror_id, do)
        if m is None:
            return JSONResponse({"error": "unknown mirror"}, status_code=404)
        return {**mirror_sync.public_view(m), "paid": paid.get("amount", 0.0)}

    @app.get("/api/analytics")
    def get_analytics():
        from tophat.server import analytics
        return analytics.build_analytics(current_pool())

    # --- simulation: backtest + Monte Carlo over the cached NQ minute bars ---
    @app.get("/api/sim/meta")
    def sim_meta():
        from tophat.services import simulator
        from tophat.store import simdata
        return {"coverage": simdata.coverage(),
                "firms": {k: asdict(p) for k, p in FIRMS.items()},
                "defaults": asdict(simulator.SimParams())}

    @app.post("/api/sim/run")
    def sim_run(body: dict):
        # sync def -> FastAPI's threadpool runs it, so a long MC never blocks
        # the event loop (or the automation tick's timing).
        from tophat.services import simulator
        from tophat.store import simdata, simstore
        if not simdata.available():
            return JSONResponse(
                {"error": "no tick data cache - import NinjaTrader tick data, "
                          "then run research/reconstruction/export_sim_ticks.py"},
                status_code=400)
        body = body or {}
        try:
            p = simulator.params_from_dict(body.get("params") or {})
            views = simdata.day_views(p.entry_time)
            result = simulator.run_sim(p, views)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        tid = str(body.get("template_id") or "")
        if tid:
            simstore.attach_run(tid, result)
        return result

    @app.get("/api/sim/templates")
    def sim_list_templates():
        from tophat.store import simstore
        return {"templates": simstore.list_templates()}

    @app.post("/api/sim/templates")
    def sim_save_template(body: dict):
        from tophat.services import simulator
        from tophat.store import simstore
        body = body or {}
        try:
            p = simulator.params_from_dict(body.get("params") or {})
            t = simstore.save_template(str(body.get("name", "")), asdict(p))
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return t

    @app.delete("/api/sim/templates/{template_id}")
    def sim_delete_template(template_id: str):
        from tophat.store import simstore
        if not simstore.delete_template(template_id):
            return JSONResponse({"error": "unknown template"}, status_code=404)
        return {"ok": True}

    @app.get("/api/sim/templates/{template_id}/result")
    def sim_template_result(template_id: str):
        from tophat.store import simstore
        r = simstore.load_run(template_id)
        if r is None:
            return JSONResponse({"error": "no stored run"}, status_code=404)
        return r

    @app.get("/api/settings")
    def get_settings():
        return asdict(load_settings())

    @app.post("/api/settings")
    async def post_settings(patch: dict):
        try:
            updated = update_settings(patch)
        except (ValueError, TypeError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        # auto_execute drives Execute-button visibility — bust the cache so the
        # change shows on the next refresh instead of waiting out SNAPSHOT_TTL.
        service.invalidate_snapshot_cache()
        return asdict(updated)

    @app.post("/api/run")
    def run(body: dict | None = None):
        body = body or {}
        execute = bool(body.get("execute", False))
        # Manual Execute from the dashboard is an explicit, confirmed operator
        # action — it fires regardless of the auto-execute (automation) switch.
        manual = bool(body.get("manual", False))
        return service.run_all_sessions(current_pool(), execute=execute, manual=manual)

    @app.websocket("/ws")
    async def ws(socket: WebSocket):
        uid = auth.verify_cookie(socket.cookies.get(auth.COOKIE_NAME))
        if uid is None:
            await socket.close(code=1008)
            return
        # The HTTP auth middleware doesn't run for WebSockets — bind the tenant
        # here so every store read in the push loop is scoped to this user
        # (asyncio.to_thread carries the context into the worker thread).
        tenant.set_user(uid)
        await socket.accept()
        try:
            while True:
                # In a worker thread: a cache-miss build does REST round-trips, and
                # blocking the event loop here would delay the automation loop's
                # precisely timed 09:45:00 wake-up.
                snap = await asyncio.to_thread(service.build_dashboard, current_pool())
                await socket.send_json(snap)
                await asyncio.sleep(WS_INTERVAL)
        except (WebSocketDisconnect, Exception):
            return

    if STATIC_DIR.exists():
        app.mount("/assets", StaticFiles(directory=STATIC_DIR), name="assets")

        @app.get("/")
        def index():
            return FileResponse(STATIC_DIR / "index.html")

    return app


app = create_app()


def main() -> None:
    from dotenv import load_dotenv
    load_dotenv()
    import uvicorn
    uvicorn.run(app, host=os.getenv("TOPHAT_HOST", "127.0.0.1"),
                port=int(os.getenv("TOPHAT_PORT", "8800")), log_level="info")


if __name__ == "__main__":
    main()
