"""Signing in with Discord. A local fake stands in for Discord's OAuth2 and user API."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

from conftest import Server

SITE = "http://manager.test"
CLIENT_ID, SECRET, GUILD = "123456789012345678", "s3cret-client-secret", "999999999999999999"


class FakeDiscord:
    """Hands out tokens for known one-time codes and answers who the token belongs to. Records every request."""

    def __init__(self) -> None:
        self.codes: dict[str, dict] = {}   # code -> account
        self.tokens: dict[str, dict] = {}  # token -> account
        self.requests: list[str] = []
        self.revoked: list[str] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def answer(self, status, body):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                form = {k: v[0] for k, v in parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode()).items()}
                fake.requests.append(f"POST {self.path}")
                if self.path == "/api/oauth2/token/revoke":
                    fake.revoked.append(form.get("token"))
                    return self.answer(200, {})
                if self.path != "/api/oauth2/token":
                    return self.answer(404, {})
                ok = (form.get("client_id") == CLIENT_ID and form.get("client_secret") == SECRET
                      and form.get("redirect_uri") == f"{SITE}/api/auth/discord/callback"
                      and form.get("grant_type") == "authorization_code" and form.get("code") in fake.codes)
                if not ok:
                    return self.answer(400, {"error": "invalid_grant"})
                token = f"token-{len(fake.tokens)}"
                fake.tokens[token] = fake.codes.pop(form["code"])  # a code works once, as at Discord
                return self.answer(200, {"access_token": token, "token_type": "Bearer"})

            def do_GET(self):
                fake.requests.append(f"GET {self.path}")
                account = fake.tokens.get(self.headers.get("Authorization", "").removeprefix("Bearer "))
                if account is None:
                    return self.answer(401, {"message": "401: Unauthorized"})
                if self.path == "/api/users/@me":
                    return self.answer(200, {k: account[k] for k in ("id", "username", "global_name", "avatar")})
                if self.path == "/api/users/@me/guilds":
                    return self.answer(200, [{"id": g, "name": "a server"} for g in account.get("guilds", [])])
                return self.answer(404, {})

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def code_for(self, discord_id: str, username: str, guilds=(), avatar=None) -> str:
        code = f"code-{len(self.codes)}-{discord_id}-{len(self.requests)}"
        self.codes[code] = {"id": discord_id, "username": username, "global_name": username.title(), "guilds": list(guilds),
                            "avatar": avatar}
        return code

    def token_requests(self) -> int:
        return self.requests.count("POST /api/oauth2/token")

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def discord():
    fake = FakeDiscord()
    yield fake
    fake.close()


@pytest.fixture
def site(tmp_path, discord):
    server = Server(tmp_path / "data", ap=False, extra_env={"DISCORD_API_URL": f"{discord.url}/api",
                                                             "DISCORD_AUTHORIZE_URL": f"{discord.url}/oauth2/authorize"})
    server.start()
    admin = server.client()
    assert admin.post("/api/auth/setup", json={"username": "boss", "password": "bosspass123",
                                               "code": server.setup_code()}).status_code == 200
    r = admin.patch("/api/settings", json={"site_url": SITE + "/", "discord_login": True, "discord_client_id": CLIENT_ID,
                                           "discord_client_secret": SECRET})
    assert r.status_code == 200, r.text
    yield server, admin
    server.stop()


def start(client, *, link=False, next_page=""):
    """What the browser does when someone clicks the Discord button: returns the state Discord would send back."""
    if link:
        r = client.post("/api/auth/discord/link")
        assert r.status_code == 200, r.text
        location = r.json()["url"]
    else:
        r = client.get("/api/auth/discord/login", params={"next": next_page}, follow_redirects=False)
        assert r.status_code == 303, r.text
        location = r.headers["location"]
    assert location.startswith(f"{urlsplit(location).scheme}://{urlsplit(location).netloc}/oauth2/authorize?")
    query = parse_qs(urlsplit(location).query)
    assert query["client_id"] == [CLIENT_ID] and query["redirect_uri"] == [f"{SITE}/api/auth/discord/callback"]
    assert SECRET not in location
    return query["state"][0]


def finish(client, state, code=None, **extra):
    """Discord sending the browser back. Returns where the app then sends it."""
    params = {"state": state, **({"code": code} if code else {}), **extra}
    r = client.get("/api/auth/discord/callback", params=params, follow_redirects=False)
    assert r.status_code == 303, r.text
    return r.headers["location"]


def me(client):
    return client.get("/api/auth/status").json()["user"]


def test_settings_keep_the_secret_on_the_server(tmp_path, discord):
    server = Server(tmp_path / "data", ap=False)
    server.start()
    try:
        admin = server.client()
        admin.post("/api/auth/setup", json={"username": "boss", "password": "bosspass123",
                                            "code": server.setup_code()})
        r = admin.patch("/api/settings", json={"discord_login": True})
        assert r.status_code == 400 and "client ID" in r.json()["detail"]
        for bad in ({"discord_client_id": "not-a-number"}, {"discord_guild_id": "12"}, {"discord_client_secret": "a b"}):
            assert admin.patch("/api/settings", json=bad).status_code == 400, bad
        r = admin.patch("/api/settings", json={"site_url": "https://ap.example.org/", "discord_login": True,
                                               "discord_client_id": CLIENT_ID, "discord_client_secret": SECRET})
        assert r.status_code == 200
        settings = admin.get("/api/settings").json()
        assert SECRET not in json.dumps(settings) and settings["discord_client_secret_set"] is True
        assert settings["site_url"] == "https://ap.example.org"
        assert settings["discord_redirect_uri"] == "https://ap.example.org/api/auth/discord/callback"
        status = server.client().get("/api/auth/status").json()
        assert status["discord_login"] is True and status["site_url"] == "https://ap.example.org"
        # leaving the secret out of a later save keeps it
        assert admin.patch("/api/settings", json={"discord_signup": True}).json()["discord_client_secret_set"] is True
        # the state cookie gets the Secure flag when the site is served over https
        r = server.client().get("/api/auth/discord/login", follow_redirects=False)
        assert "secure" in r.headers["set-cookie"].lower() and "httponly" in r.headers["set-cookie"].lower()
    finally:
        server.stop()


def test_unknown_discord_accounts_get_in_only_when_sign_up_is_allowed(site, discord):
    server, admin = site
    admin.post("/api/users", json={"username": "anna", "password": "annapass123", "role": "user"})
    browser = server.client()
    assert "discord_error=not_linked" in finish(browser, start(browser), discord.code_for("11111", "anna"))
    assert me(browser) is None
    assert [u["username"] for u in admin.get("/api/users").json()] == ["anna", "boss"]

    # with sign-up allowed, a new account is made, even though a local "anna" exists: names never link accounts
    admin.patch("/api/settings", json={"discord_signup": True})
    assert finish(browser, start(browser), discord.code_for("11111", "anna")) == "/#/?discord=welcome"
    user = me(browser)
    assert user["username"] == "anna-2" and user["role"] == "user" and user["discord_name"] == "Anna"
    assert user["has_password"] is False
    anna = next(u for u in admin.get("/api/users").json() if u["username"] == "anna")
    assert anna["discord_name"] is None and anna["has_password"] is True
    # an account without a password cannot be entered with a password, not even an empty one
    for password in ("", "anything"):
        assert server.client().post("/api/auth/login", json={"username": "anna-2", "password": password}).status_code == 401
    # signing in again finds the same account
    again = server.client()
    assert finish(again, start(again), discord.code_for("11111", "anna")) == "/#/"
    assert me(again)["username"] == "anna-2"
    assert len(discord.revoked) == discord.token_requests() == 3  # tokens are not kept around


def test_callbacks_this_browser_did_not_start_are_refused(site, discord):
    server, admin = site
    admin.patch("/api/settings", json={"discord_signup": True})
    victim, attacker = server.client(), server.client()

    # no state cookie at all (a link someone was sent): refused before Discord is even asked
    state = start(attacker)
    assert "discord_error=expired" in finish(victim, state, discord.code_for("22222", "mallory"))
    assert me(victim) is None and discord.token_requests() == 0
    # the state was spent by that attempt, so the attacker cannot use it either
    assert "discord_error=expired" in finish(attacker, state, discord.code_for("22222", "mallory"))
    assert discord.token_requests() == 0

    # unknown or reused states are refused too
    assert "discord_error=expired" in finish(victim, "made-up-state", discord.code_for("22222", "mallory"))
    state = start(victim)
    finish(victim, state, discord.code_for("33333", "vic"))
    assert me(victim)["username"] == "vic"
    assert "discord_error=expired" in finish(victim, state, discord.code_for("33333", "vic"))

    # cancelling at Discord, and odd answers, end up as a message, not an error page
    state = start(attacker)
    assert "discord_error=denied" in finish(attacker, state, error="access_denied")
    state = start(attacker)
    assert "discord_error=discord" in finish(attacker, state, "a-code-discord-does-not-know")


def test_the_page_after_signing_in_stays_on_this_site(site, discord):
    server, admin = site
    admin.patch("/api/settings", json={"discord_signup": True})
    for wanted, landing in (("#/games/3", "/#/games/3"), ("https://evil.example/", "/#/"), ("//evil.example", "/#/"),
                            ("#/x?y=<script>", "/#/"), ("javascript:alert(1)", "/#/")):
        browser = server.client()
        finish(browser, start(browser), discord.code_for("44444", "pat"))  # first sign-in creates the account
        browser = server.client()
        assert finish(browser, start(browser, next_page=wanted), discord.code_for("44444", "pat")) == landing, wanted


def test_linking_is_only_for_the_signed_in_user(site, discord):
    server, admin = site
    for name in ("anna", "ben"):
        admin.post("/api/users", json={"username": name, "password": f"{name}pass123", "role": "user"})
    anna, ben = server.client("anna", "annapass123"), server.client("ben", "benpass123")

    assert finish(anna, start(anna, link=True), discord.code_for("55555", "annad")) == "/#/?discord=linked"
    assert me(anna)["discord_name"] == "Annad"
    # the same Discord account cannot be linked to a second user
    assert "discord_error=taken" in finish(ben, start(ben, link=True), discord.code_for("55555", "annad"))
    assert me(ben)["discord_name"] is None
    # a link started by one user cannot be finished in someone else's session
    state = start(ben, link=True)
    ben.post("/api/auth/logout")
    assert ben.post("/api/auth/login", json={"username": "anna", "password": "annapass123"}).status_code == 200
    assert "discord_error=expired" in finish(ben, state, discord.code_for("66666", "bend"))
    assert me(ben)["discord_name"] == "Annad"  # anna's link is unchanged
    # without being signed in, nothing can be linked
    assert server.client().post("/api/auth/discord/link").status_code == 401

    browser = server.client()
    finish(browser, start(browser), discord.code_for("55555", "annad"))
    assert me(browser)["username"] == "anna"
    assert anna.post("/api/auth/discord/unlink").json()["discord_name"] is None
    browser = server.client()
    assert "discord_error=not_linked" in finish(browser, start(browser), discord.code_for("55555", "annad"))


def test_the_discord_picture_comes_along(site, discord):
    server, admin = site
    admin.patch("/api/settings", json={"discord_signup": True})
    picture, discord_id = "a_" + "0123456789abcdef" * 2, "88888888888888888"
    browser = server.client()
    finish(browser, start(browser), discord.code_for(discord_id, "pic", avatar=picture))
    assert me(browser)["discord_avatar"] == f"https://cdn.discordapp.com/avatars/{discord_id}/{picture}.png?size=64"
    # each sign-in brings the current one; without a picture of their own, Discord's default one for the account
    browser = server.client()
    finish(browser, start(browser), discord.code_for(discord_id, "pic"))
    default = f"https://cdn.discordapp.com/embed/avatars/{(int(discord_id) >> 22) % 6}.png"
    assert me(browser)["discord_avatar"] == default
    # what is not the name of a picture never becomes part of an address
    browser = server.client()
    finish(browser, start(browser), discord.code_for(discord_id, "pic", avatar="../../evil.example/x"))
    assert me(browser)["discord_avatar"] == default
    # the page may show pictures from Discord's image server, and from nowhere else outside this site
    assert "img-src 'self' data: https://cdn.discordapp.com;" in browser.get("/").headers["content-security-policy"]
    # without the link, no picture
    assert browser.post("/api/auth/password", json={"new_password": "picpass1234"}).status_code == 200
    assert browser.post("/api/auth/discord/unlink").json()["discord_avatar"] is None
    assert [u["discord_avatar"] for u in admin.get("/api/users").json() if u["username"] == "pic"] == [None]


def test_discord_only_accounts_set_a_password_before_unlinking(site, discord):
    server, admin = site
    admin.patch("/api/settings", json={"discord_signup": True})
    browser = server.client()
    finish(browser, start(browser), discord.code_for("77777", "solo"))
    assert browser.post("/api/auth/discord/unlink").status_code == 409  # it would lock them out
    assert browser.post("/api/auth/password", json={"new_password": "solopass123"}).status_code == 200
    assert browser.post("/api/auth/password", json={"new_password": "another123"}).status_code == 403  # now it asks
    assert browser.post("/api/auth/discord/unlink").status_code == 200
    assert server.client("solo", "solopass123")


def test_disabled_users_and_server_membership(site, discord):
    server, admin = site
    admin.patch("/api/settings", json={"discord_signup": True})
    browser = server.client()
    finish(browser, start(browser), discord.code_for("88888", "gone"))
    user_id = me(browser)["id"]
    admin.patch(f"/api/users/{user_id}", json={"disabled": True})
    assert me(browser) is None  # the session ended
    assert "discord_error=disabled_user" in finish(browser, start(browser), discord.code_for("88888", "gone"))
    admin.patch(f"/api/users/{user_id}", json={"disabled": False})

    admin.patch("/api/settings", json={"discord_guild_id": GUILD})
    assert "discord_error=not_member" in finish(browser, start(browser), discord.code_for("88888", "gone", guilds=["1234567"]))
    assert "discord_error=not_member" in finish(browser, start(browser), discord.code_for("99999", "stranger"))
    assert len([u for u in admin.get("/api/users").json() if u["username"] == "stranger"]) == 0
    assert finish(browser, start(browser), discord.code_for("88888", "gone", guilds=[GUILD])) == "/#/"
    assert me(browser)["id"] == user_id

    # an admin can unlink an account, which also ends its sessions
    assert admin.patch(f"/api/users/{user_id}", json={"unlink_discord": True}).json()["discord_name"] is None
    assert me(browser) is None


def test_discord_sign_in_is_off_unless_set_up(plain_server):
    client = plain_server.client()
    r = client.get("/api/auth/discord/login", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/#/?discord_error=disabled"
    assert client.get("/api/auth/status").json()["discord_login"] is False
    r = client.get("/api/auth/discord/callback", params={"state": "x", "code": "y"}, follow_redirects=False)
    assert r.headers["location"] == "/#/?discord_error=expired"
