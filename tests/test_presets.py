"""Templates of generation options: saved under a name by those who generate, shared by organizers, and the server
settings they carry given to the game a generation or a sign-up round makes."""
import pytest

from helpers import make_game, player_yaml, run_job, upload_yaml

RACE = {"spoiler": 0, "race": True, "skip_prog_balancing": False, "plando": ["bosses"], "host_after": False,
        "meta": None, "ap_version": None,
        "server_options": {"hint_cost": 25, "release_mode": "disabled", "disable_item_cheat": True}}


def setup(server):
    admin = server.client()
    assert admin.post("/api/auth/setup", json={"username": "boss", "password": "bosspass123",
                                               "code": server.setup_code()}).status_code == 200
    return admin


def user(server, admin, name, role="user"):
    r = admin.post("/api/users", json={"username": name, "password": "password123", "role": role})
    assert r.status_code == 200, r.text
    return r.json()["id"], server.client(name, "password123")


def test_templates_are_saved_shared_and_kept_to_their_makers(plain_server):
    admin = setup(plain_server)
    olga_id, olga = user(plain_server, admin, "olga", "organizer")
    ann_id, ann = user(plain_server, admin, "ann")
    _bob_id, bob = user(plain_server, admin, "bob")

    # an organizer's template with settings for the new game, shared with everyone who generates
    r = olga.post("/api/presets", json={"name": "  Race   night ", "options": RACE, "shared": True})
    assert r.status_code == 200, r.text
    race = r.json()
    assert race["name"] == "Race night" and race["shared"] and race["mine"] and race["can_edit"]
    server_options = race["options"]["server_options"]
    assert server_options["hint_cost"] == 25 and server_options["release_mode"] == "disabled"
    assert server_options["collect_mode"] == "auto"  # the rest from the defaults for new games
    listing = olga.get("/api/presets").json()
    assert [p["name"] for p in listing["presets"]] == ["Race night"]
    assert listing["server_defaults"]["hint_cost"] == 10

    # players use templates only where they may generate
    assert ann.get("/api/presets").status_code == 403
    assert admin.patch("/api/settings", json={"users_can_generate": True}).status_code == 200
    listing = ann.get("/api/presets").json()
    shared = listing["presets"][0]
    assert shared["owner"] == "olga" and not shared["mine"] and not shared["can_edit"]
    assert listing["server_defaults"] is None  # a player who generates does not set the game up
    assert ann.put(f"/api/presets/{race['id']}", json={"name": "Mine now"}).status_code == 403
    assert ann.delete(f"/api/presets/{race['id']}").status_code == 403

    # a player's own: generation options only, never shared
    for body, status, says in (
        ({"name": "Async", "options": RACE}, 400, "set up the games"),
        ({"name": "Async", "options": {}, "shared": True}, 403, "share"),
        ({"name": "", "options": {}}, 400, "name"),
        ({"name": "Async", "options": {"seed": 5}}, 400, "Unknown option"),
        ({"name": "Async", "options": {"spoiler": 7}}, 400, "Spoiler"),
        ({"name": "Async", "options": {"meta": "null:\n  progression_balancing: 50"}}, 400, "meta_description"),
        ({"name": "Async", "options": {"ap_version": "latest"}}, 400, "0.6.2"),
    ):
        r = ann.post("/api/presets", json=body)
        assert r.status_code == status and says in r.json()["detail"], (body, r.text)
    meta = "meta_description: everyone balanced\nnull:\n  progression_balancing: 50\n"
    r = ann.post("/api/presets", json={"name": "Async", "options": {"spoiler": 2, "meta": meta, "ap_version": "0.6.2"}})
    assert r.status_code == 200, r.text
    mine = r.json()
    assert mine["options"]["meta"] == meta and mine["options"]["ap_version"] == "0.6.2"
    assert mine["options"]["plando"] == ["bosses", "texts", "connections"] and mine["options"]["server_options"] is None
    assert ann.post("/api/presets", json={"name": "ASYNC", "options": {}}).status_code == 409  # names are hers alone
    r = ann.put(f"/api/presets/{mine['id']}", json={"options": {**mine["options"], "race": True}})
    assert r.status_code == 200 and r.json()["options"]["race"]
    # another player sees the shared template, not hers
    assert [p["name"] for p in bob.get("/api/presets").json()["presets"]] == ["Race night"]
    assert bob.delete(f"/api/presets/{mine['id']}").status_code == 404

    # an administrator changes a shared template, keeping what its maker may do
    assert admin.put(f"/api/presets/{race['id']}", json={"name": "Race"}).json()["name"] == "Race"
    actions = [e["action"] for e in admin.get("/api/audit").json()["entries"]]
    assert "job.preset_add" in actions and "job.preset_change" in actions

    # deleting a user deletes their own templates; the shared ones stay for everyone
    _olga_private = olga.post("/api/presets", json={"name": "Olga's own", "options": {}}).json()
    assert admin.delete(f"/api/users/{ann_id}").status_code == 200
    assert admin.delete(f"/api/users/{olga_id}").status_code == 200
    assert [p["name"] for p in bob.get("/api/presets").json()["presets"]] == ["Race"]
    assert bob.get("/api/presets").json()["presets"][0]["owner"] is None
    assert admin.delete(f"/api/presets/{race['id']}").status_code == 200
    assert bob.get("/api/presets").json()["presets"] == []


def test_a_sign_up_round_keeps_its_settings_for_the_new_game(plain_server):
    admin = setup(plain_server)
    r = admin.post("/api/signups", json={"name": "Race round", "options": {"race": True, "server_options": {"hint_cost": 30}}})
    assert r.status_code == 200, r.text
    options = r.json()["options"]
    assert options["race"] and options["server_options"]["hint_cost"] == 30
    r = admin.patch(f"/api/signups/{r.json()['id']}", json={"options": {"server_options": {"hint_cost": 500}}})
    assert r.status_code == 400 and "hint_cost" in r.json()["detail"]
    assert admin.post("/api/signups", json={"name": "Plain"}).json()["options"]["server_options"] is None


@pytest.mark.ap
def test_a_generated_game_gets_the_settings_of_its_template(admin, make_user):
    preset = admin.post("/api/presets", json={"name": "Hard hints", "options": RACE}).json()
    gid, job = make_game(admin, names=("Templated",), server_options=preset["options"]["server_options"])
    assert job["options"]["server_options"]["hint_cost"] == 25
    options = admin.get(f"/api/games/{gid}").json()["options"]
    assert options["hint_cost"] == 25 and options["release_mode"] == "disabled" and options["disable_item_cheat"]
    admin.delete(f"/api/games/{gid}")
    admin.delete(f"/api/presets/{preset['id']}")

    # a player who may generate gets the defaults: setting up the game is for organizers
    name, player = make_user("gen")
    assert admin.patch("/api/settings", json={"users_can_generate": True}).status_code == 200
    try:
        config = upload_yaml(player, f"{name}-config", player_yaml("Generator")).json()["created"][0]["id"]
        r = player.post("/api/jobs", json={"config_ids": [config], "server_options": {"hint_cost": 1}})
        assert r.status_code == 403
        job = run_job(player, [config])
        assert job["status"] == "succeeded", job
        assert admin.get(f"/api/games/{job['game_id']}").json()["options"]["hint_cost"] == 10
        admin.delete(f"/api/games/{job['game_id']}")
    finally:
        admin.patch("/api/settings", json={"users_can_generate": False})
