"""Application entry point."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__, apbridge, audit, db, notify, reminders, schedule, signups, sources, updates, versions
from .config import CONFIG
from .generation import GENERATION
from .rooms import ROOMS
from .textclient import TEXT_CLIENTS
from .routes import admin, apworlds, auth, configs, games, jobs, roms
from .routes import presets as preset_routes, tokens, versions as version_routes
from .routes import signups as signup_routes
from .routes import sources as source_routes
from .security import CSRF_HEADER, CSRF_VALUE, SAFE_METHODS, bearer_token, token_for_request, user_for_request

log = logging.getLogger("archipelago_manager")

STATIC_DIR = Path(__file__).resolve().parent / "static"

# Images may also come from Discord's image server: the pictures of linked Discord accounts (discord_auth.avatar_url)
CONTENT_SECURITY_POLICY = ("default-src 'self'; img-src 'self' data: https://cdn.discordapp.com; "
                           "style-src 'self' 'unsafe-inline'; "
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
    for row in db.query("SELECT version FROM ap_versions WHERE status = 'installed'"):
        if versions.present(row["version"]):  # what the further versions offer, for checks and the settings page
            try:
                await apbridge.extra_info(row["version"])
            except apbridge.APBridgeError as e:
                log.error("Archipelago %s is not usable: %s", row["version"], e)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    db.init_db()
    signups.count_slots()
    versions.reset()
    audit.prune()
    auth.bootstrap_admin()
    auth.prepare_setup()
    ROOMS.reset()
    notify.start()
    await GENERATION.start()
    tasks = [asyncio.create_task(init_archipelago()), asyncio.create_task(ROOMS.watch_progress()),
             asyncio.create_task(ROOMS.watch_idle()), asyncio.create_task(ROOMS.watch_certificate()),
             asyncio.create_task(signups.run_periodically()), asyncio.create_task(TEXT_CLIENTS.run_periodically()),
             asyncio.create_task(schedule.run_periodically()), asyncio.create_task(reminders.run_periodically())]
    if CONFIG.update_check:  # both talk to GitHub on their own, which UPDATE_CHECK=false rules out
        tasks.append(asyncio.create_task(updates.run_periodically()))
        tasks.append(asyncio.create_task(sources.run_periodically()))
    log.info("Archipelago Manager %s listening, data in %s", __version__, CONFIG.data_dir)
    yield
    for task in [*tasks, versions.STATE.task]:
        if task:
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
        if admin and path == "/api/roms":
            return upload  # base ROMs
        if user["role"] in ("admin", "organizer") and (
                path == "/api/games" or (path.startswith("/api/games/") and path.endswith("/files"))):
            return upload  # multiworld zips, extra files of a game (whose game it is, the route checks)
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


def _token_refusal(request: Request) -> JSONResponse | None:
    """Why a request with an API token cannot go ahead, if it cannot. Tokens do not manage tokens, passwords, sign-ins,
    Discord links or user accounts: a leaked token must not be able to lock its owner out, make itself new tokens or
    make an administrator of its own."""
    path = request.url.path
    if user_for_request(request) is None:
        return JSONResponse(status_code=401, content={"detail": "This API token is not valid (any more)"})
    if path.startswith("/api/tokens") or (path.startswith("/api/auth/") and path != "/api/auth/status") or (
            path.startswith("/api/users") and request.method not in SAFE_METHODS):
        return JSONResponse(status_code=403, content={"detail": "API tokens cannot be used for this. Sign in instead."})
    if request.method not in SAFE_METHODS and token_for_request(request)["access"] != "write":
        return JSONResponse(status_code=403, content={"detail": "This API token can only read"})
    return None


@app.middleware("http")
async def guard(request: Request, call_next):
    if request.url.path.startswith("/api/") and bearer_token(request) is not None:
        # A browser cannot send an Authorization header of its own choosing to another site without a CORS preflight,
        # which we never allow, so a token is no CSRF risk and needs no extra header
        refusal = _token_refusal(request)
        if refusal is not None:
            return refusal
    # Browsers cannot attach a custom header to a cross-site request without a CORS preflight, which we never allow.
    # Requiring it on every state-changing call is therefore a complete CSRF defence next to the SameSite cookie.
    elif (request.method not in SAFE_METHODS and request.url.path.startswith("/api/")
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


@app.middleware("http")
async def audit_trail(request: Request, call_next):
    """Records administrators' changes and game starts and stops (audit.py), once they succeeded."""
    if not audit.watched(request):
        return await call_next(request)
    user = user_for_request(request)
    before = audit.target(request.url.path) if user else None  # its name, before a delete takes it away
    response = await call_next(request)
    if user is not None and response.status_code < 400:
        try:
            audit.finish(request, user, before)
        except Exception:  # noqa: BLE001 - the change happened; a missing line must not turn it into an error
            log.exception("Writing the audit log failed")
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


for module in (auth, configs, apworlds, source_routes, games, jobs, signup_routes, admin, roms, version_routes, tokens,
               preset_routes):
    app.include_router(module.router)

app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
