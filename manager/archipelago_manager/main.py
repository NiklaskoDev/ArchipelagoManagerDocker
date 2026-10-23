"""Application entry point."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__, apbridge, db, notify, schedule, signups, sources, updates
from .config import CONFIG
from .generation import GENERATION
from .rooms import ROOMS
from .textclient import TEXT_CLIENTS
from .routes import admin, apworlds, auth, configs, games, jobs, roms
from .routes import signups as signup_routes
from .routes import sources as source_routes
from .security import CSRF_HEADER, CSRF_VALUE, user_for_request

log = logging.getLogger("archipelago_manager")

STATIC_DIR = Path(__file__).resolve().parent / "static"

CONTENT_SECURITY_POLICY = ("default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
                           "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")


async def init_archipelago() -> None:
    """Prepares the Archipelago side in the background so the web UI is available immediately."""
    try:
        await apbridge.get_where()
        await apbridge.sync_apworlds()
        await apworlds.adopt_orphans()
    except apbridge.APBridgeError as e:
        apbridge.STATE.error = str(e)
        log.error("Archipelago is not usable: %s", e)
        return
    await ROOMS.start_saved()
    await apworlds.refresh_load_status()
    await ROOMS.catch_up_history()
    log.info("Archipelago %s ready, %d games available", apbridge.ap_version(),
             len(apbridge.installed_games(include_hidden=True)))


@asynccontextmanager
async def lifespan(_app: FastAPI):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    db.init_db()
    auth.bootstrap_admin()
    ROOMS.reset()
    notify.start()
    await GENERATION.start()
    tasks = [asyncio.create_task(init_archipelago()), asyncio.create_task(ROOMS.watch_progress()),
             asyncio.create_task(signups.run_periodically()), asyncio.create_task(TEXT_CLIENTS.run_periodically()),
             asyncio.create_task(schedule.run_periodically())]
    if CONFIG.update_check:  # both talk to GitHub on their own, which UPDATE_CHECK=false rules out
        tasks.append(asyncio.create_task(updates.run_periodically()))
        tasks.append(asyncio.create_task(sources.run_periodically()))
    log.info("Archipelago Manager %s listening, data in %s", __version__, CONFIG.data_dir)
    yield
    for task in tasks:
        task.cancel()
    await GENERATION.stop()
    await TEXT_CLIENTS.shutdown()
    await ROOMS.shutdown()
    notify.stop()


class BodyLimit:
    """Rejects oversized request bodies before Starlette spools them to disk.

    FastAPI reads a form, files and all, before the route checks who is asking. So the limit depends on the session up
    front: without one only the tiny sign-in requests fit, and only the uploads of those allowed to upload may be large.
    Everything else is a small JSON document or a handful of yaml files.
    """

    ANONYMOUS_LIMIT = 64 * 1024
    SMALL_LIMIT = 16 * 1024 * 1024
    PLAYER_APWORLD_LIMIT = 100 * 1024 * 1024  # a submission still waits for approval; admins may upload more

    def __init__(self, app) -> None:
        self.app = app

    def limit_for(self, scope) -> int:
        user = user_for_request(Request(scope))
        if user is None:
            return self.ANONYMOUS_LIMIT
        path, upload = scope["path"], CONFIG.max_upload_bytes + 1024 * 1024
        admin = user["role"] == "admin"
        if path == "/api/apworlds":
            return upload if admin else min(upload, self.PLAYER_APWORLD_LIMIT)
        if admin and (path in ("/api/games", "/api/roms") or (path.startswith("/api/games/") and path.endswith("/files"))):
            return upload  # multiworld zips, extra files of a game, base ROMs
        return self.SMALL_LIMIT

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or scope["method"] not in ("POST", "PUT", "PATCH"):
            await self.app(scope, receive, send)
            return
        limit = self.limit_for(scope)
        declared = dict(scope["headers"]).get(b"content-length")
        started = False
        received = 0

        async def reject() -> None:
            body = b'{"detail":"The upload is too large"}'
            await send({"type": "http.response.start", "status": 413,
                        "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()),
                                    (b"connection", b"close")]})
            await send({"type": "http.response.body", "body": body})

        if declared and declared.isdigit() and int(declared) > limit:
            await reject()
            return

        class TooLarge(Exception):
            pass

        async def counting_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:  # chunked uploads have no Content-Length to check up front
                    raise TooLarge
            return message

        async def tracking_send(message) -> None:
            nonlocal started
            started = started or message["type"] == "http.response.start"
            await send(message)

        try:
            await self.app(scope, counting_receive, tracking_send)
        except TooLarge:
            if not started:
                await reject()


app = FastAPI(title="Archipelago Manager", version=__version__, lifespan=lifespan,
              docs_url=None, redoc_url=None, openapi_url=None)


_proxy_warned = False


def _warn_about_untrusted_proxy(request: Request) -> None:
    """Behind a reverse proxy that FORWARDED_ALLOW_IPS does not list, every visitor seems to come from the proxy: they
    share one budget of failed logins, so a few wrong passwords lock everyone out. Say so once in the log."""
    global _proxy_warned
    forwarded = request.headers.get("x-forwarded-for")
    peer = request.client.host if request.client else None
    if _proxy_warned or not forwarded or not peer or forwarded.split(",")[-1].strip() == peer:
        return  # (uvicorn puts the forwarded address in place of the proxy's when it trusts the proxy)
    _proxy_warned = True
    log.warning("Requests arrive through a reverse proxy at %s that is not trusted, so all visitors look like %s. "
                "Set FORWARDED_ALLOW_IPS=%s on the container.", peer, peer, peer)


@app.middleware("http")
async def guard(request: Request, call_next):
    # Browsers cannot attach a custom header to a cross-site request without a CORS preflight, which we never allow.
    # Requiring it on every state-changing call is therefore a complete CSRF defence next to the SameSite cookie.
    if (request.method not in ("GET", "HEAD", "OPTIONS") and request.url.path.startswith("/api/")
            and request.headers.get(CSRF_HEADER) != CSRF_VALUE):
        return JSONResponse(status_code=403, content={"detail": "Missing request header"})
    _warn_about_untrusted_proxy(request)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Content-Security-Policy"] = CONTENT_SECURITY_POLICY
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    elif "cache-control" not in response.headers:
        response.headers["Cache-Control"] = "no-cache"
    return response


app.add_middleware(BodyLimit)


@app.exception_handler(apbridge.APBridgeError)
async def bridge_error(_request: Request, exc: apbridge.APBridgeError):
    return JSONResponse(status_code=503, content={"detail": str(exc)})


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "version": __version__, "archipelago": apbridge.ap_version(),
            # False until the list of installed games has been loaded (a few seconds after startup)
            "ready": apbridge.STATE.info is not None, "archipelago_error": apbridge.STATE.error}


for module in (auth, configs, apworlds, source_routes, games, jobs, signup_routes, admin, roms):
    app.include_router(module.router)

app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
