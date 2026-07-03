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

from tophat.server import auth, service
from tophat.services.automation import Automation
from tophat.store import credentials as creds_store
from tophat.store import single_instance
from tophat.store.config import load_settings, update_settings

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
    creds_store.seed_credentials_from_env()
    app.state.brokers = service.build_broker_pool()
    app.state.automation = Automation(lambda: app.state.brokers)
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

    def rebuild_pool():
        """Re-read credentials and rebuild the broker pool (after a key change)."""
        for h in getattr(app.state, "brokers", []):
            try:
                if h.broker is not None and hasattr(h.broker, "close"):
                    h.broker.close()
            except Exception:
                pass
        app.state.brokers = service.build_broker_pool()
        service.invalidate_snapshot_cache()

    @app.middleware("http")
    async def auth_gate(request, call_next):
        path = request.url.path
        if path in PUBLIC_PATHS or path.startswith("/assets"):
            return await call_next(request)
        if auth.verify_cookie(request.cookies.get(auth.COOKIE_NAME)) is None:
            if path.startswith("/api") or path == "/ws":
                return JSONResponse({"error": "unauthorized"}, status_code=401)
            return RedirectResponse("/login")
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
        return service.build_dashboard(app.state.brokers)

    @app.get("/api/automation")
    def automation_status():
        s = app.state.automation.status()
        s["auto_execute"] = load_settings().auto_execute
        return s

    @app.get("/api/accounts")
    def list_accounts():
        return service.list_accounts_detail(app.state.brokers)

    @app.post("/api/accounts/{account_id}/lifecycle")
    def patch_lifecycle(account_id: int, body: dict):
        return service.update_account_lifecycle(account_id, body, app.state.brokers)

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

    @app.post("/api/accounts/{account_id}/payout-taken")
    def payout_taken(account_id: int):
        return service.mark_payout(account_id)

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
        return service.run_all_sessions(app.state.brokers, execute=execute, manual=manual)

    @app.websocket("/ws")
    async def ws(socket: WebSocket):
        if auth.verify_cookie(socket.cookies.get(auth.COOKIE_NAME)) is None:
            await socket.close(code=1008)
            return
        await socket.accept()
        try:
            while True:
                # In a worker thread: a cache-miss build does REST round-trips, and
                # blocking the event loop here would delay the automation loop's
                # precisely timed 09:45:00 wake-up.
                snap = await asyncio.to_thread(service.build_dashboard, app.state.brokers)
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
