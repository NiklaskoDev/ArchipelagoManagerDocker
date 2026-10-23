"""Base ROMs uploaded through the web UI: checked against the checksums the game knows, stored where it looks."""
import pytest

from helpers import build_apworld, player_yaml, run_job, upload_yaml

pytestmark = pytest.mark.ap

GAME = "Rom Checks"
ROM = b"the base rom of a test game " * 200  # its checksum is the one the test world knows


def roms(client):
    r = client.get("/api/roms")
    assert r.status_code == 200, r.text
    return r.json()


def rom_of(client, game):
    return next(r for r in roms(client) if r["game"] == game)


def upload(client, rom, content, name="dump.bin"):
    return client.post("/api/roms", data={"game": rom["game"], "setting": rom["setting"] or ""},
                       files={"file": (name, content)})


def test_the_games_say_which_rom_they_need_and_whether_for_generating(admin):
    by_setting = {r["setting"]: r for r in roms(admin)}
    lttp = by_setting["lttp_options.rom_file"]
    assert lttp["game"] == "A Link to the Past" and lttp["needed"] and lttp["checked"]
    assert not lttp["exists"] and lttp["valid"] is None
    # where Archipelago keeps user files: in the image that is in the data folder, here it is the Archipelago folder
    assert lttp["file"] == "Zelda no Densetsu - Kamigami no Triforce (Japan).sfc"
    # players patch Emerald with their own ROM; the server can generate without it
    assert not by_setting["pokemon_emerald_settings.rom_file"]["needed"]
    # the games that need a ROM to generate come with the same place as before
    created = admin.post("/api/configs", files=[("files", ("z1.yaml", player_yaml("Link", "The Legend of Zelda")))])
    config = created.json()["created"][0]
    assert config["missing_roms"][0]["setting"] == "tloz_options.rom_file"
    admin.delete(f"/api/configs/{config['id']}")


def test_an_admin_uploads_a_rom_and_the_game_generates(ap_server, admin, make_user, tmp_path):
    _name, player = make_user("rom")
    assert player.get("/api/roms").status_code == 403
    world = build_apworld(tmp_path / "romchecks.apworld", game=GAME, module="romchecks", rom=ROM)
    with open(world, "rb") as f:
        installed = admin.post("/api/apworlds", files={"file": ("romchecks.apworld", f.read())})
    assert installed.status_code == 200, installed.text
    try:
        rom = rom_of(admin, GAME)
        assert (rom["setting"], rom["file"], rom["needed"], rom["exists"]) == \
            ("romchecks_options.rom_file", f"{GAME} (Test).rom", True, False)
        cfg = upload_yaml(admin, "romplayer", player_yaml("RomPlayer", GAME)).json()["created"][0]
        assert cfg["missing_roms"][0]["game"] == GAME
        r = admin.post("/api/jobs", json={"config_ids": [cfg["id"]], "host_after": False})
        assert r.status_code == 409 and "base ROM" in r.json()["detail"]

        # a wrong file is refused and not kept; players cannot upload at all
        r = upload(admin, rom, b"not the right game")
        assert r.status_code == 400 and "checksum" in r.json()["detail"], r.text
        assert not rom_of(admin, GAME)["exists"]
        assert upload(admin, rom, b"").status_code == 400
        assert upload(player, rom, ROM).status_code == 403
        assert admin.post("/api/roms", data={"game": "ChecksFinder", "setting": ""},
                          files={"file": ("x", ROM)}).status_code == 404

        # the right one goes where the game looks, under the name it expects, whatever the upload was called
        r = upload(admin, rom, ROM, name="my own dump.bin")
        assert r.status_code == 200, r.text
        rom = rom_of(admin, GAME)
        assert rom["exists"] and rom["valid"] is True
        stored = ap_server.data_dir / rom["folder"] / rom["file"]
        assert stored.read_bytes() == ROM
        # the warning is gone
        assert not next(c for c in admin.get("/api/configs").json() if c["id"] == cfg["id"])["missing_roms"]
        job = run_job(admin, [cfg["id"]])
        assert job["status"] == "succeeded", job["error"]

        # removing it brings the warning back
        r = admin.delete("/api/roms", params={"game": GAME, "setting": rom["setting"]})
        assert r.status_code == 200 and not stored.exists()
        assert player.delete("/api/roms", params={"game": GAME, "setting": rom["setting"]}).status_code == 403
        assert admin.post("/api/jobs", json={"config_ids": [cfg["id"]], "host_after": False}).status_code == 409
        admin.delete(f"/api/games/{job['game_id']}")
        admin.delete(f"/api/configs/{cfg['id']}")
    finally:
        admin.delete(f"/api/apworlds/{installed.json()['id']}")
