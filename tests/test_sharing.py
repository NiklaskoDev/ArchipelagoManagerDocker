"""Players share configs; the others look at them and copy them into their own library."""
import pytest

from helpers import player_yaml, upload_yaml

pytestmark = pytest.mark.ap

ORIGINAL = "# tuned for a short game\n" + player_yaml("Sharer", extra="description: Quick one\n")
TWO_PLAYERS = player_yaml("First") + "---\n" + player_yaml("Second", "APQuest")


def upload(client, name, content):
    r = upload_yaml(client, name, content)
    assert r.status_code == 200, r.text
    return r.json()["created"][0]


def share(client, config, shared=True):
    return client.put(f"/api/configs/{config['id']}/shared", json={"shared": shared})


def listed(client, config):
    return next((c for c in client.get("/api/configs/shared").json() if c["id"] == config["id"]), None)


def test_players_share_configs_and_copy_them(ap_server, admin, make_user):
    ann_name, ann = make_user("ann")
    _bob_name, bob = make_user("bob")
    mine = upload(ann, "short", ORIGINAL)
    assert mine["shared"] is False

    # a config nobody shared stays private
    for path in (f"/api/configs/shared/{mine['id']}", f"/api/configs/{mine['id']}/download"):
        assert bob.get(path).status_code == 404
    assert bob.post(f"/api/configs/{mine['id']}/copy", json={}).status_code == 404
    assert share(bob, mine).status_code == 404
    assert listed(bob, mine) is None
    # only its owner offers it; an administrator can only take it back
    assert share(admin, mine).status_code == 403

    r = share(ann, mine)
    assert r.status_code == 200 and r.json()["shared"] is True and r.json()["shared_at"]
    assert next(c for c in ann.get("/api/configs").json() if c["id"] == mine["id"])["shared"] is True
    seen = listed(bob, mine)
    assert (seen["owner"], seen["mine"], seen["player_names"], seen["description"]) == (ann_name, False, ["Sharer"],
                                                                                        "Quick one")
    assert listed(ann, mine)["mine"] is True
    detail = bob.get(f"/api/configs/shared/{mine['id']}").json()
    assert detail["content"] == ORIGINAL
    assert bob.get(f"/api/configs/{mine['id']}/download").text == ORIGINAL
    # looking is not editing
    assert bob.get(f"/api/configs/{mine['id']}").status_code == 404
    assert bob.put(f"/api/configs/{mine['id']}", json={"content": player_yaml("Mine")}).status_code == 404
    assert bob.delete(f"/api/configs/{mine['id']}").status_code == 404

    # a copy with your own player name keeps the rest of the file, comments included
    r = bob.post(f"/api/configs/{mine['id']}/copy", json={"player_name": "Bobby"})
    assert r.status_code == 200, r.text
    copy = r.json()
    assert (copy["name"], copy["player_names"], copy["shared"]) == ("short.yaml", ["Bobby"], False)
    assert bob.get(f"/api/configs/{copy['id']}").json()["content"] == ORIGINAL.replace("name: Sharer", 'name: "Bobby"')
    # or as it is
    plain = bob.post(f"/api/configs/{mine['id']}/copy", json={}).json()
    assert plain["name"] == "short (2).yaml" and plain["player_names"] == ["Sharer"]
    for bad in ("{player}", "x" * 17):
        r = bob.post(f"/api/configs/{mine['id']}/copy", json={"player_name": bad})
        assert r.status_code == 400 and "player name" in r.json()["detail"]

    # the copy and the original go their own ways
    assert ann.put(f"/api/configs/{mine['id']}", json={"content": player_yaml("Changed")}).status_code == 200
    assert bob.get(f"/api/configs/{copy['id']}").json()["player_names"] == ["Bobby"]
    assert bob.put(f"/api/configs/{copy['id']}", json={"content": player_yaml("Bob2")}).status_code == 200
    assert ann.get(f"/api/configs/{mine['id']}").json()["player_names"] == ["Changed"]

    # a file with several players is copied as it is
    both = upload(ann, "both", TWO_PLAYERS)
    share(ann, both)
    r = bob.post(f"/api/configs/{both['id']}/copy", json={"player_name": "Bobby"})
    assert r.status_code == 400 and "several players" in r.json()["detail"]
    assert bob.post(f"/api/configs/{both['id']}/copy", json={}).json()["player_names"] == ["First", "Second"]

    # a disabled player's configs are not offered
    ann_id = next(u["id"] for u in admin.get("/api/users").json() if u["username"] == ann_name)
    admin.patch(f"/api/users/{ann_id}", json={"disabled": True})
    assert listed(bob, mine) is None and bob.get(f"/api/configs/shared/{mine['id']}").status_code == 404
    admin.patch(f"/api/users/{ann_id}", json={"disabled": False})
    assert listed(bob, mine) is not None
    ann = ap_server.client(ann_name, "password123")  # disabling the account signed it out

    # its owner or an administrator takes it back
    assert share(ann, mine, False).json()["shared"] is False
    assert listed(bob, mine) is None and bob.post(f"/api/configs/{mine['id']}/copy", json={}).status_code == 404
    assert share(admin, both, False).status_code == 200 and listed(bob, both) is None
    # the copies stay
    assert {c["id"] for c in bob.get("/api/configs").json()} >= {copy["id"], plain["id"]}
