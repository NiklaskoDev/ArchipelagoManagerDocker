"""Hosting games: importing pregenerated multiworlds, who sees what, and running real servers."""
import io
import socket
import zipfile

import pytest

from helpers import (APClient, make_game, player_yaml, pregenerated_zip, save_now, upload_yaml, wait_for, wait_state)

pytestmark = pytest.mark.ap


FIRST, SECOND = "Gwen", "Garth"  # slot names nobody else in the test run uses, so automatic linking is predictable


@pytest.fixture(scope="module")
def seed_game(admin):
    """A generated game used as the source of multidata for the upload tests."""
    game_id, _job = make_game(admin, names=(FIRST, SECOND))
    # make_game uploads the configs as the admin; remove them so they do not take part in slot matching
    for c in admin.get("/api/configs").json():
        if c["player_names"] and c["player_names"][0] in (FIRST, SECOND):
            admin.delete(f"/api/configs/{c['id']}")
    return game_id


def by_slot(game):
    """Slots keyed by number. The generator numbers players in directory-scan order, so tests never assume names."""
    return {s["slot"]: s for s in game["slots"]}


def upload_game(client, data: bytes, filename="seed.zip", **form):
    return client.post("/api/games", files={"file": (filename, data)}, data={k: str(v).lower() if isinstance(v, bool) else v for k, v in form.items()})


@pytest.fixture
def uploaded(admin, seed_game):
    """An uploaded pregenerated game with patch files; deleted again afterwards."""
    r = upload_game(admin, pregenerated_zip(admin, seed_game), name="Pregenerated Test")
    assert r.status_code == 200, r.text
    game = r.json()
    yield game
    admin.delete(f"/api/games/{game['id']}")


# ---------------------------------------------------------------------------------------------------------------------
# importing
# ---------------------------------------------------------------------------------------------------------------------
def test_upload_extracts_files_per_slot(uploaded, admin):
    assert uploaded["name"] == "Pregenerated Test" and uploaded["source"] == "upload"
    assert {s["name"] for s in uploaded["slots"]} == {FIRST, SECOND}
    slots = by_slot(uploaded)
    assert [f["filename"].split("_P1_")[1] for f in slots[1]["files"]] == [f"{slots[1]['name']}.apchecks"]
    two = sorted(f["filename"] for f in slots[2]["files"])
    assert len(two) == 2 and two[0].endswith(f"_P2_{slots[2]['name']}.apquest") and two[1].endswith(".extra")
    shared = {f["filename"]: f["kind"] for f in uploaded["shared_files"]}
    assert shared["README.txt"] == "other" and shared[f"{slots[1]['name'].lower()}-original.yaml"] == "yaml"
    assert any(k == "spoiler" for k in shared.values())


def test_zip_slip_cannot_write_outside_the_game_folder(uploaded, ap_server):
    data_dir = ap_server.data_dir
    assert not (data_dir.parent / "escaped.txt").exists() and not (data_dir / "escaped.txt").exists()
    stored = [f["filename"] for f in uploaded["shared_files"]]
    assert "escaped.txt" in stored  # flattened into the game's own files folder
    assert (data_dir / "games" / str(uploaded["id"]) / "files" / "escaped.txt").is_file()


def test_slots_are_linked_to_users_by_their_config_names(admin, seed_game, make_user):
    name, user = make_user()
    upload_yaml(user, "alice", player_yaml(FIRST))
    other_name, other = make_user()
    upload_yaml(other, "alice-too", player_yaml(FIRST.lower()))  # the same name, differently cased: ambiguous
    r = upload_game(admin, pregenerated_zip(admin, seed_game), name="Linked")
    game = r.json()
    try:
        # two users claim to be Alice, so nobody gets her slot automatically
        assert {s["name"]: s["owner"] for s in game["slots"]} == {FIRST: None, SECOND: None}
    finally:
        admin.delete(f"/api/games/{game['id']}")


def test_unambiguous_names_link_automatically(admin, seed_game, make_user):
    # SECOND is not used by any other config in this module, so the only owner gets the slot
    name, user = make_user()
    upload_yaml(user, "bob", player_yaml(SECOND, "APQuest"))
    configs = [c for c in admin.get("/api/configs?all=true").json() if c["player_names"] == [SECOND]]
    if len({c["user_id"] for c in configs}) != 1:
        pytest.skip("another test left a config named Bob")
    game = upload_game(admin, pregenerated_zip(admin, seed_game)).json()
    try:
        assert {s["name"]: s["owner"] for s in game["slots"]}[SECOND] == name
        assert user.get(f"/api/games/{game['id']}").status_code == 200
    finally:
        admin.delete(f"/api/games/{game['id']}")


def test_a_slot_is_not_handed_to_whoever_names_a_config_after_it(admin, seed_game, make_user):
    seed = admin.get(f"/api/games/{seed_game}").json()
    bundled, other = seed["slots"][0], seed["slots"][1]  # the zip bundles the first slot's yaml
    for c in admin.get("/api/configs?all=true").json():  # earlier tests left configs with these names
        if {n.casefold() for n in c["player_names"]} & {bundled["name"].casefold(), other["name"].casefold()}:
            admin.delete(f"/api/configs/{c['id']}")
    owner_name, owner = make_user()
    squatter_name, squatter = make_user()
    # the squatter copies the name and game; the owner's config is the yaml that came with the game, byte for byte
    upload_yaml(squatter, "copy", player_yaml(bundled["name"], bundled["game"]))
    upload_yaml(owner, "mine", player_yaml(bundled["name"], bundled["game"], "# original upload\n"))
    # a config with the right name but for another game is not that player's either
    wrong_game = "ChecksFinder" if other["game"] != "ChecksFinder" else "APQuest"
    upload_yaml(squatter, "other", player_yaml(other["name"], wrong_game))
    game = upload_game(admin, pregenerated_zip(admin, seed_game)).json()
    try:
        owners = {s["name"]: s["owner"] for s in game["slots"]}
        assert owners[bundled["name"]] == owner_name
        assert owners[other["name"]] != squatter_name
    finally:
        admin.delete(f"/api/games/{game['id']}")
        for client in (owner, squatter):
            for c in client.get("/api/configs").json():
                client.delete(f"/api/configs/{c['id']}")


def test_assigning_a_slot_copies_bundled_yaml_into_the_users_library(uploaded, admin, make_user):
    name, user = make_user()
    user_id = user.get("/api/auth/status").json()["user"]["id"]
    assert user.get("/api/configs").json() == []
    slots = by_slot(uploaded)
    # the bundled yaml belongs to slot 1, so assigning slot 2 brings nothing
    assert admin.post(f"/api/games/{uploaded['id']}/slots/2/assign", json={"user_id": user_id}).json()["configs_added"] == 0
    r = admin.post(f"/api/games/{uploaded['id']}/slots/1/assign", json={"user_id": user_id})
    assert r.status_code == 200 and r.json()["configs_added"] == 1
    configs = user.get("/api/configs").json()
    assert len(configs) == 1 and configs[0]["player_names"] == [slots[1]["name"]]
    assert configs[0]["source_game_id"] == uploaded["id"]
    # assigning again does not duplicate it
    again = admin.post(f"/api/games/{uploaded['id']}/slots/1/assign", json={"user_id": user_id})
    assert again.json()["configs_added"] == 0 and len(user.get("/api/configs").json()) == 1
    # and unassigning is possible
    assert admin.post(f"/api/games/{uploaded['id']}/slots/1/assign", json={"user_id": None}).json()["slots"][0]["owner"] is None


def test_bare_multidata_and_bad_uploads(admin, seed_game):
    bare = admin.get(f"/api/games/{seed_game}/multidata").content
    r = upload_game(admin, bare, "AP_whatever.archipelago")
    assert r.status_code == 200, r.text
    assert r.json()["slots"] and r.json()["shared_files"] == []
    admin.delete(f"/api/games/{r.json()['id']}")

    assert upload_game(admin, b"PK\x03\x04 not really a zip").status_code == 400
    empty = io.BytesIO()
    with zipfile.ZipFile(empty, "w") as zf:
        zf.writestr("readme.txt", "no seed in here")
    r = upload_game(admin, empty.getvalue())
    assert r.status_code == 400 and "archipelago" in r.json()["detail"].lower()
    r = upload_game(admin, b"this is not multiworld data", "x.archipelago")
    assert r.status_code == 400 and "read" in r.json()["detail"].lower()
    assert all(g["name"] != "x" for g in admin.get("/api/games").json())  # failed imports leave nothing behind


def test_only_admins_add_games(make_user, seed_game, admin):
    _n, user = make_user()
    assert upload_game(user, pregenerated_zip(admin, seed_game)).status_code == 403
    assert user.post(f"/api/games/{seed_game}/start").status_code == 404  # cannot even see it


def test_deleting_a_game_removes_its_files(admin, seed_game, ap_server):
    game = upload_game(admin, pregenerated_zip(admin, seed_game)).json()
    folder = ap_server.data_dir / "games" / str(game["id"])
    assert (folder / "multidata.archipelago").is_file()
    assert admin.delete(f"/api/games/{game['id']}").status_code == 200
    assert not folder.exists() and admin.get(f"/api/games/{game['id']}").status_code == 404


# ---------------------------------------------------------------------------------------------------------------------
# who can see and download what
# ---------------------------------------------------------------------------------------------------------------------
def test_file_permissions(uploaded, admin, make_user):
    gid = uploaded["id"]
    alice_name, alice = make_user("alice")
    bob_name, bob = make_user("bob")
    _o, outsider = make_user("out")
    uid = lambda c: c.get("/api/auth/status").json()["user"]["id"]
    admin.post(f"/api/games/{gid}/slots/1/assign", json={"user_id": uid(alice)})
    admin.post(f"/api/games/{gid}/slots/2/assign", json={"user_id": uid(bob)})
    slot1_name = by_slot(uploaded)[1]["name"]

    alice_view = alice.get(f"/api/games/{gid}").json()
    own, other = by_slot(alice_view)[1], by_slot(alice_view)[2]
    assert len(own["files"]) == 1 and other["files"] == []
    assert alice_view["shared_files"] == []  # spoiler, yaml and readme are not for players

    alice_file = own["files"][0]["id"]
    bob_file = admin.get(f"/api/games/{gid}").json()["slots"][1]["files"][0]["id"]
    assert alice.get(f"/api/games/{gid}/files/{alice_file}").content.startswith(f"patch-for-{slot1_name}".encode())
    assert alice.get(f"/api/games/{gid}/files/{bob_file}").status_code == 404
    assert outsider.get(f"/api/games/{gid}/files/{alice_file}").status_code == 404

    spoiler = next(f for f in admin.get(f"/api/games/{gid}").json()["shared_files"] if f["kind"] == "spoiler")
    assert alice.get(f"/api/games/{gid}/files/{spoiler['id']}").status_code == 404
    assert admin.get(f"/api/games/{gid}/files/{spoiler['id']}").content == b"secret spoiler"

    # the admin can release a file to everyone in the game
    assert admin.patch(f"/api/games/{gid}/files/{spoiler['id']}", json={"public": True}).status_code == 200
    assert alice.get(f"/api/games/{gid}/files/{spoiler['id']}").status_code == 200
    assert outsider.get(f"/api/games/{gid}/files/{spoiler['id']}").status_code == 404  # but not to people outside it

    # zip of one's own files
    z = bob.get(f"/api/games/{gid}/my-files.zip")
    assert z.status_code == 200
    names = zipfile.ZipFile(io.BytesIO(z.content)).namelist()
    assert len(names) == 2 and all("_P2_" in n for n in names)
    assert outsider.get(f"/api/games/{gid}/my-files.zip").status_code == 404

    # only the admin sees the log, the console and the multidata
    for call in (lambda c: c.get(f"/api/games/{gid}/log"), lambda c: c.post(f"/api/games/{gid}/command", json={"command": "/help"}),
                 lambda c: c.get(f"/api/games/{gid}/multidata"), lambda c: c.delete(f"/api/games/{gid}"),
                 lambda c: c.patch(f"/api/games/{gid}", json={"name": "hacked"})):
        assert call(alice).status_code == 403
    assert "server_password" not in alice_view and alice_view["password"] is None  # no password set yet


def test_admin_can_move_files_between_slots_and_manage_them(uploaded, admin):
    gid = uploaded["id"]
    readme = next(f for f in uploaded["shared_files"] if f["filename"] == "README.txt")
    bob = by_slot(uploaded)[2]
    r = admin.patch(f"/api/games/{gid}/files/{readme['id']}", json={"slot": bob["slot"]})
    assert r.status_code == 200
    assert admin.patch(f"/api/games/{gid}/files/{readme['id']}", json={"slot": 99}).status_code == 404
    new = admin.post(f"/api/games/{gid}/files", files={"file": ("extra.bin", b"123")}, data={"slot": str(bob["slot"])})
    assert new.status_code == 200
    assert admin.delete(f"/api/games/{gid}/files/{readme['id']}").status_code == 200
    detail = admin.get(f"/api/games/{gid}").json()
    assert "README.txt" not in [f["filename"] for f in detail["shared_files"]]


def test_claiming_slots(uploaded, admin, make_user):
    gid = uploaded["id"]
    _a, alice = make_user()
    _b, bob = make_user()
    # users cannot see games they have nothing to do with ...
    assert alice.get(f"/api/games/{gid}").status_code == 404
    admin.patch(f"/api/games/{gid}", json={"visibility": "all"})
    detail = alice.get(f"/api/games/{gid}").json()
    assert not any(s["claimable"] for s in detail["slots"])
    slot = 1
    assert alice.post(f"/api/games/{gid}/slots/{slot}/claim").status_code == 403  # claiming is off

    admin.patch(f"/api/games/{gid}", json={"claimable": True})
    assert by_slot(alice.get(f"/api/games/{gid}").json())[slot]["claimable"]
    r = alice.post(f"/api/games/{gid}/slots/{slot}/claim")
    assert r.status_code == 200 and r.json()["my_slots"][0]["slot"] == slot and r.json()["configs_added"] == 1
    assert [f["filename"] for s in r.json()["slots"] if s["mine"] for f in s["files"]]  # her patch is available
    assert bob.post(f"/api/games/{gid}/slots/{slot}/claim").status_code == 409  # taken
    assert bob.post(f"/api/games/{gid}/slots/{slot}/unclaim").status_code == 403  # not Bob's to release
    assert alice.post(f"/api/games/{gid}/slots/{slot}/unclaim").status_code == 200
    assert bob.post(f"/api/games/{gid}/slots/{slot}/claim").status_code == 200  # free again


def test_game_settings_validation(uploaded, admin):
    gid = uploaded["id"]
    other, _ = make_game(admin)
    other_port = admin.get(f"/api/games/{other}").json()["port"]
    assert admin.patch(f"/api/games/{gid}", json={"port": other_port}).status_code == 409
    assert admin.patch(f"/api/games/{gid}", json={"port": 80}).status_code == 400
    assert admin.patch(f"/api/games/{gid}", json={"password": "has space"}).status_code == 400
    assert admin.patch(f"/api/games/{gid}", json={"options": {"hint_cost": 1000}}).status_code == 400
    assert admin.patch(f"/api/games/{gid}", json={"options": {"bogus": 1}}).status_code == 400
    assert admin.patch(f"/api/games/{gid}", json={"visibility": "everyone"}).status_code == 400
    assert admin.patch(f"/api/games/{gid}", json={"name": "   "}).status_code == 400
    r = admin.patch(f"/api/games/{gid}", json={"name": "Renamed", "notes": "hello", "options": {"hint_cost": 33}, "password": "pw1", "server_password": "adm1"})
    body = r.json()
    assert r.status_code == 200 and body["name"] == "Renamed" and body["options"]["hint_cost"] == 33
    assert body["password"] == "pw1" and body["server_password"] == "adm1" and body["notes"] == "hello"
    assert body["options"]["release_mode"] == "auto"  # untouched options keep their value
    cleared = admin.patch(f"/api/games/{gid}", json={"password": ""}).json()
    assert cleared["password"] is None and cleared["has_password"] is False
    admin.delete(f"/api/games/{other}")


# ---------------------------------------------------------------------------------------------------------------------
# real servers
# ---------------------------------------------------------------------------------------------------------------------
def test_play_a_game_end_to_end(admin, make_user):
    alice_name, alice = make_user("alice")
    bob_name, bob = make_user("bob")
    gid, _job = make_game(admin, names=("Aly", "Bobo"), owners={"Aly": alice, "Bobo": bob})
    port = admin.get(f"/api/games/{gid}").json()["port"]

    # participants may start their own game (the default), strangers may not
    _o, stranger = make_user("stranger")
    assert stranger.post(f"/api/games/{gid}/start").status_code == 404
    r = alice.post(f"/api/games/{gid}/start")
    assert r.status_code == 200
    game = wait_state(admin, gid, "running")
    assert game["can_control"] and game["address"] == f"play.example.test:{port}"

    with APClient(port, "Aly", "ChecksFinder") as client:
        assert client.connected
        client.check(4)
        save_now(admin, gid)
        view = alice.get(f"/api/games/{gid}").json()
        me = next(s for s in view["slots"] if s["mine"])
        assert me["checked"] == 4 and me["status"] == "online" and view["progress"]["checked"] == 4
        assert next(s for s in view["slots"] if s["name"] == "Bobo")["status"] == "offline"
    save_now(admin, gid)
    assert next(s for s in alice.get(f"/api/games/{gid}").json()["slots"] if s["mine"])["status"] == "offline"

    # unknown slot names are refused by the server
    with APClient(port, "Nobody", "ChecksFinder") as client:
        assert not client.connected and client.response["errors"] == ["InvalidSlot"]

    # passwords take effect on the running server without a restart
    assert admin.patch(f"/api/games/{gid}", json={"password": "letmein"}).status_code == 200
    wait_for(lambda: not APClient(port, "Aly", "ChecksFinder").connected, timeout=10, message="password to apply")
    with APClient(port, "Aly", "ChecksFinder", "wrong") as client:
        assert client.response["errors"] == ["InvalidPassword"]
    with APClient(port, "Aly", "ChecksFinder", "letmein") as client:
        assert client.connected and len(client.response["checked_locations"]) == 4  # progress is kept
    assert alice.get(f"/api/games/{gid}").json()["password"] == "letmein"
    assert stranger.get("/api/games").json() == []

    # stopping and starting keeps progress and settings
    assert bob.post(f"/api/games/{gid}/stop").status_code == 200
    wait_state(admin, gid, "stopped")
    with pytest.raises((OSError, ConnectionError, TimeoutError)):
        socket.create_connection(("127.0.0.1", port), timeout=2).close()
    assert admin.post(f"/api/games/{gid}/start").status_code == 200
    wait_state(admin, gid, "running")
    with APClient(port, "Aly", "ChecksFinder", "letmein") as client:
        assert client.connected and len(client.response["checked_locations"]) == 4
    with APClient(port, "Aly", "ChecksFinder") as client:
        assert client.response["errors"] == ["InvalidPassword"]  # the password survived the restart

    # the console log is the admin's
    log = [l["text"] for l in admin.get(f"/api/games/{gid}/log").json()["lines"]]
    assert any("Aly" in l and "has joined" in l for l in log)
    assert admin.post(f"/api/games/{gid}/command", json={"command": "/send Bobo Anything"}).status_code == 200
    assert admin.post(f"/api/games/{gid}/command", json={"command": "line1\nline2"}).status_code == 409

    assert admin.post(f"/api/games/{gid}/reset-save").status_code == 409  # not while running
    admin.post(f"/api/games/{gid}/stop")
    wait_state(admin, gid, "stopped")
    assert admin.post(f"/api/games/{gid}/reset-save").status_code == 200
    admin.post(f"/api/games/{gid}/start")
    wait_state(admin, gid, "running")
    with APClient(port, "Aly", "ChecksFinder", "letmein") as client:
        assert client.connected and client.response["checked_locations"] == []  # back to zero
    admin.delete(f"/api/games/{gid}")


def test_participants_cannot_control_games_when_the_setting_is_off(admin, make_user):
    name, user = make_user()
    gid, _ = make_game(admin, names=("Pia",), owners={"Pia": user})
    admin.patch("/api/settings", json={"users_can_start_rooms": False})
    try:
        assert user.get(f"/api/games/{gid}").json()["can_control"] is False
        assert user.post(f"/api/games/{gid}/start").status_code == 403
        assert admin.post(f"/api/games/{gid}/start").status_code == 200
        wait_state(admin, gid, "running")
        assert user.post(f"/api/games/{gid}/stop").status_code == 403
    finally:
        admin.patch("/api/settings", json={"users_can_start_rooms": True})
        admin.delete(f"/api/games/{gid}")


def test_each_game_gets_its_own_port_and_conflicts_are_reported(admin, ap_server):
    first, _ = make_game(admin, names=("One",))
    second, _ = make_game(admin, names=("Two",))
    ports = [admin.get(f"/api/games/{g}").json()["port"] for g in (first, second)]
    assert ports[0] != ports[1] and all(ap_server.game_port_start <= p < ap_server.game_port_start + 20 for p in ports)
    for g in (first, second):
        assert admin.post(f"/api/games/{g}/start").status_code == 200
    wait_state(admin, first, "running")
    wait_state(admin, second, "running")
    # a port taken by something else is a clear error, not a hang
    admin.post(f"/api/games/{second}/stop")
    wait_state(admin, second, "stopped")
    blocker = socket.socket()
    blocker.bind(("0.0.0.0", ports[1]))
    blocker.listen()
    try:
        r = admin.post(f"/api/games/{second}/start")
        assert r.status_code == 409 and "already in use" in r.json()["detail"]
    finally:
        blocker.close()
    assert admin.post(f"/api/games/{second}/start").status_code == 200
    wait_state(admin, second, "running")
    for g in (first, second):
        admin.delete(f"/api/games/{g}")


def test_restart_is_only_requested_when_a_startup_option_really_changes(admin):
    gid, _ = make_game(admin, names=("Careful",))
    admin.post(f"/api/games/{gid}/start")
    wait_state(admin, gid, "running")
    try:
        options = admin.get(f"/api/games/{gid}").json()["options"]
        # the web UI always sends every option; resending the same values is not a change
        r = admin.patch(f"/api/games/{gid}", json={"options": options, "name": "Careful 2"})
        assert r.status_code == 200 and r.json()["restart_required"] is False
        # settings that are applied live never need a restart
        r = admin.patch(f"/api/games/{gid}", json={"options": {**options, "hint_cost": 77}, "password": "live"})
        assert r.json()["restart_required"] is False and r.json()["options"]["hint_cost"] == 77
        r = admin.patch(f"/api/games/{gid}", json={"options": {**options, "auto_shutdown_minutes": 30}})
        assert r.json()["restart_required"] is False  # the manager puts the server to sleep, not the server
        # but a startup-only option does
        r = admin.patch(f"/api/games/{gid}", json={"options": {**options, "loglevel": "debug"}})
        assert r.json()["restart_required"] is True
        port = admin.get(f"/api/games/{gid}").json()["port"]
        r = admin.patch(f"/api/games/{gid}", json={"port": port + 7 if port + 7 < 65535 else port - 7})
        assert r.status_code == 200 and r.json()["restart_required"] is True
    finally:
        admin.delete(f"/api/games/{gid}")


def test_a_seed_that_needs_a_newer_server_is_refused_clearly(admin, seed_game):
    from helpers import multidata_requiring_server
    multidata = admin.get(f"/api/games/{seed_game}/multidata").content
    newer = multidata_requiring_server(multidata, (99, 0, 0))
    r = upload_game(admin, newer, "AP_future.archipelago")
    assert r.status_code == 400
    assert "99.0.0" in r.json()["detail"] and "0.6.8" in r.json()["detail"]
    # the same data with a version this server can run is fine
    ok = upload_game(admin, multidata_requiring_server(multidata, (0, 5, 0)), "AP_ok.archipelago")
    assert ok.status_code == 200
    admin.delete(f"/api/games/{ok.json()['id']}")


@pytest.mark.parametrize("word", ["null", "NONE", "None", '""', "''"])
def test_passwords_the_server_would_read_as_no_password_are_refused(admin, word):
    gid, _ = make_game(admin, names=("Pwd",))
    try:
        assert admin.patch(f"/api/games/{gid}", json={"password": word}).status_code == 400
        assert admin.patch(f"/api/games/{gid}", json={"server_password": word}).status_code == 400
        assert admin.patch(f"/api/games/{gid}", json={"password": "nullable"}).status_code == 200  # only the exact words
    finally:
        admin.delete(f"/api/games/{gid}")


def test_one_slot_each_when_the_game_says_so(uploaded, admin, make_user):
    gid = uploaded["id"]
    _a, alice = make_user()
    _b, bob = make_user()
    alice_id = alice.get("/api/auth/status").json()["user"]["id"]
    admin.patch(f"/api/games/{gid}", json={"visibility": "all", "claimable": True})

    # off (the default): one player may take several slots
    assert alice.post(f"/api/games/{gid}/slots/1/claim").status_code == 200
    assert alice.post(f"/api/games/{gid}/slots/2/claim").status_code == 200
    assert len(alice.get(f"/api/games/{gid}").json()["my_slots"]) == 2
    alice.post(f"/api/games/{gid}/slots/2/unclaim")

    # on: the second claim is refused and no longer offered
    r = admin.patch(f"/api/games/{gid}", json={"single_claim": True})
    assert r.status_code == 200 and r.json()["single_claim"] is True
    view = alice.get(f"/api/games/{gid}").json()
    assert view["single_claim"] is True and not any(s["claimable"] for s in view["slots"])
    r = alice.post(f"/api/games/{gid}/slots/2/claim")
    assert r.status_code == 409 and "only one" in r.json()["detail"]
    assert by_slot(bob.get(f"/api/games/{gid}").json())[2]["claimable"]  # others still can
    assert bob.post(f"/api/games/{gid}/slots/2/claim").status_code == 200

    # switching works by releasing first
    bob.post(f"/api/games/{gid}/slots/2/unclaim")
    assert alice.post(f"/api/games/{gid}/slots/1/unclaim").status_code == 200
    assert alice.post(f"/api/games/{gid}/slots/2/claim").status_code == 200

    # the limit is for claiming only: an admin may still give someone a second slot
    r = admin.post(f"/api/games/{gid}/slots/1/assign", json={"user_id": alice_id})
    assert r.status_code == 200 and len(alice.get(f"/api/games/{gid}").json()["my_slots"]) == 2
