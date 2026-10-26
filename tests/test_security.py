"""Fixes from a security review: what strangers can send, what logs reveal, and work one request can cause."""
import re
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
    # nor through aliases that repeat each other
    bomb = "a0: &a0 [x, x, x, x, x, x, x, x, x]\n" + "".join(
        f"a{i}: &a{i} [{', '.join([f'*a{i - 1}'] * 9)}]\n" for i in range(1, 9)) + "meta_description: x\n"
    with pytest.raises(JobRequestError, match="repeat too much"):
        clean_meta(bomb)


def nested_aliases(levels: int) -> str:
    """A player file of a few hundred bytes whose description, written out, is 9 ** (levels + 1) words."""
    lines = ["name: A", "game: Clique", "Clique: {}", "x0: &a0 [lol, lol, lol, lol, lol, lol, lol, lol, lol]"]
    lines += [f"x{i}: &a{i} [{', '.join([f'*a{i - 1}'] * 9)}]" for i in range(1, levels + 1)]
    return "\n".join([*lines, f"description: *a{levels}"]) + "\n"


def test_a_tiny_yaml_cannot_stand_for_gigabytes(plain_server):
    admin = plain_server.client()
    admin.post("/api/auth/setup", json={"username": "boss", "password": "bosspass123", "code": plain_server.setup_code()})
    bomb = nested_aliases(9)
    assert len(bomb) < 600
    # everywhere a player file comes in, before anything can print or copy it (the form printed the description)
    for path, body in (("/api/configs/text", {"name": "x", "content": bomb}), ("/api/configs/form/read", {"content": bomb}),
                       ("/api/configs/roll", {"content": bomb})):
        r = admin.post(path, json=body)
        assert r.status_code == 400 and "repeat too much" in r.json()["detail"], (path, r.text)
    r = admin.post("/api/configs", files={"files": ("bomb.yaml", bomb.encode())})
    assert r.status_code == 400 and "repeat too much" in r.json()["detail"]
    assert admin.post("/api/configs/text", json={"name": "fine", "content": nested_aliases(2)}).status_code == 200


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


def test_the_first_account_needs_the_setup_code(plain_server):
    # Only whoever runs the container can read it: in the log and in the data folder. Which address a request names is
    # the sender's to choose (a stranger on the internet sends "Host: 192.168.1.20" as easily as anything else).
    path = plain_server.data_dir / "setup-code.txt"
    code = plain_server.setup_code()
    assert re.fullmatch(r"[A-Z2-9]{4}-[A-Z2-9]{4}-[A-Z2-9]{4}", code)
    assert path.stat().st_mode & 0o077 == 0
    assert code in plain_server.log()
    c = plain_server.client()
    for wrong in ("", "AAAA-AAAA-AAAA", code[:-1]):
        r = c.post("/api/auth/setup", json={"username": "boss", "password": "bosspass123", "code": wrong},
                   headers={"Host": "192.168.1.20:8080"})
        assert r.status_code == 403 and "setup code" in r.json()["detail"], wrong
    assert c.get("/api/auth/status").json()["needs_setup"] is True

    # a restart keeps the code; typed in small letters and without dashes, through a domain name, it works
    plain_server.stop()
    plain_server.start()
    assert plain_server.setup_code() == code
    r = c.post("/api/auth/setup", json={"username": "boss", "password": "bosspass123",
                                        "code": code.lower().replace("-", " ")}, headers={"Host": "play.example.com"})
    assert r.status_code == 200 and r.json()["role"] == "admin"
    assert not path.exists()
    r = plain_server.client().post("/api/auth/setup", json={"username": "evil", "password": "evilpass123", "code": code})
    assert r.status_code == 409
    plain_server.stop()
    plain_server.start()
    assert not path.exists()  # none is made once there is an administrator


def test_spaces_around_a_username_do_not_buy_more_guesses(plain_server):
    c = plain_server.client()
    c.post("/api/auth/setup", json={"username": "boss", "password": "bosspass123", "code": plain_server.setup_code()})
    statuses = [plain_server.client().post("/api/auth/login", json={"username": "boss" + " " * i, "password": "wrong"})
                .status_code for i in range(10)]
    assert statuses[:8] == [401] * 8 and set(statuses[8:]) == {429}


def test_cookies_are_secure_when_the_web_ui_is_on_https(plain_server):
    admin = plain_server.client()
    admin.post("/api/auth/setup", json={"username": "boss", "password": "bosspass123", "code": plain_server.setup_code()})
    admin.patch("/api/settings", json={"site_url": "https://archipelago.example.com"})
    r = plain_server.client().post("/api/auth/login", json={"username": "boss", "password": "bosspass123"})
    assert r.status_code == 200 and "secure" in r.headers["set-cookie"].lower()
