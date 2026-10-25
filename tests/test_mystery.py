"""Chance in configs (mystery.py): rolling a file with weights as the generator does, and mystery rounds, where every
player gets what one file of weights rolls for them."""
import time

import pytest
import yaml

from helpers import player_yaml, upload_yaml, wait_for

pytestmark = pytest.mark.ap

WEIGHTS = """name: Somebody
description: Anything goes
game:
  ChecksFinder: 1
  APQuest: 1
ChecksFinder:
  progression_balancing: random-range-20-40
APQuest:
  progression_balancing: random-range-60-80
  accessibility:
    full: 1
    minimal: 1
"""


def test_a_file_with_weights_is_rolled_as_the_generator_rolls_it(admin, make_user):
    _name, ann = make_user("ann")
    seen = set()
    for _ in range(6):
        r = ann.post("/api/configs/roll", json={"content": WEIGHTS})
        assert r.status_code == 200, r.text
        result = r.json()
        doc = yaml.safe_load(result["yaml"])
        game = doc["game"]
        assert result["rolled"] == [{"name": "Somebody", "game": game}] and game in ("ChecksFinder", "APQuest")
        assert doc["description"] == "Anything goes" and "random" not in result["yaml"]
        balancing = doc[game]["progression_balancing"]
        assert (20 <= balancing <= 40) if game == "ChecksFinder" else (60 <= balancing <= 80), result["yaml"]
        if game == "APQuest":
            assert doc[game]["accessibility"] in ("full", "minimal")
        seen.add(game)
        if len(seen) == 2:
            break
    # a rolled file is a config like any other
    r = upload_yaml(ann, "rolled", result["yaml"])
    assert r.status_code == 200 and r.json()["created"][0]["games"] == [game]

    # several players at once; and what the generator would refuse
    r = ann.post("/api/configs/roll", json={"content": WEIGHTS + "---\n" + player_yaml("Other")})
    assert [p["name"] for p in r.json()["rolled"]] == ["Somebody", "Other"]
    r = ann.post("/api/configs/roll", json={"content": player_yaml("Nobody", "Not A Game")})
    assert r.status_code == 400 and r.json()["detail"].startswith("This file cannot be rolled: ")
    assert ann.post("/api/configs/roll", json={"content": "name: [unclosed"}).status_code == 400


def test_in_a_mystery_round_everyone_joins_with_a_name_and_gets_what_the_weights_roll(admin, make_user):
    _org_name, org = make_user("org", role="organizer")
    ann_name, ann = make_user("ann")
    bob_name, bob = make_user("bob")
    name = f"Mystery {time.time_ns()}"
    assert org.post("/api/signups", json={"name": name, "mystery": True}).status_code == 400
    r = org.post("/api/signups", json={"name": name, "mystery": True,
                                       "mystery_weights": WEIGHTS.replace("APQuest: 1", "Not A Game: 1") + "Not A Game: {}\n"})
    assert r.status_code == 409 and r.json()["missing_games"] == ["Not A Game"]
    r = org.post("/api/signups", json={"name": name, "mystery": True, "mystery_weights": "name: A\n---\nname: B\n"})
    assert r.status_code == 400
    r = org.post("/api/signups", json={"name": name, "mystery": True, "mystery_weights": WEIGHTS,
                                       "allowed_games": ["Clique"], "configs_per_user": 2})
    assert r.status_code == 200, r.text
    signup = r.json()
    sid = signup["id"]
    assert signup["mystery"] and signup["mystery_games"] == ["APQuest", "ChecksFinder"] and signup["allowed_games"] == []

    # players join with a name only; their configs are not wanted here
    own = upload_yaml(ann, f"own-{time.time_ns()}", player_yaml("AnnOwn")).json()["created"][0]["id"]
    r = ann.post(f"/api/signups/{sid}/entries", json={"config_id": own})
    assert r.status_code == 409 and "mystery round" in r.json()["detail"]
    r = ann.post(f"/api/signups/{sid}/mystery-entries", json={"player_name": "AnnM"})
    assert r.status_code == 200, r.text
    assert bob.post(f"/api/signups/{sid}/mystery-entries", json={"player_name": "annm"}).status_code == 409
    assert bob.post(f"/api/signups/{sid}/mystery-entries", json={"player_name": "{player}"}).status_code == 400
    r = bob.post(f"/api/signups/{sid}/mystery-entries", json={"player_name": "BobM"})
    assert r.status_code == 200, r.text
    entries = {e["username"]: e for e in r.json()["entries"]}
    assert entries[bob_name]["player_names"] == ["BobM"] and entries[bob_name]["games"] == ["APQuest", "ChecksFinder"]
    mine = ann.get(f"/api/signups/{sid}/entries/{entries[ann_name]['id']}/download").text
    assert yaml.safe_load(mine)["name"] == "AnnM" and "Anything goes" in mine
    assert "random-range-20-40" in bob.get(f"/api/signups/{sid}/mystery.yaml").text  # what one may get is no secret

    # the weights change for everyone; a round with entries stays a mystery round
    narrower = WEIGHTS.replace("  APQuest: 1\n", "")
    r = org.patch(f"/api/signups/{sid}", json={"mystery_weights": narrower})
    assert r.status_code == 200, r.text
    assert r.json()["mystery_games"] == ["ChecksFinder"]
    assert {tuple(e["games"]) for e in r.json()["entries"]} == {("ChecksFinder",)}
    assert yaml.safe_load(ann.get(f"/api/signups/{sid}/entries/{entries[ann_name]['id']}/download").text)["name"] == "AnnM"
    assert org.patch(f"/api/signups/{sid}", json={"mystery": False}).status_code == 409
    assert ann.patch(f"/api/signups/{sid}", json={"mystery_weights": WEIGHTS}).status_code == 403
    org.patch(f"/api/signups/{sid}", json={"mystery_weights": WEIGHTS})

    # the generator rolls a game for each of them
    assert org.post(f"/api/signups/{sid}/close").status_code == 200
    done = wait_for(lambda: (s := org.get(f"/api/signups/{sid}").json())["status"] in ("done", "closed") and s,
                    timeout=240)
    assert done["status"] == "done", done
    game = ann.get(f"/api/games/{done['game_id']}").json()
    players = {s["name"]: s for s in game["slots"]}
    assert set(players) == {"AnnM", "BobM"} and players["AnnM"]["owner"] == ann_name
    assert {s["game"] for s in players.values()} <= {"APQuest", "ChecksFinder"}
    admin.delete(f"/api/games/{done['game_id']}")


def test_a_round_becomes_a_mystery_round_only_while_nobody_entered(admin, make_user):
    _name, ann = make_user("ann")
    r = admin.post("/api/signups", json={"name": f"Plain {time.time_ns()}", "allowed_games": ["ChecksFinder"]})
    sid = r.json()["id"]
    assert not r.json()["mystery"]
    assert admin.post(f"/api/signups/{sid}/mystery-entries", json={"player_name": "X"}).status_code == 409
    r = admin.patch(f"/api/signups/{sid}", json={"mystery": True, "mystery_weights": WEIGHTS})
    assert r.status_code == 200 and r.json()["mystery"] and r.json()["allowed_games"] == []
    r = admin.patch(f"/api/signups/{sid}", json={"mystery": False})
    assert r.status_code == 200 and not r.json()["mystery"]
    assert admin.get(f"/api/signups/{sid}/mystery.yaml").status_code == 404
    admin.delete(f"/api/signups/{sid}")
