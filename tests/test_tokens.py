"""Personal API tokens: scripts and bots act as the user who made the token, with read or full access."""
import sqlite3
import time

import httpx
import pytest


@pytest.fixture
def setup(plain_server):
    admin = plain_server.client()
    assert admin.post("/api/auth/setup", json={"username": "boss", "password": "bosspass123"}).status_code == 200
    admin = plain_server.client("boss", "bosspass123")
    assert admin.post("/api/users", json={"username": "ann", "password": "password123"}).status_code == 200
    return plain_server, admin, plain_server.client("ann", "password123")


def make_token(client, name="script", access="read", **extra):
    r = client.post("/api/tokens", json={"name": name, "access": access, **extra})
    assert r.status_code == 200, r.text
    return r.json()


def bearer(server, token, **headers):
    """A client without cookies or the web UI's extra header, as a script would be."""
    return httpx.Client(base_url=server.base_url, headers={"Authorization": f"Bearer {token}", **headers}, timeout=30)


def test_a_token_acts_as_its_owner(setup):
    server, admin, ann = setup
    made = make_token(ann, name="  my   tracker ")
    assert made["token"].startswith("apm_") and made["hint"] == made["token"][-4:]
    assert (made["name"], made["access"], made["expires_at"], made["last_used_at"]) == ("my tracker", "read", None, None)
    assert "token" not in ann.get("/api/tokens").json()[0]  # shown once only

    script = bearer(server, made["token"])
    status = script.get("/api/auth/status").json()
    assert status["user"]["username"] == "ann"
    assert script.get("/api/games").status_code == 200
    assert ann.get("/api/tokens").json()[0]["last_used_at"]

    # read: no changes, whatever the request
    r = script.post("/api/configs/text", json={"name": "x", "content": "name: X\ngame: Clique\n"})
    assert r.status_code == 403 and r.json()["detail"] == "This API token can only read"
    # tokens never manage tokens, passwords or sign-ins, whatever their access
    writer = bearer(server, make_token(ann, name="bot", access="write")["token"])
    for method, path in (("GET", "/api/tokens"), ("POST", "/api/tokens"), ("POST", "/api/auth/logout"),
                         ("POST", "/api/auth/password")):
        r = writer.request(method, path, json={"name": "more", "access": "write"})
        assert r.status_code == 403 and "Sign in instead" in r.json()["detail"], (method, path, r.text)
    # write: what the user may do, without the web UI's extra header; not what they may not
    assert writer.patch("/api/settings", json={"public_host": "x"}).status_code == 403
    assert writer.get("/api/users").status_code == 403


def test_administrators_scripts_and_the_log(setup):
    server, admin, ann = setup
    token = make_token(admin, name="nightly", access="write")["token"]
    r = bearer(server, token).patch("/api/settings", json={"public_host": "play.example.org"})
    assert r.status_code == 200, r.text
    entry = admin.get("/api/audit?action=settings").json()["entries"][0]
    assert entry["username"] == "boss" and entry["details"]["token"] == "nightly"

    # an administrator sees and revokes everybody's tokens; others only their own
    theirs = make_token(ann, name="ann's")
    assert [t["username"] for t in admin.get("/api/tokens?all=true").json()] == ["ann", "boss"]
    assert [t["name"] for t in ann.get("/api/tokens?all=true").json()] == ["ann's"]
    mine = admin.get("/api/tokens").json()[0]["id"]
    assert ann.delete(f"/api/tokens/{mine}").status_code == 404
    assert admin.delete(f"/api/tokens/{theirs['id']}").status_code == 200
    assert bearer(server, theirs["token"]).get("/api/games").status_code == 401


def test_tokens_that_no_longer_work(setup):
    server, admin, ann = setup
    for body in ({"name": " "}, {"name": "x" * 61}, {"name": "a", "access": "admin"}, {"name": "a", "expires_days": 0}):
        assert ann.post("/api/tokens", json=body).status_code == 400, body
    assert bearer(server, "apm_made-up").get("/api/games").status_code == 401

    # expired
    made = make_token(ann, expires_days=30)
    assert made["expires_at"] == pytest.approx(time.time() + 30 * 86400, abs=60)
    conn = sqlite3.connect(server.data_dir / "manager.sqlite3")
    with conn:
        conn.execute("UPDATE api_tokens SET expires_at = ? WHERE id = ?", (time.time() - 1, made["id"]))
    conn.close()
    r = bearer(server, made["token"]).get("/api/games")
    assert r.status_code == 401 and r.json()["detail"] == "This API token is not valid (any more)"
    assert ann.get("/api/tokens").json()[0]["expired"] is True

    # revoked by its owner, or its owner disabled
    made = make_token(ann)
    assert ann.delete(f"/api/tokens/{made['id']}").status_code == 200
    assert bearer(server, made["token"]).get("/api/games").status_code == 401
    made = make_token(ann)
    ann_id = next(u["id"] for u in admin.get("/api/users").json() if u["username"] == "ann")
    assert admin.patch(f"/api/users/{ann_id}", json={"disabled": True}).status_code == 200
    assert bearer(server, made["token"]).get("/api/games").status_code == 401
    assert admin.patch(f"/api/users/{ann_id}", json={"disabled": False}).status_code == 200
    assert bearer(server, made["token"]).get("/api/games").status_code == 200

    # a reverse proxy's basic auth is not a token: the session goes on as usual, CSRF rule included
    proxied = server.client("boss", "bosspass123")
    proxied.headers["Authorization"] = "Basic dXNlcjpwYXNz"
    assert proxied.get("/api/auth/status").json()["user"]["username"] == "boss"
    del proxied.headers["X-Requested-With"]
    assert proxied.patch("/api/settings", json={"public_host": "y"}).status_code == 403


def test_tokens_per_user_are_limited(setup):
    _server, _admin, ann = setup
    for i in range(25):
        make_token(ann, name=f"t{i}")
    assert ann.post("/api/tokens", json={"name": "one more"}).status_code == 409
