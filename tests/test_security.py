"""Fixes from a security review: what strangers can send, what logs reveal, and work one request can cause."""
import zipfile

import httpx
import pytest

from archipelago_manager.generation import JobRequestError, clean_meta
from archipelago_manager.rooms import mask_secrets
from archipelago_manager.routes.apworlds import InvalidAPWorld, inspect_apworld


def test_passwords_never_reach_the_log():
    # MultiServer repeats every /option it is given, and the manager sets both passwords at each start
    assert mask_secrets("Set option server_password to hunter2") == "Set option server_password to ********"
    assert mask_secrets("Set option password to letmein") == "Set option password to ********"
    assert mask_secrets("> /option server_password hunter2") == "> /option server_password ********"
    assert mask_secrets("Notice (all): Bob: !admin login hunter2") == "Notice (all): Bob: !admin login ********"
    assert mask_secrets("Set option hint_cost to 10") == "Set option hint_cost to 10"


def test_meta_files_cannot_expand_through_merge_keys():
    bomb = "a: &a [x, x]\n" + "".join(f"l{i}: &l{i}\n  <<: *{'a' if i == 0 else f'l{i - 1}'}\n  k{i}: 1\n"
                                      for i in range(5)) + "meta_description: x\n"
    with pytest.raises(JobRequestError):
        clean_meta(bomb)
    assert clean_meta("meta_description: Friday\nnull:\n  progression_balancing: 50\n")


def test_archives_with_too_many_files_are_refused_before_they_are_read(tmp_path):
    def apworld(name, files):
        with zipfile.ZipFile(tmp_path / name, "w") as zf:
            zf.writestr("many/archipelago.json", '{"game": "Many"}')
            zf.writestr("many/__init__.py", "")
            for i in range(files):
                zf.writestr(f"many/f{i}", "")
        return tmp_path / name

    with pytest.raises(InvalidAPWorld, match="too many files"):
        inspect_apworld(apworld("many.apworld", 20001))
    assert inspect_apworld(apworld("fine.apworld", 50))["game"] == "Many"


def test_strangers_cannot_send_large_bodies(plain_server):
    bare = httpx.Client(base_url=plain_server.base_url, headers={"X-Requested-With": "archipelago-manager"})
    r = bare.post("/api/apworlds", files={"file": ("big.apworld", b"x" * (2 * 1024 * 1024))})
    assert r.status_code == 413  # refused while still arriving, not after storing it and then asking who is there
    assert bare.post("/api/auth/login", json={"username": "x" * 70_000, "password": "y"}).status_code == 413
    assert bare.post("/api/auth/login", json={"username": "nobody", "password": "nothing1"}).status_code == 401


def test_the_first_account_is_only_created_on_a_local_address(plain_server):
    c = plain_server.client()
    for host in ("evil.example.com", "archipelago.example.com:8080"):
        r = c.post("/api/auth/setup", json={"username": "boss", "password": "bosspass123"}, headers={"Host": host})
        assert r.status_code == 403 and "IP address" in r.json()["detail"], host
    assert c.get("/api/auth/status").json()["needs_setup"] is True
    for host in ("tower", "tower.local", "192.168.1.20:8080", "[fd00::1]:8080"):
        r = plain_server.client().post("/api/auth/setup", json={"username": "x", "password": "short"},
                                       headers={"Host": host})
        assert r.status_code == 400, host  # gets past the address check to the password rules
    r = c.post("/api/auth/setup", json={"username": "boss", "password": "bosspass123"})  # 127.0.0.1
    assert r.status_code == 200


def test_spaces_around_a_username_do_not_buy_more_guesses(plain_server):
    c = plain_server.client()
    c.post("/api/auth/setup", json={"username": "boss", "password": "bosspass123"})
    statuses = [plain_server.client().post("/api/auth/login", json={"username": "boss" + " " * i, "password": "wrong"})
                .status_code for i in range(10)]
    assert statuses[:8] == [401] * 8 and set(statuses[8:]) == {429}


def test_cookies_are_secure_when_the_web_ui_is_on_https(plain_server):
    admin = plain_server.client()
    admin.post("/api/auth/setup", json={"username": "boss", "password": "bosspass123"})
    admin.patch("/api/settings", json={"site_url": "https://archipelago.example.com"})
    r = plain_server.client().post("/api/auth/login", json={"username": "boss", "password": "bosspass123"})
    assert r.status_code == 200 and "secure" in r.headers["set-cookie"].lower()
