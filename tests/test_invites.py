"""Invite links and password-reset links (invites.py): what an administrator hands out instead of an account or a
password. Runs without Archipelago."""
import sqlite3
import time

import httpx

from test_auth import setup_admin


def db(server):
    return sqlite3.connect(server.data_dir / "manager.sqlite3")


def stored_anywhere(server, token: str) -> bool:
    """Whether a token can be read from the data folder's database, its write-ahead log included."""
    return any(token.encode() in path.read_bytes() for path in server.data_dir.glob("manager.sqlite3*"))


def actions(admin, action):
    return [(e["username"], e["target"]["name"] if e["target"] else None, e["details"])
            for e in admin.get("/api/audit", params={"action": action}).json()["entries"]]


def test_an_invite_link_makes_accounts_until_it_is_used_up(plain_server):
    admin = setup_admin(plain_server)
    r = admin.post("/api/users/invites", json={"role": "organizer", "max_uses": 2, "days": 7, "note": "  Friday   group "})
    assert r.status_code == 200, r.text
    token, invite = r.json()["token"], r.json()["invite"]
    assert len(token) >= 40
    assert (invite["role"], invite["note"], invite["uses"], invite["max_uses"], invite["state"], invite["created_by"]) == \
        ("organizer", "Friday group", 0, 2, "active", "boss")
    assert 7 * 86400 - 60 < invite["expires_at"] - time.time() <= 7 * 86400
    assert not stored_anywhere(plain_server, token)  # only a hash of it is kept

    # registration is closed, and the link opens it anyway, for the role it gives
    anon = plain_server.client()
    assert anon.post("/api/auth/register", json={"username": "frida", "password": "fridapass1"}).status_code == 403
    info = anon.post("/api/auth/invite", json={"token": token}).json()
    assert (info["role"], info["note"], info["invited_by"], info["discord"]) == ("organizer", "Friday group", "boss", False)
    r = anon.post("/api/auth/invite/join", json={"token": token, "username": "frida", "password": "fridapass1"})
    assert r.status_code == 200 and (r.json()["username"], r.json()["role"]) == ("frida", "organizer")
    assert anon.get("/api/auth/status").json()["user"]["username"] == "frida"  # signed in

    # a name that is taken or a password that is too short does not spend a use
    other = plain_server.client()
    assert other.post("/api/auth/invite/join", json={"token": token, "username": "FRIDA", "password": "password12"}).status_code == 409
    assert other.post("/api/auth/invite/join", json={"token": token, "username": "greta", "password": "short"}).status_code == 400
    assert admin.get("/api/users/invites").json()[0]["uses"] == 1
    assert other.post("/api/auth/invite/join", json={"token": token, "username": "greta", "password": "gretapass1"}).status_code == 200
    used = admin.get("/api/users/invites").json()[0]
    assert (used["uses"], used["state"], used["joined"]) == (2, "used_up", ["frida", "greta"])

    # used up: the link shows nothing and makes nothing more
    late = plain_server.client()
    assert late.post("/api/auth/invite", json={"token": token}).status_code == 410
    r = late.post("/api/auth/invite/join", json={"token": token, "username": "hugo", "password": "hugopass12"})
    assert r.status_code == 410 and "used up" in r.json()["detail"]
    assert late.post("/api/auth/invite", json={"token": "x" * 43}).status_code == 410

    # the administrators' log says who made it and who joined with it
    assert actions(admin, "user.invite") == [("boss", "Friday group", {"role": "organizer", "uses": 2, "days": 7})]
    assert actions(admin, "user.join") == [("greta", "greta", {"role": "organizer", "invite": "Friday group"}),
                                            ("frida", "frida", {"role": "organizer", "invite": "Friday group"})]

    # revoked: the link stops working, the accounts it made stay
    second = admin.post("/api/users/invites", json={}).json()  # one regular user, for a week
    assert (second["invite"]["role"], second["invite"]["max_uses"]) == ("user", 1)
    assert admin.delete(f"/api/users/invites/{second['invite']['id']}").status_code == 200
    assert admin.delete(f"/api/users/invites/{second['invite']['id']}").status_code == 404
    assert late.post("/api/auth/invite", json={"token": second["token"]}).status_code == 410
    assert admin.delete(f"/api/users/invites/{invite['id']}").status_code == 200
    assert {u["username"] for u in admin.get("/api/users").json()} == {"boss", "frida", "greta"}
    assert actions(admin, "user.invite_delete")[0][0] == "boss"


def test_what_an_invite_can_be(plain_server):
    admin = setup_admin(plain_server)
    for body in ({"role": "admin"}, {"max_uses": 0}, {"max_uses": 101}, {"days": 3}, {"note": "x" * 101}):
        assert admin.post("/api/users/invites", json=body).status_code == 400, body

    # only administrators make invites, and not with an API token, which must not be able to make accounts
    admin.post("/api/users", json={"username": "olga", "password": "olgapass12", "role": "organizer"})
    olga = plain_server.client("olga", "olgapass12")
    assert olga.post("/api/users/invites", json={}).status_code == 403
    assert olga.get("/api/users/invites").status_code == 403
    token = admin.post("/api/tokens", json={"name": "bot", "access": "write"}).json()["token"]
    bot = httpx.Client(base_url=plain_server.base_url, headers={"Authorization": f"Bearer {token}"})
    assert bot.post("/api/users/invites", json={}).status_code == 403
    invite = admin.post("/api/users/invites", json={}).json()
    assert bot.post("/api/auth/invite/join", json={"token": invite["token"], "username": "botty", "password": "bottypass1"}
                    ).status_code == 403

    # expired: nothing more, and the list says so
    with db(plain_server) as conn:
        conn.execute("UPDATE invites SET expires_at = ?", (time.time() - 1,))
    assert plain_server.client().post("/api/auth/invite", json={"token": invite["token"]}).status_code == 410
    assert admin.get("/api/users/invites").json()[0]["state"] == "expired"


def test_a_reset_link_sets_a_new_password_once(plain_server):
    admin = setup_admin(plain_server)
    victim = admin.post("/api/users", json={"username": "vic", "password": "forgotten12"}).json()
    session = plain_server.client("vic", "forgotten12")
    api_token = session.post("/api/tokens", json={"name": "bot", "access": "read"}).json()["token"]

    r = admin.post(f"/api/users/{victim['id']}/reset-link")
    assert r.status_code == 200, r.text
    token = r.json()["token"]
    assert 24 * 3600 - 60 < r.json()["expires_at"] - time.time() <= 24 * 3600
    assert not stored_anywhere(plain_server, token)

    anon = plain_server.client()
    assert anon.post("/api/auth/reset", json={"token": token}).json() == {"username": "vic"}
    assert anon.post("/api/auth/reset/use", json={"token": token, "password": "short"}).status_code == 400  # not spent
    r = anon.post("/api/auth/reset/use", json={"token": token, "password": "rememberme1"})
    assert r.status_code == 200 and r.json()["username"] == "vic" and r.json()["tokens_revoked"] == 1
    assert anon.get("/api/auth/status").json()["user"]["username"] == "vic"
    # signed out everywhere else, and the tokens whoever knew the old password could have made are gone
    assert session.get("/api/games").status_code == 401
    assert httpx.get(f"{plain_server.base_url}/api/games", headers={"Authorization": f"Bearer {api_token}"}).status_code == 401
    assert plain_server.client().post("/api/auth/login", json={"username": "vic", "password": "forgotten12"}).status_code == 401
    plain_server.client("vic", "rememberme1")
    # once only
    assert anon.post("/api/auth/reset", json={"token": token}).status_code == 410
    assert anon.post("/api/auth/reset/use", json={"token": token, "password": "another123"}).status_code == 410

    # a new link ends the one before
    first = admin.post(f"/api/users/{victim['id']}/reset-link").json()["token"]
    second = admin.post(f"/api/users/{victim['id']}/reset-link").json()["token"]
    assert anon.post("/api/auth/reset", json={"token": first}).status_code == 410
    assert anon.post("/api/auth/reset", json={"token": second}).status_code == 200
    # so does a password set some other way, and disabling the account; a disabled account gets none
    admin.patch(f"/api/users/{victim['id']}", json={"password": "byadmin123"})
    assert anon.post("/api/auth/reset", json={"token": second}).status_code == 410
    third = admin.post(f"/api/users/{victim['id']}/reset-link").json()["token"]
    admin.patch(f"/api/users/{victim['id']}", json={"disabled": True})
    assert admin.post(f"/api/users/{victim['id']}/reset-link").status_code == 409
    admin.patch(f"/api/users/{victim['id']}", json={"disabled": False})
    assert anon.post("/api/auth/reset", json={"token": third}).status_code == 410

    # only administrators make reset links
    assert plain_server.client("vic", "byadmin123").post(f"/api/users/{victim['id']}/reset-link").status_code == 403
    assert admin.post("/api/users/9999/reset-link").status_code == 404
    assert actions(admin, "user.reset_link")[0][:2] == ("boss", "vic")
    assert actions(admin, "user.password_reset") == [("vic", "vic", {"tokens_revoked": 1})]


def test_guessing_link_tokens_is_cut_short(plain_server):
    setup_admin(plain_server)
    guesser = plain_server.client()
    statuses = [guesser.post("/api/auth/invite", json={"token": f"guess-{n:040d}"}).status_code for n in range(40)]
    assert set(statuses) == {410}
    assert guesser.post("/api/auth/reset", json={"token": "y" * 43}).status_code == 429
    assert guesser.post("/api/auth/login", json={"username": "boss", "password": "bosspass123"}).status_code == 429
