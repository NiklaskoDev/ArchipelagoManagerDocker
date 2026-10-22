#!/usr/bin/env python3
"""End-to-end smoke test for a running Archipelago Manager (typically the freshly built Docker image).

    python tests/smoke.py http://localhost:8080 [game-host]

Needs an instance with no users yet. It creates an administrator, generates a two-player multiworld, hosts it and checks
that the game server answers on its published port. Uses only the standard library.
"""
import http.cookiejar
import json
import socket
import sys
import time
import urllib.error
import urllib.request

BASE = sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "http://localhost:8080"
GAME_HOST = sys.argv[2] if len(sys.argv) > 2 else "127.0.0.1"
HEADERS = {"X-Requested-With": "archipelago-manager", "Content-Type": "application/json"}

jar = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(BASE + path, data=data, method=method, headers=HEADERS)
    try:
        with opener.open(request, timeout=60) as response:
            return json.loads(response.read() or b"null")
    except urllib.error.HTTPError as e:
        raise SystemExit(f"{method} {path} -> {e.code}: {e.read().decode()[:300]}") from None


def wait(description, probe, timeout=180):
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = probe()
        if result:
            return result
        time.sleep(1.5)
    raise SystemExit(f"timed out waiting for {description}")


def health():
    try:
        return call("GET", "/api/health")
    except SystemExit:
        return None
    except OSError:
        return None


def step(message):
    print(f"- {message}", flush=True)


ready = wait("the manager to be ready", lambda: (h := health()) and h.get("ready") and h, timeout=300)
step(f"manager {ready['version']} is ready with Archipelago {ready['archipelago']}")

status = call("GET", "/api/auth/status")
assert status["needs_setup"], "this smoke test needs an instance without users"
call("POST", "/api/auth/setup", {"username": "smoke", "password": "smoke-password-1"})
step("administrator created")

system = call("GET", "/api/system")
assert system["ap_error"] is None, system["ap_error"]
assert not system["failed_worlds"], f"worlds failed to load: {system['failed_worlds']}"
assert system["world_count"] > 70, f"only {system['world_count']} games loaded"
step(f"{system['world_count']} games loaded, none failed")

created = call("POST", "/api/configs/text", {"name": "Alice", "content": "name: Alice\ngame: ChecksFinder\nChecksFinder: {}\n"})
ids = [created["id"]]
templates = call("GET", "/api/templates")
assert len(templates) > 70, "yaml templates were not generated"
assert sum(t["form"] for t in templates) > 70, "the options forms were not generated"
# the second player's config comes from the options form, as a player would make it
form = call("POST", "/api/configs/form/write", {"game": "APQuest", "name": "Bob", "options": {"progression_balancing": 25}})
created = call("POST", "/api/configs/text", {"name": "Bob", "content": form["content"]})
ids.append(created["id"])
step(f"configs stored, one from the options form; {len(templates)} templates available")

job = call("POST", "/api/jobs", {"config_ids": ids, "name": "Smoke test", "host_after": True})
job = wait("generation to finish", lambda: (j := call("GET", f"/api/jobs/{job['id']}"))["status"] in ("succeeded", "failed") and j)
assert job["status"] == "succeeded", f"generation failed: {job['error']}"
step("multiworld generated")

game = wait("the game server to start", lambda: (g := call("GET", f"/api/games/{job['game_id']}"))["state"] in ("running", "crashed") and g)
assert game["state"] == "running", f"server did not start: {game['error']}"
port = game["port"]
with socket.create_connection((GAME_HOST, port), timeout=10) as sock:
    sock.sendall(f"GET / HTTP/1.1\r\nHost: {GAME_HOST}:{port}\r\nConnection: Upgrade\r\nUpgrade: websocket\r\n"
                 "Sec-WebSocket-Version: 13\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n\r\n".encode())
    reply = sock.recv(200)
assert reply.startswith(b"HTTP/1.1 101"), f"game server did not accept a websocket connection: {reply[:60]!r}"
step(f"game server answers on port {port}")

assert call("POST", f"/api/games/{game['id']}/stop")["state"] in ("stopping", "stopped")
step("game server stopped")
print("smoke test passed")
