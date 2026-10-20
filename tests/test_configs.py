"""Per-user player configs and templates (needs Archipelago for templates and missing-game detection)."""
import pytest

from helpers import player_yaml, upload_yaml

pytestmark = pytest.mark.ap


def test_upload_list_edit_download_delete(make_user):
    _name, user = make_user()
    r = upload_yaml(user, "mine", player_yaml("Zed"))
    assert r.status_code == 200, r.text
    cfg = r.json()["created"][0]
    assert cfg["name"] == "mine.yaml" and cfg["games"] == ["ChecksFinder"] and cfg["player_names"] == ["Zed"]
    assert cfg["missing_games"] == []

    listing = user.get("/api/configs").json()
    assert [c["id"] for c in listing] == [cfg["id"]]

    detail = user.get(f"/api/configs/{cfg['id']}").json()
    assert "name: Zed" in detail["content"]

    edited = detail["content"].replace("Zed", "Zoe")
    r = user.put(f"/api/configs/{cfg['id']}", json={"content": edited, "name": "renamed"})
    assert r.status_code == 200 and r.json()["name"] == "renamed.yaml" and r.json()["player_names"] == ["Zoe"]

    dl = user.get(f"/api/configs/{cfg['id']}/download")
    assert dl.status_code == 200 and b"name: Zoe" in dl.content and "renamed.yaml" in dl.headers["content-disposition"]

    assert user.put(f"/api/configs/{cfg['id']}", json={"content": "game: [broken"}).status_code == 400
    assert "Zoe" in user.get(f"/api/configs/{cfg['id']}").json()["content"]  # failed save changed nothing

    assert user.delete(f"/api/configs/{cfg['id']}").status_code == 200
    assert user.get(f"/api/configs/{cfg['id']}").status_code == 404


def test_configs_are_private_to_their_owner(make_user, admin):
    _a, alice = make_user("alice")
    _b, bob = make_user("bob")
    cfg = upload_yaml(alice, "secret", player_yaml("Alice")).json()["created"][0]
    for call in (lambda: bob.get(f"/api/configs/{cfg['id']}"), lambda: bob.get(f"/api/configs/{cfg['id']}/download"),
                 lambda: bob.put(f"/api/configs/{cfg['id']}", json={"name": "x"}), lambda: bob.delete(f"/api/configs/{cfg['id']}")):
        assert call().status_code == 404
    assert cfg["id"] not in [c["id"] for c in bob.get("/api/configs").json()]
    assert cfg["id"] not in [c["id"] for c in bob.get("/api/configs?all=true").json()]  # ?all is admin-only
    assert cfg["id"] in [c["id"] for c in admin.get("/api/configs?all=true").json()]
    assert admin.get(f"/api/configs/{cfg['id']}").status_code == 200


def test_upload_reports_bad_files_individually(make_user):
    _n, user = make_user()
    files = [("files", ("good.yaml", player_yaml("Good"))), ("files", ("bad.yaml", "game: [")),
             ("files", ("nogame.yml", "name: x")), ("files", ("binary.yaml", b"\xff\xfe\x00\x01"))]
    r = user.post("/api/configs", files=files)
    assert r.status_code == 200
    body = r.json()
    assert [c["name"] for c in body["created"]] == ["good.yaml"]
    assert sorted(e["filename"] for e in body["errors"]) == ["bad.yaml", "binary.yaml", "nogame.yml"]
    only_bad = user.post("/api/configs", files=[("files", ("bad.yaml", "game: ["))])
    assert only_bad.status_code == 400


def test_duplicate_names_are_numbered_and_paths_are_neutralized(make_user):
    _n, user = make_user()
    names = [upload_yaml(user, "same", player_yaml(f"P{i}")).json()["created"][0]["name"] for i in range(3)]
    assert names == ["same.yaml", "same (2).yaml", "same (3).yaml"]
    r = user.post("/api/configs", files=[("files", ("../../etc/evil.yaml", player_yaml("Evil")))])
    assert r.json()["created"][0]["name"] == "evil.yaml"


def test_unknown_games_are_flagged_and_listed_as_missing(make_user, admin):
    name, user = make_user()
    cfg = upload_yaml(user, "mystery", player_yaml("Mystic", game="Not A Real Game")).json()["created"][0]
    assert cfg["missing_games"] == ["Not A Real Game"]
    missing = user.get("/api/missing-games").json()
    entry = next(m for m in missing if m["game"] == "Not A Real Game")
    assert entry["configs"][0]["name"] == "mystery.yaml" and entry["games"] == []
    # another user does not see it, the admin does (with the owner's name)
    _o, other = make_user()
    assert all(m["game"] != "Not A Real Game" for m in other.get("/api/missing-games").json())
    admin_entry = next(m for m in admin.get("/api/missing-games").json() if m["game"] == "Not A Real Game")
    assert any(c["owner"] == name for c in admin_entry["configs"])


def test_templates(make_user):
    _n, user = make_user()
    listing = user.get("/api/templates").json()
    games = {t["game"]: t for t in listing}
    assert len(games) > 50 and "ChecksFinder" in games and "Archipelago" not in games
    text = user.get("/api/templates/yaml", params={"game": "ChecksFinder"})
    assert text.status_code == 200 and "game: ChecksFinder" in text.text and not text.text.startswith("﻿")
    assert user.get("/api/templates/yaml", params={"game": "Nope"}).status_code == 404
    # a template, with the name filled in, is a valid config as is
    body = text.text.replace("name: Player{number}", "name: Templated")
    r = user.post("/api/configs/text", json={"name": "from template", "content": body})
    assert r.status_code == 200, r.text
    assert r.json()["player_names"] == ["Templated"] and r.json()["name"] == "from template.yaml"


def test_every_template_is_a_valid_config(make_user):
    """Guards the yaml analysis against real-world files: each shipped template must be accepted."""
    _n, user = make_user()
    failures = []
    for t in user.get("/api/templates").json():
        text = user.get("/api/templates/yaml", params={"game": t["game"]}).text
        r = user.post("/api/configs/text", json={"name": "t", "content": text})
        if r.status_code != 200:
            failures.append((t["game"], r.json().get("detail")))
        elif r.json()["games"] != [t["game"]]:
            failures.append((t["game"], r.json()["games"]))
    assert not failures, failures[:5]


def test_a_file_without_the_game_section_is_stored_with_a_warning_but_cannot_be_generated(make_user, admin):
    _n, user = make_user()
    r = upload_yaml(user, "bare", "name: Bare\ngame: ChecksFinder\n")
    cfg = r.json()["created"][0]
    detail = user.get(f"/api/configs/{cfg['id']}").json()
    assert any("ChecksFinder:" in w for w in detail["warnings"])
    job = admin.post("/api/jobs", json={"config_ids": [cfg["id"]], "host_after": False})
    assert job.status_code == 400 and "ChecksFinder:" in job.json()["detail"]
