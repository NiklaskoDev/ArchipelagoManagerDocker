"""Custom games: installing, approving, replacing and removing .apworld files."""
import io
import zipfile

import pytest

from helpers import build_apworld, player_yaml, run_job, upload_yaml

pytestmark = pytest.mark.ap

CUSTOM = "Custom Checks"


def post_apworld(client, path, name=None):
    with open(path, "rb") as f:
        return client.post("/api/apworlds", files={"file": (name or path.name, f.read())})


def game_names(client):
    return {g["name"] for g in client.get("/api/worlds").json()["games"]}


@pytest.fixture
def clean_custom(admin):
    """Removes any custom world left over from other tests so each test starts without it."""
    def clean():
        for item in admin.get("/api/apworlds").json()["items"]:
            admin.delete(f"/api/apworlds/{item['id']}")
    clean()
    yield
    clean()


def test_admin_install_makes_the_game_usable_end_to_end(admin, make_user, tmp_path, clean_custom):
    _n, user = make_user()
    cfg = upload_yaml(user, "custom", player_yaml("Cust", game=CUSTOM)).json()["created"][0]
    assert cfg["missing_games"] == [CUSTOM] and CUSTOM not in game_names(user)

    # generating before the world exists is refused, with the missing game named for the prompt
    r = admin.post("/api/jobs", json={"config_ids": [cfg["id"]], "host_after": False})
    assert r.status_code == 409 and r.json()["missing_games"] == [CUSTOM]

    r = post_apworld(admin, build_apworld(tmp_path / "customchecks.apworld"))
    assert r.status_code == 200, r.text
    item = r.json()
    assert item["status"] == "installed" and item["game"] == CUSTOM and item["version"] == "1.2.0" and item["load_error"] is None

    assert CUSTOM in game_names(user)
    assert user.get("/api/configs").json()[0]["missing_games"] == []
    assert not any(m["game"] == CUSTOM for m in user.get("/api/missing-games").json())
    templates = {t["game"]: t for t in user.get("/api/templates").json()}
    assert templates[CUSTOM]["source"] == "custom"
    assert f"game: {CUSTOM}" in user.get("/api/templates/yaml", params={"game": CUSTOM}).text

    job = run_job(admin, [cfg["id"]])
    assert job["status"] == "succeeded", job["error"]
    game = admin.get(f"/api/games/{job['game_id']}").json()
    assert game["games"] == [CUSTOM] and game["missing_games"] == []

    # installed worlds can be downloaded by everyone so players can use them locally
    dl = user.get(f"/api/apworlds/{item['id']}/download")
    assert dl.status_code == 200 and zipfile.is_zipfile(io.BytesIO(dl.content))
    assert 'customchecks.apworld' in dl.headers["content-disposition"]

    assert admin.delete(f"/api/apworlds/{item['id']}").status_code == 200
    assert CUSTOM not in game_names(user)
    assert user.get("/api/configs").json()[0]["missing_games"] == [CUSTOM]
    assert any(m["game"] == CUSTOM for m in user.get("/api/missing-games").json())


def test_user_submission_waits_for_approval(admin, make_user, tmp_path, clean_custom):
    _n, user = make_user()
    r = post_apworld(user, build_apworld(tmp_path / "customchecks.apworld"))
    assert r.status_code == 200 and r.json()["status"] == "pending"
    pending = r.json()
    assert CUSTOM not in game_names(user)  # not installed yet

    other_name, other = make_user()
    assert other.get("/api/apworlds").json()["items"] == []  # others cannot see pending submissions
    assert other.get(f"/api/apworlds/{pending['id']}/download").status_code == 404
    assert other.delete(f"/api/apworlds/{pending['id']}").status_code == 403
    assert other.post(f"/api/apworlds/{pending['id']}/approve").status_code == 403
    assert user.post(f"/api/apworlds/{pending['id']}/approve").status_code == 403
    assert user.get(f"/api/apworlds/{pending['id']}/download").status_code == 200  # the submitter can

    assert admin.post(f"/api/apworlds/{pending['id']}/approve").status_code == 200
    assert CUSTOM in game_names(user)
    assert admin.post(f"/api/apworlds/{pending['id']}/approve").status_code == 409
    assert user.delete(f"/api/apworlds/{pending['id']}").status_code == 403  # installed: admin only


def test_submitter_can_withdraw_and_admin_can_reject(admin, make_user, tmp_path, clean_custom):
    _n, user = make_user()
    path = build_apworld(tmp_path / "customchecks.apworld")
    first = post_apworld(user, path).json()
    assert user.delete(f"/api/apworlds/{first['id']}").status_code == 200
    second = post_apworld(user, path).json()
    assert admin.delete(f"/api/apworlds/{second['id']}").status_code == 200
    assert admin.get("/api/apworlds").json()["items"] == []


def test_users_may_be_barred_from_submitting(admin, make_user, tmp_path, clean_custom):
    _n, user = make_user()
    admin.patch("/api/settings", json={"users_can_upload_apworlds": False})
    try:
        assert post_apworld(user, build_apworld(tmp_path / "customchecks.apworld")).status_code == 403
        assert user.get("/api/apworlds").json()["can_submit"] is False
    finally:
        admin.patch("/api/settings", json={"users_can_upload_apworlds": True})


def test_missing_game_shows_pending_submission(admin, make_user, tmp_path, clean_custom):
    name, user = make_user()
    upload_yaml(user, "custom", player_yaml("Cust", game=CUSTOM))
    _o, other = make_user()
    upload_yaml(other, "custom", player_yaml("Cust2", game=CUSTOM))
    post_apworld(user, build_apworld(tmp_path / "customchecks.apworld"))
    mine = next(m for m in user.get("/api/missing-games").json() if m["game"] == CUSTOM)
    assert mine["pending"][0]["id"] is not None
    theirs = next(m for m in other.get("/api/missing-games").json() if m["game"] == CUSTOM)
    assert theirs["pending"] == [{"id": None, "uploader": None}]  # knows something is pending, not whose
    admin_view = next(m for m in admin.get("/api/missing-games").json() if m["game"] == CUSTOM)
    assert admin_view["pending"][0]["uploader"] == name


def test_newer_version_replaces_the_installed_one(admin, tmp_path, clean_custom):
    first = post_apworld(admin, build_apworld(tmp_path / "customchecks.apworld", version="1.2.0")).json()
    second = post_apworld(admin, build_apworld(tmp_path / "customchecks.apworld", version="1.3.0")).json()
    assert second["replaced"] == [{"id": first["id"], "version": "1.2.0"}]
    items = admin.get("/api/apworlds").json()["items"]
    assert [(i["id"], i["version"]) for i in items] == [(second["id"], "1.3.0")]
    worlds = {g["name"]: g for g in admin.get("/api/worlds").json()["games"]}
    assert worlds[CUSTOM]["version"] == "1.3.0"


@pytest.mark.parametrize("builder, message", [
    (lambda p: p.write_bytes(b"not a zip"), "valid zip"),
    (lambda p: zipfile.ZipFile(p, "w").writestr("x/__init__.py", ""), "manifest"),
    (lambda p: zipfile.ZipFile(p, "w").writestr("x/archipelago.json", "{}"), "__init__"),
    (lambda p: (lambda z: (z.writestr("x/__init__.py", ""), z.writestr("x/archipelago.json", "{not json")))(zipfile.ZipFile(p, "w")), "JSON"),
    (lambda p: (lambda z: (z.writestr("x/__init__.py", ""), z.writestr("x/archipelago.json", "{\"no\": 1}")))(zipfile.ZipFile(p, "w")), "game"),
])
def test_invalid_files_are_rejected(admin, tmp_path, clean_custom, builder, message):
    path = tmp_path / "broken.apworld"
    builder(path)
    r = post_apworld(admin, path)
    assert r.status_code == 400 and message in r.json()["detail"]
    assert admin.get("/api/apworlds").json()["items"] == []


def test_rejects_wrong_extension_and_builtin_name_clash_and_too_new(admin, tmp_path, clean_custom):
    assert admin.post("/api/apworlds", files={"file": ("world.zip", b"x")}).status_code == 400
    r = post_apworld(admin, build_apworld(tmp_path / "clash.apworld", game="Clash", module="alttp"))
    assert r.status_code == 400 and "built-in" in r.json()["detail"]
    r = post_apworld(admin, build_apworld(tmp_path / "new.apworld", game="Future", module="futuregame", minimum="9.0.0"))
    assert r.status_code == 400 and "9.0.0" in r.json()["detail"]
    assert "Future" not in game_names(admin)


def test_a_world_that_cannot_load_is_reported(admin, tmp_path, clean_custom):
    path = tmp_path / "brokenworld.apworld"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("brokenworld/__init__.py", "raise RuntimeError('this world is broken on purpose')\n")
        zf.writestr("brokenworld/archipelago.json", '{"game": "Broken World", "version": 7, "compatible_version": 7, "world_version": "0.1.0"}')
    r = post_apworld(admin, path)
    assert r.status_code == 200
    assert r.json()["load_error"] and "broken on purpose" in r.json()["load_error"]
    assert "Broken World" not in game_names(admin)
    # the rest of Archipelago keeps working
    assert "ChecksFinder" in game_names(admin)


def test_installed_worlds_survive_a_restart(ap_server, admin, tmp_path, clean_custom):
    post_apworld(admin, build_apworld(tmp_path / "customchecks.apworld"))
    assert CUSTOM in game_names(admin)
    ap_server.stop()
    ap_server.start()
    try:
        fresh = ap_server.client("admin", "adminpass1")
        assert CUSTOM in game_names(fresh)
    finally:
        pass


def test_worlds_dropped_into_the_folder_by_hand_are_adopted(ap_server, admin, tmp_path, clean_custom):
    """A file placed in Archipelago's worlds folder works, so it must also be visible and removable in the UI."""
    folder = ap_server.ap_dir / "custom_worlds"
    build_apworld(folder / "customchecks.apworld")
    ap_server.stop()
    ap_server.start()
    fresh = ap_server.client("admin", "adminpass1")
    items = fresh.get("/api/apworlds").json()["items"]
    assert [(i["game"], i["status"], i["load_error"]) for i in items] == [(CUSTOM, "installed", None)]
    assert CUSTOM in game_names(fresh)
    # it is now a normal library entry: downloadable and removable
    assert fresh.get(f"/api/apworlds/{items[0]['id']}/download").status_code == 200
    assert fresh.delete(f"/api/apworlds/{items[0]['id']}").status_code == 200
    assert not (folder / "customchecks.apworld").exists()
    assert CUSTOM not in game_names(fresh)


def test_files_that_would_break_archipelago_are_quarantined(ap_server, clean_custom):
    """One corrupt .apworld makes Archipelago refuse to load any world. The manager moves it aside instead."""
    folder = ap_server.ap_dir / "custom_worlds"
    (folder / "garbage.apworld").write_bytes(b"not a zip")
    ap_server.stop()
    ap_server.start()
    fresh = ap_server.client("admin", "adminpass1")
    assert "ChecksFinder" in game_names(fresh)  # Archipelago still works
    assert not (folder / "garbage.apworld").exists()
    moved = list((ap_server.data_dir / "quarantine").glob("garbage.apworld.*"))
    assert len(moved) == 1 and moved[0].read_bytes() == b"not a zip"  # moved, never deleted
    system = fresh.get("/api/system").json()
    assert any("garbage.apworld" in q for q in system["quarantined"])


def test_a_world_with_the_wrong_name_is_left_in_place_and_reported(ap_server, clean_custom):
    folder = ap_server.ap_dir / "custom_worlds"
    build_apworld(folder / "wrongname.apworld")  # contains customchecks/, so it cannot load as "wrongname"
    ap_server.stop()
    ap_server.start()
    fresh = ap_server.client("admin", "adminpass1")
    assert fresh.get("/api/apworlds").json()["items"] == []  # not adopted: it can never work
    assert (folder / "wrongname.apworld").exists()  # and not deleted either
    assert "wrongname" in fresh.get("/api/system").json()["failed_worlds"]
    (folder / "wrongname.apworld").unlink()


def test_host_yaml_can_be_read_and_safely_edited(ap_server, admin):
    current = admin.get("/api/host-yaml").json()
    assert "server_options" in current["content"] and current["persistent"] is False  # tests run as root, in local mode
    # broken files are refused with a useful message and nothing changes
    r = admin.put("/api/host-yaml", json={"content": "server_options: [oops"})
    assert r.status_code == 400 and "line" in r.json()["detail"]
    r = admin.put("/api/host-yaml", json={"content": "generator:\n  spoiler: 9\n"})
    assert r.status_code == 400 and "Archipelago rejected" in r.json()["detail"]
    assert admin.get("/api/host-yaml").json()["content"] == current["content"]
    # a valid edit is written, with a backup of the previous file
    edited = current["content"].replace("hint_cost: 10", "hint_cost: 15")
    assert edited != current["content"]
    assert admin.put("/api/host-yaml", json={"content": edited}).status_code == 200
    assert "hint_cost: 15" in admin.get("/api/host-yaml").json()["content"]
    assert (ap_server.ap_dir / "host.yaml.bak").read_text() == current["content"]
    admin.put("/api/host-yaml", json={"content": current["content"]})


def test_oversized_manifest_is_rejected(admin, tmp_path, clean_custom):
    path = tmp_path / "bigmanifest.apworld"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("bigmanifest/__init__.py", "")
        zf.writestr("bigmanifest/archipelago.json", '{"game": "Big", "pad": "' + "x" * (2 * 1024 * 1024) + '"}')
    r = post_apworld(admin, path)
    assert r.status_code == 400 and "unreasonably large" in r.json()["detail"]
