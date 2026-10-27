"""Accounts, sessions, permissions and the database. Runs without Archipelago."""
import pytest


def login(server, name, password):
    client = server.client()
    return client, client.post("/api/auth/login", json={"username": name, "password": password})


def setup_admin(server):
    c = server.client()
    r = c.post("/api/auth/setup", json={"username": "boss", "password": "bosspass123", "code": server.setup_code()})
    assert r.status_code == 200, r.text
    return c


def test_first_run_setup_flow(plain_server):
    c = plain_server.client()
    status = c.get("/api/auth/status").json()
    assert status["needs_setup"] is True and status["user"] is None
    assert c.get("/api/games").status_code == 401

    code = plain_server.setup_code()
    assert c.post("/api/auth/setup", json={"username": "a", "password": "bosspass123", "code": code}
                  ).status_code == 400  # name too short
    assert c.post("/api/auth/setup", json={"username": "boss", "password": "short", "code": code}).status_code == 400
    r = c.post("/api/auth/setup", json={"username": "boss", "password": "bosspass123", "code": code})
    assert r.status_code == 200 and r.json()["role"] == "admin"
    assert c.get("/api/auth/status").json()["user"]["username"] == "boss"
    # setup is a one-time door
    assert plain_server.client().post("/api/auth/setup", json={"username": "evil", "password": "evilpass123"}).status_code == 409


def test_state_changing_calls_need_the_csrf_header(plain_server):
    setup_admin(plain_server)
    import httpx
    bare = httpx.Client(base_url=plain_server.base_url)
    r = bare.post("/api/auth/login", json={"username": "boss", "password": "bosspass123"})
    assert r.status_code == 403
    assert bare.get("/api/auth/status").status_code == 200  # reads are fine


def test_session_cookie_flags(plain_server):
    c = plain_server.client()
    r = c.post("/api/auth/setup", json={"username": "boss", "password": "bosspass123", "code": plain_server.setup_code()})
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie


def test_wrong_password_and_throttling(plain_server):
    setup_admin(plain_server)
    for _ in range(8):
        _c, r = login(plain_server, "boss", "nope")
        assert r.status_code == 401
    _c, r = login(plain_server, "boss", "bosspass123")  # now locked out even with the right password
    assert r.status_code == 429


def test_logout_invalidates_the_session(plain_server):
    c = setup_admin(plain_server)
    assert c.post("/api/auth/logout", json={}).status_code == 200
    assert c.get("/api/games").status_code == 401


def test_registration_is_off_by_default_and_can_be_enabled(plain_server):
    admin = setup_admin(plain_server)
    anon = plain_server.client()
    assert anon.post("/api/auth/register", json={"username": "newbie", "password": "newbiepass1"}).status_code == 403
    assert admin.patch("/api/settings", json={"allow_registration": True}).status_code == 200
    assert anon.get("/api/auth/status").json()["allow_registration"] is True
    r = anon.post("/api/auth/register", json={"username": "newbie", "password": "newbiepass1"})
    assert r.status_code == 200 and r.json()["role"] == "user"
    assert anon.post("/api/auth/register", json={"username": "NEWBIE", "password": "newbiepass1"}).status_code == 409  # case-insensitive


def test_regular_users_cannot_use_admin_endpoints(plain_server):
    admin = setup_admin(plain_server)
    admin.post("/api/users", json={"username": "plain", "password": "plainpass1"})
    user = plain_server.client("plain", "plainpass1")
    for method, path in [("get", "/api/users"), ("get", "/api/settings"), ("get", "/api/system"), ("get", "/api/host-yaml")]:
        assert getattr(user, method)(path).status_code == 403, path
    assert user.post("/api/users", json={"username": "x1", "password": "xxxxxxxx1"}).status_code == 403
    assert user.patch("/api/settings", json={"allow_registration": True}).status_code == 403


def test_last_admin_is_protected(plain_server):
    admin = setup_admin(plain_server)
    me = admin.get("/api/auth/status").json()["user"]["id"]
    assert admin.patch(f"/api/users/{me}", json={"role": "user"}).status_code == 409
    assert admin.patch(f"/api/users/{me}", json={"disabled": True}).status_code == 409
    assert admin.delete(f"/api/users/{me}").status_code == 409
    second = admin.post("/api/users", json={"username": "second", "password": "secondpass1", "role": "admin"}).json()
    assert admin.patch(f"/api/users/{second['id']}", json={"role": "user"}).status_code == 200  # fine while boss remains


def test_disabling_a_user_ends_their_sessions(plain_server):
    admin = setup_admin(plain_server)
    u = admin.post("/api/users", json={"username": "temp", "password": "temppass12"}).json()
    user = plain_server.client("temp", "temppass12")
    assert user.get("/api/games").status_code == 200
    admin.patch(f"/api/users/{u['id']}", json={"disabled": True})
    assert user.get("/api/games").status_code == 401
    assert plain_server.client().post("/api/auth/login", json={"username": "temp", "password": "temppass12"}).status_code == 401
    admin.patch(f"/api/users/{u['id']}", json={"disabled": False})
    assert plain_server.client("temp", "temppass12").get("/api/games").status_code == 200


def test_password_change_keeps_this_session_but_drops_others(plain_server):
    admin = setup_admin(plain_server)
    admin.post("/api/users", json={"username": "pw", "password": "oldpassword1"})
    a = plain_server.client("pw", "oldpassword1")
    b = plain_server.client("pw", "oldpassword1")
    assert a.post("/api/auth/password", json={"current_password": "wrong", "new_password": "newpassword1"}).status_code == 403
    assert a.post("/api/auth/password", json={"current_password": "oldpassword1", "new_password": "short"}).status_code == 400
    assert a.post("/api/auth/password", json={"current_password": "oldpassword1", "new_password": "newpassword1"}).status_code == 200
    assert a.get("/api/games").status_code == 200
    assert b.get("/api/games").status_code == 401
    assert plain_server.client("pw", "newpassword1").get("/api/games").status_code == 200


def test_admin_password_reset_and_delete(plain_server):
    admin = setup_admin(plain_server)
    u = admin.post("/api/users", json={"username": "victim", "password": "victimpass1"}).json()
    user = plain_server.client("victim", "victimpass1")
    admin.patch(f"/api/users/{u['id']}", json={"password": "resetpassword1"})
    assert user.get("/api/games").status_code == 401
    assert plain_server.client("victim", "resetpassword1").get("/api/games").status_code == 200
    assert admin.delete(f"/api/users/{u['id']}").status_code == 200
    assert admin.delete(f"/api/users/{u['id']}").status_code == 404


def test_username_rules(plain_server):
    admin = setup_admin(plain_server)
    for bad in ["x", "a" * 33, "bad/name", "semi;colon", "<script>", "★★", "a\u200bb", "\u202eabc", "..", "- -", "__"]:
        assert admin.post("/api/users", json={"username": bad, "password": "password123"}).status_code == 400, bad
    assert "letter or digit" in admin.post("/api/users", json={"username": "-.-", "password": "password123"}).json()["detail"]
    for good in ["Good Name_1.x", "名前", "Ölfeld"]:
        assert admin.post("/api/users", json={"username": good, "password": "password123"}).status_code == 200, good
    # one alphabet per name, so none can pass for another: "аdmin" with a Cyrillic а, a fullwidth Ａ, a bold 𝐀
    for mixed in ["аdmin", "Ivanов", "Ａdmin", "𝐀dmin", "Mario名前", "Ελένη Smith"]:
        r = admin.post("/api/users", json={"username": mixed, "password": "password123"})
        assert r.status_code == 400 and "one alphabet" in r.json()["detail"], mixed
    # names in other alphabets, and Japanese, Korean and Chinese ones written the way they are, with digits too
    for name in ["Иван", "Ελένη", "佐々木", "やまだ太郎", "カタカナー", "김민준", "王小明", "דוד", "Иван_2"]:
        assert admin.post("/api/users", json={"username": name, "password": "password123"}).status_code == 200, name
    # é typed as e and a combining accent is the same é, when the account is made and when signing in
    r = admin.post("/api/users", json={"username": "Rene\u0301e", "password": "password123"})
    assert r.status_code == 200 and r.json()["username"] == "Renée"
    for typed in ("Renée", "Rene\u0301e"):
        assert plain_server.client(typed, "password123").get("/api/auth/status").json()["user"]["username"] == "Renée"
    assert admin.post("/api/users", json={"username": "Rene\u0301e", "password": "password123"}).status_code == 409


def test_env_bootstrap_admin(tmp_path):
    from conftest import Server
    server = Server(tmp_path / "data", ap=False, extra_env={"ADMIN_USER": "envadmin", "ADMIN_PASSWORD": "envpassword1"})
    server.start()
    try:
        assert server.client().get("/api/auth/status").json()["needs_setup"] is False
        assert server.client("envadmin", "envpassword1").get("/api/users").status_code == 200
    finally:
        server.stop()


def test_settings_validation(plain_server):
    admin = setup_admin(plain_server)
    assert admin.patch("/api/settings", json={"default_server_options": {"hint_cost": 500}}).status_code == 400
    assert admin.patch("/api/settings", json={"public_host": "bad host/"}).status_code == 400
    r = admin.patch("/api/settings", json={"public_host": "games.example.org", "default_server_options": {"hint_cost": 25}})
    assert r.status_code == 200 and r.json()["default_server_options"]["hint_cost"] == 25
    assert admin.get("/api/settings").json()["public_host"] == "games.example.org"


def test_security_headers_and_static_ui(plain_server):
    c = plain_server.client()
    r = c.get("/")
    assert r.status_code == 200 and "Archipelago Manager" in r.text
    assert r.headers["x-frame-options"] == "DENY" and "default-src 'self'" in r.headers["content-security-policy"]
    assert c.get("/api/health").headers["cache-control"] == "no-store"
    assert c.get("/js/app.js").status_code == 200
    assert c.get("/..%2f..%2fetc/passwd").status_code in (400, 404)


def test_host_yaml_editor_requires_a_working_archipelago(plain_server):
    admin = setup_admin(plain_server)
    r = admin.get("/api/host-yaml")
    assert r.status_code == 503  # no Archipelago here, and the message says so rather than failing obscurely
    assert "Archipelago" in r.json()["detail"]


def test_rotating_usernames_does_not_bypass_the_login_limit(plain_server):
    setup_admin(plain_server)
    statuses = [login(plain_server, f"guess{i}", "wrong")[1].status_code for i in range(45)]
    assert statuses[:40] == [401] * 40 and set(statuses[40:]) == {429}  # per-address limit, whatever the username
    assert login(plain_server, "boss", "bosspass123")[1].status_code == 429


def test_oversized_uploads_are_rejected_before_they_are_stored(tmp_path):
    from conftest import Server
    server = Server(tmp_path / "data", ap=False, extra_env={"MAX_UPLOAD_MB": "1"})
    server.start()
    try:
        admin = server.client()
        admin.post("/api/auth/setup", json={"username": "boss", "password": "bosspass123", "code": server.setup_code()})
        big = b"x" * (3 * 1024 * 1024)
        r = admin.post("/api/apworlds", files={"file": ("big.apworld", big)})  # upload endpoint: 1 MB + 1 MB overhead
        assert r.status_code == 413 and "too large" in r.json()["detail"]
        r = admin.post("/api/games", files={"file": ("big.zip", big)})
        assert r.status_code == 413
        r = admin.post("/api/configs", files=[("files", ("big.yaml", b"x" * (17 * 1024 * 1024)))])  # ordinary endpoint
        assert r.status_code == 413
        assert not list((tmp_path / "data" / "tmp").glob("*"))  # nothing was left behind
        assert admin.get("/api/auth/status").status_code == 200  # the server is fine
    finally:
        server.stop()


def test_a_database_from_the_first_version_is_upgraded(tmp_path):
    """Schema v1 had no single_claim column, no Discord accounts, no errors per player file, no progress history and
    none of the later tables; a newer manager adds them, keeping the data."""
    import sqlite3
    from conftest import Server
    server = Server(tmp_path / "data", ap=False)
    server.start()
    setup_admin(server)
    server.stop()
    db_path = tmp_path / "data" / "manager.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO games(name, created_at, options) VALUES ('Old game', 0, '{}')")
    conn.execute("ALTER TABLE games DROP COLUMN single_claim")  # what a v1 database looks like
    conn.execute("DROP TABLE signup_entries")
    conn.execute("DROP TABLE signups")
    conn.execute("DROP TABLE apworld_sources")
    conn.execute("DROP INDEX users_discord")
    conn.execute("ALTER TABLE users DROP COLUMN discord_id")
    conn.execute("ALTER TABLE users DROP COLUMN discord_name")
    conn.execute("ALTER TABLE users DROP COLUMN discord_pings")
    conn.execute("ALTER TABLE users DROP COLUMN discord_avatar")
    conn.execute("ALTER TABLE games DROP COLUMN discord_webhook_url")
    conn.execute("ALTER TABLE configs DROP COLUMN shared_at")
    for column in ("start_at", "start_open_minutes", "start_countdown", "start_stage"):
        conn.execute(f"ALTER TABLE games DROP COLUMN {column}")
    conn.execute("ALTER TABLE game_slots DROP COLUMN goal_logged_at")
    conn.execute("DROP TABLE audit_log")
    conn.execute("DROP TABLE game_visits")
    for column in ("player_status", "status_note", "status_at", "handover_at", "handover_to", "handover_note"):
        conn.execute(f"ALTER TABLE game_slots DROP COLUMN {column}")
    conn.execute("DROP TABLE api_tokens")
    conn.execute("DROP TABLE generation_presets")
    conn.execute("DROP TABLE ap_versions")
    conn.execute("ALTER TABLE games DROP COLUMN ap_version")
    conn.execute("ALTER TABLE games DROP COLUMN stopped_at")
    conn.execute("ALTER TABLE games DROP COLUMN asleep")
    conn.execute("ALTER TABLE job_players DROP COLUMN sha256")
    conn.execute("ALTER TABLE job_players DROP COLUMN error")
    conn.execute("DROP TABLE game_progress")
    for column in ("play_started_at", "finished_at", "share_spoiler_when_done"):
        conn.execute(f"ALTER TABLE games DROP COLUMN {column}")
    conn.execute("ALTER TABLE game_slots DROP COLUMN goal_at")
    conn.execute("DROP INDEX games_spectate")
    for column in ("spectate_token", "spectate_activity", "remind_days"):
        conn.execute(f"ALTER TABLE games DROP COLUMN {column}")
    for column in ("player_since", "reminded_at", "reminders"):
        conn.execute(f"ALTER TABLE game_slots DROP COLUMN {column}")
    conn.execute("PRAGMA user_version = 1")
    conn.commit()
    conn.close()

    server.start()
    try:
        admin = server.client("boss", "bosspass123")  # users survived
        games = admin.get("/api/games").json()
        assert [(g["name"], g["single_claim"], g["finished_at"]) for g in games] == [("Old game", False, None)]
        conn = sqlite3.connect(db_path)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 21
        conn.close()
        # tables added since then exist and work
        r = admin.post("/api/signups", json={"name": "After the upgrade"})
        assert r.status_code == 200 and admin.get("/api/signups").json()[0]["name"] == "After the upgrade"
        assert r.json()["test"] is None
        assert admin.get(f"/api/games/{games[0]['id']}/history").json()["slots"] == []
        assert admin.get(f"/api/games/{games[0]['id']}").json()["discord_webhook_url"] == ""
        assert admin.get(f"/api/games/{games[0]['id']}").json()["start"] is None
        assert admin.get(f"/api/games/{games[0]['id']}").json()["state"] == "stopped"
        assert admin.post(f"/api/games/{games[0]['id']}/visit", json={}).json() == {"summary": None}
        assert [e["action"] for e in admin.get("/api/audit").json()["entries"]] == ["signup.add"]
        assert admin.get("/api/apworld-sources").json() == []
        assert admin.get("/api/users").json()[0]["discord_name"] is None
        assert admin.get("/api/users").json()[0]["discord_pings"] == []
        assert admin.get("/api/configs/shared").json() == []
        assert admin.get("/api/presets").json()["presets"] == []
        # the users table was built anew for the organizer role, with everything that refers to it
        boss = admin.get("/api/auth/status").json()["user"]
        assert boss["username"] == "boss" and boss["role"] == "admin"
        r = admin.post("/api/users", json={"username": "planner", "password": "password123", "role": "organizer"})
        assert r.status_code == 200 and r.json()["role"] == "organizer" and r.json()["id"] > boss["id"]
    finally:
        server.stop()


def test_entries_from_before_slots_were_counted_are_counted(tmp_path):
    """Schema v20 limits a sign-up round's slots per player instead of its files. Entries made before have no count:
    the manager counts the players in their files when it starts."""
    import sqlite3
    from conftest import Server
    from helpers import player_yaml, upload_yaml
    server = Server(tmp_path / "data", ap=False)
    server.start()
    try:
        admin = setup_admin(server)
        sid = admin.post("/api/signups", json={"name": "Old round", "slots_per_user": 3}).json()["id"]
        pair = upload_yaml(admin, "pair", player_yaml("One") + "---\n" + player_yaml("Two")).json()["created"][0]["id"]
        assert admin.post(f"/api/signups/{sid}/entries", json={"config_id": pair}).status_code == 200
    finally:
        server.stop()
    conn = sqlite3.connect(tmp_path / "data" / "manager.sqlite3")
    conn.execute("ALTER TABLE signup_entries DROP COLUMN slots")  # what a v19 database looks like
    conn.execute("ALTER TABLE signups RENAME COLUMN slots_per_user TO configs_per_user")
    conn.execute("ALTER TABLE users DROP COLUMN discord_avatar")  # added after v19, in v21
    conn.execute("PRAGMA user_version = 19")
    conn.commit()
    conn.close()

    server.start()
    try:
        signup = server.client("boss", "bosspass123").get(f"/api/signups/{sid}").json()
        assert signup["slots_per_user"] == 3 and signup["my_slot_count"] == 2
        assert [e["slots"] for e in signup["entries"]] == [2]
    finally:
        server.stop()


def test_a_database_from_a_newer_version_is_not_touched(tmp_path):
    import sqlite3
    from conftest import Server
    server = Server(tmp_path / "data", ap=False)
    server.start()
    server.stop()
    conn = sqlite3.connect(tmp_path / "data" / "manager.sqlite3")
    conn.execute("PRAGMA user_version = 99")
    conn.commit()
    conn.close()
    with pytest.raises(RuntimeError, match="exited early"):
        server.start()
    assert "Refusing to downgrade" in server.log()
