"""Copies of a game's save: automatic, by hand and before resets, and restoring them."""
import json
import time
import zlib

import pytest

from archipelago_manager.backups import _read_complete
from helpers import APClient, make_game, save_now, wait_for, wait_state


def test_a_save_caught_mid_write_is_not_copied(tmp_path):
    whole = zlib.compress(b"progress" * 1000)
    (tmp_path / "whole.apsave").write_bytes(whole)
    (tmp_path / "half.apsave").write_bytes(whole[: len(whole) // 2])
    assert _read_complete(tmp_path / "whole.apsave") == whole
    assert _read_complete(tmp_path / "half.apsave") is None


@pytest.mark.ap
def test_backups_make_a_reset_undoable(admin, make_user):
    _name, player = make_user("player")
    gid, _job = make_game(admin, names=("Bax", "Bix"), owners={"Bax": player})
    url = f"/api/games/{gid}/backups"
    assert admin.get(url).json()["backups"] == [] and admin.get(url).json()["has_save"] is False
    assert admin.post(url).status_code == 409  # nothing to back up before the first save

    admin.post(f"/api/games/{gid}/start")
    port = wait_state(admin, gid, "running")["port"]
    with APClient(port, "Bax", "ChecksFinder") as client:
        client.check(2)
    save_now(admin, gid)
    # while the game runs, the first change of the save is copied automatically
    auto = wait_for(lambda: [b for b in admin.get(url).json()["backups"] if b["reason"] == "auto"], timeout=40,
                    message="an automatic backup")
    assert len(auto) == 1 and auto[0]["size"] > 0
    manual = admin.post(url).json()["backups"][0]
    assert manual["reason"] == "manual" and manual["name"].endswith("-manual.apsave")
    data = admin.get(f"{url}/{manual['name']}").content
    assert zlib.decompress(data)  # a whole save, ready to keep elsewhere

    # restoring needs the server stopped; resetting keeps a copy first
    assert admin.post(f"{url}/{manual['name']}/restore").status_code == 409
    admin.post(f"/api/games/{gid}/stop")
    wait_state(admin, gid, "stopped")
    assert admin.post(f"/api/games/{gid}/reset-save").status_code == 200
    listing = admin.get(url).json()
    assert listing["has_save"] is False and listing["backups"][0]["reason"] == "before-reset"

    r = admin.post(f"{url}/{manual['name']}/restore")
    assert r.status_code == 200 and r.json()["progress"]["checked"] == 2
    assert [b["reason"] for b in admin.get(url).json()["backups"]].count("before-restore") == 0  # there was no save
    admin.post(f"/api/games/{gid}/start")
    wait_state(admin, gid, "running")
    with APClient(port, "Bax", "ChecksFinder") as client:
        assert len(client.response["checked_locations"]) == 2  # the server picked the restored progress up
        client.ws.send(json.dumps([{"cmd": "LocationChecks", "locations": client.missing[:1]}]))
        time.sleep(1)
    save_now(admin, gid)
    admin.post(f"/api/games/{gid}/stop")
    wait_state(admin, gid, "stopped")
    # restoring over existing progress keeps that progress as a copy too
    admin.post(f"{url}/{manual['name']}/restore")
    assert admin.get(url).json()["backups"][0]["reason"] == "before-restore"
    assert admin.get(f"/api/games/{gid}").json()["progress"]["checked"] == 2

    # only admins, and only real backup names
    assert player.get(url).status_code == 403
    assert player.post(f"{url}/{manual['name']}/restore").status_code == 403
    assert player.get(f"{url}/{manual['name']}").status_code == 403
    assert admin.get(f"{url}/not-a-backup.apsave").status_code == 404
    assert admin.post(f"{url}/multidata.apsave/restore").status_code == 404  # only names of backups are taken
    assert admin.post(f"{url}/..%2Fmultidata.apsave/restore").status_code in (404, 405)  # never leaves the folder
    assert admin.delete(f"{url}/{manual['name']}").json()["backups"][0]["name"] != manual["name"]
    admin.delete(f"/api/games/{gid}")


@pytest.mark.ap
def test_old_backups_are_pruned(admin):
    gid, _job = make_game(admin, names=("Prune",))
    admin.post(f"/api/games/{gid}/start")
    wait_state(admin, gid, "running")
    save_now(admin, gid)
    admin.post(f"/api/games/{gid}/stop")
    wait_state(admin, gid, "stopped")
    for _ in range(23):
        assert admin.post(f"/api/games/{gid}/backups").status_code == 200
    manual = [b for b in admin.get(f"/api/games/{gid}/backups").json()["backups"] if b["reason"] == "manual"]
    assert len(manual) == 20 and len({b["name"] for b in manual}) == 20  # several in one second get their own names
    admin.delete(f"/api/games/{gid}")
