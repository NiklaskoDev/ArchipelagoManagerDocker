"""The options form of the config editor: a game's options as form fields, and yaml <-> form values.

The form's yaml must be one Archipelago accepts: one test hands what the form writes for every installed game to
Archipelago's own option parser, another generates a multiworld from it.
"""
import json
import os
import subprocess

import pytest
import yaml

from conftest import AP_DIR, AP_PYTHON
from helpers import build_apworld, run_job, upload_yaml

pytestmark = pytest.mark.ap


def form_fields(schema):
    return {f["key"]: f for group in schema["groups"] for f in group["options"]}


def write(client, game, options, *, content=None, name="Former", description=""):
    return client.post("/api/configs/form/write", json={"content": content, "game": game, "name": name,
                                                         "description": description, "options": options})


def test_a_game_is_described_as_form_fields(make_user):
    _n, user = make_user()
    listing = {t["game"]: t for t in user.get("/api/templates").json()}
    assert listing["ChecksFinder"]["form"] and listing["ChecksFinder"]["template"]

    r = user.get("/api/templates/options", params={"game": "ChecksFinder"}, headers={"Accept-Encoding": "gzip"})
    assert r.status_code == 200 and r.headers["content-encoding"] == "gzip"
    schema = r.json()
    fields = form_fields(schema)
    assert schema["game"] == "ChecksFinder" and "Item & Location Options" in [g["name"] for g in schema["groups"]]
    assert fields["accessibility"]["type"] == "choice" and ["full", "Full", 0] in fields["accessibility"]["choices"]
    balancing = fields["progression_balancing"]
    assert balancing["type"] == "range" and (balancing["min"], balancing["max"], balancing["default"]) == (0, 99, 50)
    assert ["normal", 50] in balancing["named"] and balancing["random"]
    assert fields["local_items"]["type"] == "set" and fields["local_items"]["names"] == "items"
    assert fields["start_inventory"]["type"] == "counter" and not fields["start_inventory"]["random"]
    assert "Map Width" in schema["items"] and schema["locations"]
    assert fields["accessibility"]["doc"]  # the option's own description

    assert user.get("/api/templates/options", params={"game": "Nope"}).status_code == 404


def test_the_form_writes_a_config_archipelago_generates(make_user, admin):
    _n, user = make_user()
    options = {"accessibility": "minimal", "progression_balancing": "random", "local_items": ["Map Width"],
               "start_inventory": {"Map Height": 2}, "exclude_locations": []}
    r = write(user, "ChecksFinder", options, description="from the form")
    assert r.status_code == 200, r.text
    content = r.json()["content"]
    assert content.startswith("name: Former\ndescription: from the form\ngame: ChecksFinder\nChecksFinder:\n")

    saved = user.post("/api/configs/text", json={"name": "formed", "content": content})
    assert saved.status_code == 200, saved.text
    assert saved.json()["player_names"] == ["Former"] and saved.json()["games"] == ["ChecksFinder"]

    back = user.post("/api/configs/form/read", json={"content": content}).json()
    assert back["name"] == "Former" and back["description"] == "from the form" and back["others"] == []
    assert back["options"]["accessibility"] == {"value": "minimal"}
    assert back["options"]["progression_balancing"] == {"random": True}
    assert back["options"]["local_items"] == {"value": ["Map Width"]}
    assert back["options"]["start_inventory"] == {"value": {"Map Height": 2}}

    job = run_job(admin, [saved.json()["id"]])
    assert job["status"] == "succeeded", job.get("error")
    admin.delete(f"/api/games/{job['game_id']}")  # the shared server has only so many ports for games


def test_what_the_form_cannot_show_stays_in_the_file(make_user):
    _n, user = make_user()
    text = ("# my notes\nname: Keeper\ngame: ChecksFinder\nrequires:\n  version: 0.6.0\n"
            "triggers:\n- option_name: accessibility\n  option_result: minimal\n  options: {}\n"
            "ChecksFinder:\n  accessibility:\n    full: 50\n    minimal: 50\n"
            "  progression_balancing:\n    random-low: 1\n  plando_items:\n  - item: Map Width\n    location: Tile 1\n"
            "  local_items: [Map Width]\n  death_link: true\n")
    form = user.post("/api/configs/form/read", json={"content": text}).json()
    assert form["options"]["accessibility"] == {"kept": "{full: 50, minimal: 50}"}
    assert form["options"]["progression_balancing"] == {"kept": "{random-low: 1}"}
    assert form["options"]["local_items"] == {"value": ["Map Width"]}
    # options without a field and other settings are named, so nobody wonders where they went
    assert form["others"] == ["plando_items", "death_link", "triggers"]

    r = write(user, "ChecksFinder", {"local_items": [], "start_hints": ["Map Bombs"]}, content=text, name="Keeper")
    assert r.status_code == 200, r.text
    doc = yaml.safe_load(r.json()["content"])
    assert list(doc) == ["name", "game", "requires", "triggers", "ChecksFinder"]
    section = doc["ChecksFinder"]
    assert section["accessibility"] == {"full": 50, "minimal": 50} and section["progression_balancing"] == {"random-low": 1}
    assert section["plando_items"] == [{"item": "Map Width", "location": "Tile 1"}] and section["death_link"] is True
    assert section["local_items"] == [] and section["start_hints"] == ["Map Bombs"]
    assert doc["triggers"][0]["option_result"] == "minimal" and "# my notes" not in r.json()["content"]


def test_values_an_option_does_not_take_are_refused(make_user):
    _n, user = make_user()
    for options, words in [({"accessibility": "sometimes"}, "Accessibility"),
                           ({"progression_balancing": 100}, "from 0 to 99"),
                           ({"progression_balancing": True}, "Progression Balancing"),
                           ({"local_items": ["Not An Item"]}, "Not An Item"),
                           ({"start_inventory": {"Map Width": "lots"}}, "whole number"),
                           ({"no_such_option": 1}, "no_such_option")]:
        r = write(user, "ChecksFinder", options)
        assert r.status_code == 400 and words in r.json()["detail"], (options, r.text)
    assert write(user, "ChecksFinder", {}, name="  ").json()["detail"] == "Enter a player name"
    r = write(user, "ChecksFinder", {}, content="name: X\ngame: APQuest\nAPQuest: {}\n")
    assert r.status_code == 400 and "APQuest" in r.json()["detail"]


def test_files_the_form_cannot_edit_say_why(make_user):
    _n, user = make_user()
    read = lambda text: user.post("/api/configs/form/read", json={"content": text})
    several = read("name: A\ngame: ChecksFinder\nChecksFinder: {}\n---\nname: B\ngame: ChecksFinder\nChecksFinder: {}\n")
    assert "several players" in several.json()["unavailable"]
    assert "at random" in read("name: A\ngame: {ChecksFinder: 1, APQuest: 1}\n").json()["unavailable"]
    assert "not installed" in read("name: A\ngame: Not A Game\n").json()["unavailable"]
    assert "which game" in read("name: Player\ngame: \n").json()["unavailable"]
    # one game with weight left over is just that game
    assert read("name: A\ngame: {ChecksFinder: 1, APQuest: 0}\n").json()["game"] == "ChecksFinder"
    bad = read("name: [unclosed\n")
    assert bad.status_code == 400 and "line" in bad.json()["detail"]


def test_every_game_opens_in_the_form_and_its_defaults_are_valid(make_user, tmp_path):
    """Every installed game's template opens in the form without anything the form cannot show, and the yaml the form
    writes with the defaults (and with "random" where an option has it) passes Archipelago's own option checks."""
    _n, user = make_user()
    games = [t["game"] for t in user.get("/api/templates").json()]
    assert len(games) > 50
    written = []
    for game in games:
        schema = user.get("/api/templates/options", params={"game": game}).json()
        fields = form_fields(schema)
        template = user.get("/api/templates/yaml", params={"game": game}).text
        form = user.post("/api/configs/form/read", json={"content": template}).json()
        assert not [key for key, value in form["options"].items() if "kept" in value], game
        for values in ({k: f["default"] for k, f in fields.items()},
                       {k: "random" if f["random"] else f["default"] for k, f in fields.items()}):
            r = write(user, game, values, name="Check")
            assert r.status_code == 200, (game, r.text)
            path = tmp_path / f"{len(written)}.yaml"
            path.write_text(r.json()["content"], encoding="utf-8")
            written.append(str(path))

    check = ("import json, sys, yaml\nimport Generate\nfailed = []\nfor path in sys.argv[1:]:\n"
             "    try:\n        Generate.roll_settings(yaml.safe_load(open(path, encoding='utf-8')))\n"
             "    except Exception as e:\n        failed.append((path, repr(e), repr(e.__cause__)))\n"
             "print('@@' + json.dumps(failed))\n")
    result = subprocess.run([AP_PYTHON, "-c", check, *written], cwd=AP_DIR, capture_output=True, text=True, timeout=600,
                            env={**os.environ, "SKIP_REQUIREMENTS_UPDATE": "true"})
    line = next((ln for ln in result.stdout.splitlines() if ln.startswith("@@")), None)
    assert line, result.stderr[-2000:]
    assert json.loads(line[2:]) == []


def test_a_custom_game_gets_a_form_with_its_own_options(admin, make_user, tmp_path):
    _n, user = make_user()
    game = "Formed Checks"
    with open(build_apworld(tmp_path / "formedchecks.apworld", game=game, module="formedchecks", with_options=True), "rb") as f:
        r = admin.post("/api/apworlds", files={"file": ("formedchecks.apworld", f.read())})
    assert r.status_code == 200 and r.json()["load_error"] is None, r.text
    try:
        listing = {t["game"]: t for t in user.get("/api/templates").json()}
        assert listing[game]["form"] and listing[game]["source"] == "custom"
        schema = user.get("/api/templates/options", params={"game": game}).json()
        fields = form_fields(schema)
        assert schema["groups"][0]["name"] == "Game Options"
        shape = fields["board_shape"]
        assert shape["label"] == "Board shape" and shape["choices"] == [["square", "Square", 0], ["wide", "Wide", 1]]
        assert shape["aliases"]["classic"] == "square" and "**Wide:**" in shape["doc"]
        assert (fields["board_size"]["min"], fields["board_size"]["max"], fields["board_size"]["default"]) == (5, 20, 10)
        assert fields["hardcore"]["type"] == "toggle" and fields["hardcore"]["default"] is False
        assert fields["colors"]["keys"] == ["Red", "Green", "Blue"] and fields["colors"]["default"] == ["Red"]

        # an alias in the yaml shows as the choice it stands for
        form = user.post("/api/configs/form/read", json={"content": f"name: C\ngame: {game}\n{game}:\n  board_shape: classic\n"})
        assert form.json()["options"]["board_shape"] == {"value": "square"}

        r = write(user, game, {"board_shape": "wide", "board_size": 12, "hardcore": True, "colors": ["Blue", "Red"]})
        assert r.status_code == 200, r.text
        cfg = upload_yaml(user, "formed", r.json()["content"]).json()["created"][0]
        assert cfg["games"] == [game] and cfg["missing_games"] == []
        job = run_job(admin, [cfg["id"]])
        assert job["status"] == "succeeded", job.get("error")
        admin.delete(f"/api/games/{job['game_id']}")
    finally:
        for item in admin.get("/api/apworlds").json()["items"]:
            if item["game"] == game:
                admin.delete(f"/api/apworlds/{item['id']}")
