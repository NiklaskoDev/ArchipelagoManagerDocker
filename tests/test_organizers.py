"""Organizers: they add games, generate and open sign-up rounds, and run those like administrators, without
administering the server (users, settings, custom games, ROMs, Archipelago versions)."""
import time

import pytest

from helpers import make_game, player_yaml, pregenerated_zip, upload_yaml, wait_for

pytestmark = pytest.mark.ap


def own_config(client, player, game="ChecksFinder"):
    r = upload_yaml(client, f"{player}-{time.time_ns()}", player_yaml(player, game))
    assert r.status_code == 200, r.text
    return r.json()["created"][0]["id"]


def test_an_organizer_runs_the_games_they_add_and_nothing_else(ap_server, admin, make_user):
    org_name, org = make_user("org", role="organizer")
    _other_name, other_org = make_user("org", role="organizer")
    ann_name, ann = make_user("ann")
    assert org.get("/api/auth/status").json()["permissions"]["organize"] is True
    assert ann.get("/api/auth/status").json()["permissions"]["organize"] is False
    admins_game, _job = make_game(admin, names=("Lead", "Follow"))
    zipped = pregenerated_zip(admin, admins_game, with_zip_slip=False)
    try:
        # players cannot add games; organizers can, and run them
        assert ann.post("/api/games", files={"file": ("seed.zip", zipped)}).status_code == 403
        r = org.post("/api/games", files={"file": ("seed.zip", zipped)}, data={"name": "Org's game"})
        assert r.status_code == 200, r.text
        game = r.json()
        gid = game["id"]
        assert game["can_manage"] is True and game["notes"] is None and "server_password" in game
        assert [g["id"] for g in org.get("/api/games").json()] == [gid]  # not the admin's game
        assert other_org.get(f"/api/games/{gid}").status_code == 404

        r = org.patch(f"/api/games/{gid}", json={"notes": "only for me", "password": "letmein", "remind_days": 4})
        assert r.status_code == 200, r.text
        assert (r.json()["notes"], r.json()["password"], r.json()["remind_days"]) == ("only for me", "letmein", 4)
        # within what the container publishes, and Discord's webhooks only (the manager posts to the address)
        assert org.patch(f"/api/games/{gid}", json={"port": 1999}).status_code == 400
        taken = {g["port"] for g in admin.get("/api/games").json()}
        port = next(p for p in range(ap_server.game_port_start, ap_server.game_port_start + 20) if p not in taken)
        assert org.patch(f"/api/games/{gid}", json={"port": port}).status_code == 200
        assert org.patch(f"/api/games/{gid}", json={"discord_webhook_url": "http://10.0.0.1/hook"}).status_code == 400
        hook = "https://discord.com/api/webhooks/123/abc"
        assert org.patch(f"/api/games/{gid}", json={"discord_webhook_url": hook}).json()["discord_webhook_url"] == hook
        assert admin.patch(f"/api/games/{gid}", json={"discord_webhook_url": "http://10.0.0.1/hook"}).status_code == 200
        # an administrator's address stays when the organizer saves the form with it unchanged
        assert org.patch(f"/api/games/{gid}", json={"discord_webhook_url": "http://10.0.0.1/hook", "name": "Renamed"}
                         ).status_code == 200
        assert org.patch(f"/api/games/{gid}", json={"discord_webhook_url": ""}).status_code == 200

        # they give slots to players, who then see the game, but not its notes
        users = org.get("/api/users/names").json()
        ann_id = next(u["id"] for u in users if u["username"] == ann_name)
        assert set(users[0]) == {"id", "username"}
        slot = game["slots"][0]["slot"]
        assert org.post(f"/api/games/{gid}/slots/{slot}/assign", json={"user_id": ann_id}).status_code == 200
        seen = ann.get(f"/api/games/{gid}").json()
        assert seen["can_manage"] is False and "notes" not in seen and "server_password" not in seen
        assert ann.patch(f"/api/games/{gid}", json={"name": "mine now"}).status_code == 403
        assert ann.get(f"/api/games/{gid}/log").status_code == 403

        # everything an administrator does with a game
        for path in (f"/api/games/{gid}/log", f"/api/games/{gid}/backups", f"/api/games/{gid}/multidata",
                     f"/api/games/{gid}/slots/{slot}/tracker"):
            assert org.get(path).status_code == 200, path
        assert org.post(f"/api/games/{gid}/start").status_code == 200
        assert wait_for(lambda: org.get(f"/api/games/{gid}").json()["state"] == "running", timeout=60)
        assert org.post(f"/api/games/{gid}/command", json={"command": "/help"}).status_code == 200
        assert org.post(f"/api/games/{gid}/stop").status_code == 200
        spoiler = next(f for f in org.get(f"/api/games/{gid}").json()["shared_files"] if f["kind"] == "spoiler")
        assert org.get(f"/api/games/{gid}/files/{spoiler['id']}").status_code == 200
        assert ann.get(f"/api/games/{gid}/files/{spoiler['id']}").status_code == 404

        # the administrators' game: seen when everyone may see it, but not theirs to run
        admin.patch(f"/api/games/{admins_game}", json={"visibility": "all", "notes": "admins only"})
        seen = org.get(f"/api/games/{admins_game}").json()
        assert seen["can_manage"] is False and "notes" not in seen
        for method, path in (("PATCH", ""), ("POST", "/start"), ("GET", "/log"), ("DELETE", "")):
            r = org.request(method, f"/api/games/{admins_game}{path}", json={"name": "x"} if method == "PATCH" else None)
            assert r.status_code == 403, (method, path, r.text)

        # and the server is not theirs
        for method, path in (("GET", "/api/users"), ("PATCH", "/api/settings"), ("GET", "/api/audit"),
                             ("POST", "/api/ap-versions"), ("PUT", "/api/host-yaml")):
            assert org.request(method, path, json={}).status_code == 403, path
        assert ann.get("/api/users/names").status_code == 403

        # what they did is in the administrators' log
        org_id = org.get("/api/auth/status").json()["user"]["id"]
        actions = {e["action"] for e in admin.get(f"/api/audit?user_id={org_id}").json()["entries"]}
        assert {"game.add", "game.settings", "slot.assign", "game.command"} <= actions

        # an organizer who is a player again runs nothing
        assert admin.patch(f"/api/users/{org_id}", json={"role": "user"}).status_code == 200
        assert org.get(f"/api/games/{gid}").status_code == 404
        assert org.post("/api/games", files={"file": ("seed.zip", zipped)}).status_code == 403
        assert admin.patch(f"/api/users/{org_id}", json={"role": "organizer"}).status_code == 200
        assert org.delete(f"/api/games/{gid}").status_code == 200
    finally:
        admin.delete(f"/api/games/{admins_game}")
        for g in admin.get("/api/games").json():
            if g["name"] in ("Org's game", "Renamed"):
                admin.delete(f"/api/games/{g['id']}")


def test_an_organizer_generates_and_runs_sign_up_rounds(ap_server, admin, make_user):
    _org_name, org = make_user("org", role="organizer")
    _other_name, other_org = make_user("org", role="organizer")
    ann_name, ann = make_user("ann")
    assert admin.patch("/api/settings", json={"users_can_generate": False}).status_code == 200

    # organizers generate from their own configs, also where players may not
    assert ann.post("/api/jobs", json={"config_ids": [own_config(ann, "AnnSolo")]}).status_code == 403
    r = org.post("/api/jobs", json={"config_ids": [own_config(org, "OrgSolo")], "host_after": False})
    assert r.status_code == 200, r.text
    job = wait_for(lambda: (j := org.get(f"/api/jobs/{r.json()['id']}").json())["status"] in ("succeeded", "failed") and j,
                   timeout=180)
    assert job["status"] == "succeeded", job
    assert org.get(f"/api/games/{job['game_id']}").json()["can_manage"] is True
    admin.delete(f"/api/games/{job['game_id']}")

    # rounds: players cannot open them, organizers run their own
    assert ann.post("/api/signups", json={"name": "Nope"}).status_code == 403
    r = org.post("/api/signups", json={"name": f"Org round {time.time_ns()}", "auto_generate": True})
    assert r.status_code == 200, r.text
    sid = r.json()["id"]
    assert r.json()["can_manage"] is True
    assert ann.post(f"/api/signups/{sid}/entries", json={"config_id": own_config(ann, "AnnEntry")}).status_code == 200
    assert org.post(f"/api/signups/{sid}/entries", json={"config_id": own_config(org, "OrgEntry", "APQuest")}
                    ).status_code == 200
    seen = org.get(f"/api/signups/{sid}").json()
    entry = next(e for e in seen["entries"] if e["username"] == ann_name)
    assert "name" in entry and "generation_error" in entry  # what the round's organizer needs to help
    assert "name" not in next(e for e in other_org.get(f"/api/signups/{sid}").json()["entries"] if e["username"] == ann_name)
    assert ann.get(f"/api/signups/{sid}").json()["can_manage"] is False
    for client in (ann, other_org):
        assert client.patch(f"/api/signups/{sid}", json={"name": "taken"}).status_code == 403
        assert client.post(f"/api/signups/{sid}/close").status_code == 403
    assert org.patch(f"/api/signups/{sid}", json={"description": "Two worlds"}).status_code == 200
    assert org.post(f"/api/signups/{sid}/test").status_code == 200

    # closing generates the game, which the organizer then runs
    assert org.post(f"/api/signups/{sid}/close").status_code == 200
    done = wait_for(lambda: (s := org.get(f"/api/signups/{sid}").json())["status"] in ("done", "closed") and s,
                    timeout=240)
    assert done["status"] == "done", done
    game = org.get(f"/api/games/{done['game_id']}").json()
    assert game["can_manage"] is True and {s["owner"] for s in game["slots"]} >= {ann_name}
    admin.delete(f"/api/games/{done['game_id']}")
    assert org.delete(f"/api/signups/{sid}").status_code == 200


def test_roles_are_checked(admin, make_user):
    name, client = make_user("someone")
    uid = client.get("/api/auth/status").json()["user"]["id"]
    assert admin.patch(f"/api/users/{uid}", json={"role": "owner"}).status_code == 400
    assert admin.post("/api/users", json={"username": f"x{time.time_ns() % 10**6}", "password": "password123",
                                          "role": "superuser"}).status_code == 400
    assert admin.patch(f"/api/users/{uid}", json={"role": "organizer"}).json()["role"] == "organizer"
    # the last administrator cannot become an organizer
    admin_id = admin.get("/api/auth/status").json()["user"]["id"]
    others = [u for u in admin.get("/api/users").json() if u["role"] == "admin" and not u["disabled"] and u["id"] != admin_id]
    if not others:
        assert admin.patch(f"/api/users/{admin_id}", json={"role": "organizer"}).status_code == 409
