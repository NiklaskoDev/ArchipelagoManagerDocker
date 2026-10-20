"""Application entry point."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__, apbridge, db
from .config import CONFIG
from .generation import GENERATION
from .rooms import ROOMS
from .routes import admin, apworlds, auth, configs, games, jobs
from .security import CSRF_HEADER, CSRF_VALUE

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
    log.info("Archipelago %s ready, %d games available", apbridge.ap_version(),
             len(apbridge.installed_games(include_hidden=True)))


@asynccontextmanager
async def lifespan(_app: FastAPI):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    db.init_db()
    auth.bootstrap_admin()
    ROOMS.reset()
    await GENERATION.start()
    init_task = asyncio.create_task(init_archipelago())
    log.info("Archipelago Manager %s listening, data in %s", __version__, CONFIG.data_dir)
    yield
    init_task.cancel()
    await GENERATION.stop()
    await ROOMS.shutdown()


class BodyLimit:
    """Rejects oversized request bodies before Starlette spools them to disk.

    Only the upload endpoints may be large; everything else is a small JSON document or a handful of yaml files.
    """

    UPLOAD_PATHS = ("/api/games", "/api/apworlds")  # multiworld zips, .apworld files and extra files of a game
    SMALL_LIMIT = 16 * 1024 * 1024

    def __init__(self, app) -> None:
        self.app = app

    def limit_for(self, scope) -> int:
        path = scope["path"]
        is_upload = path in self.UPLOAD_PATHS or (path.startswith("/api/games/") and path.endswith("/files"))
        return CONFIG.max_upload_bytes + 1024 * 1024 if is_upload else self.SMALL_LIMIT

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


@app.middleware("http")
async def guard(request: Request, call_next):
    # Browsers cannot attach a custom header to a cross-site request without a CORS preflight, which we never allow.
    # Requiring it on every state-changing call is therefore a complete CSRF defence next to the SameSite cookie.
    if (request.method not in ("GET", "HEAD", "OPTIONS") and request.url.path.startswith("/api/")
            and request.headers.get(CSRF_HEADER) != CSRF_VALUE):
        return JSONResponse(status_code=403, content={"detail": "Missing request header"})
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


for module in (auth, configs, apworlds, games, jobs, admin):
    app.include_router(module.router)

app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
