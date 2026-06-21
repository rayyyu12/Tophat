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
from tophat.store.config import load_settings, update_settings

STATIC_DIR = Path(__file__).parent / "static"
PUBLIC_PATHS = {"/login", "/api/login"}
SECURE_COOKIES = os.getenv("TOPHAT_HTTPS", "").lower() in ("1", "true", "yes")


@asynccontextmanager
async def lifespan(app: FastAPI):
    auth.seed_admin_from_env()
    broker, mode = service.make_broker()
    app.state.broker = broker
    app.state.mode = mode
    app.state.automation = Automation(broker)
    app.state.automation.start()
    try:
        yield
    finally:
        await app.state.automation.stop()


def create_app() -> FastAPI:
    app = FastAPI(title="TopHat", docs_url=None, redoc_url=None, lifespan=lifespan)

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
        return service.build_snapshot(app.state.broker, mode=app.state.mode)

    @app.get("/api/automation")
    def automation_status():
        s = app.state.automation.status()
        s["auto_execute"] = load_settings().auto_execute
        return s

    @app.post("/api/accounts/{account_id}/toggle")
    def toggle(account_id: int):
        return {"account_id": account_id, "enabled": service.toggle_account(account_id)}

    @app.post("/api/accounts/{account_id}/payout-taken")
    def payout_taken(account_id: int):
        return service.mark_payout(account_id)

    @app.get("/api/settings")
    def get_settings():
        return asdict(load_settings())

    @app.post("/api/settings")
    async def post_settings(patch: dict):
        return asdict(update_settings(patch))

    @app.post("/api/run")
    def run(body: dict | None = None):
        execute = bool((body or {}).get("execute", False))
        return service.run_session(app.state.broker, execute=execute)

    @app.websocket("/ws")
    async def ws(socket: WebSocket):
        if auth.verify_cookie(socket.cookies.get(auth.COOKIE_NAME)) is None:
            await socket.close(code=1008)
            return
        await socket.accept()
        try:
            while True:
                snap = service.build_snapshot(app.state.broker, mode=app.state.mode)
                await socket.send_json(snap)
                await asyncio.sleep(3.0)
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
