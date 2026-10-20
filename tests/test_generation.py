"""Generating multiworlds with the real Generate.py."""
import pytest

from helpers import player_yaml, run_job, upload_yaml, wait_for, wait_state

pytestmark = pytest.mark.ap


def cfg(client, name, game="ChecksFinder", content=None):
    r = upload_yaml(client, f"{name}-cfg", content or player_yaml(name, game))
    assert r.status_code == 200, r.text
    return r.json()["created"][0]["id"]


def test_generation_links_slots_to_the_owners_of_the_configs(admin, make_user):
    alice_name, alice = make_user("alice")
    bob_name, bob = make_user("bob")
    a, b = cfg(alice, "Alyce"), cfg(bob, "Bobby", "APQuest")
    job = run_job(admin, [a, b], name="Friday run", spoiler=2)
    assert job["status"] == "succeeded", job
    assert job["creator"] == "admin" and job["name"] == "Friday run"
    assert [p["owner"] for p in job["players"]] == [alice_name, bob_name]

    game = admin.get(f"/api/games/{job['game_id']}").json()
    assert game["name"] == "Friday run" and game["source"] == "generated"
    assert {s["name"]: s["owner"] for s in game["slots"]} == {"Alyce": alice_name, "Bobby": bob_name}
    assert game["games"] == ["APQuest", "ChecksFinder"]
    assert game["port"] is not None and game["state"] == "stopped"  # host_after was False

    # the people in the game see it; nobody else does
    assert [g["name"] for g in alice.get("/api/games").json()] == ["Friday run"]
    _o, outsider = make_user()
    assert outsider.get("/api/games").json() == []
    assert outsider.get(f"/api/games/{job['game_id']}").status_code == 404

    # the spoiler and the yaml copies stay with the admin
    kinds = {f["kind"] for f in game["shared_files"]}
    assert kinds == {"spoiler", "yaml"}
    assert alice.get(f"/api/games/{job['game_id']}").json()["shared_files"] == []
    log = admin.get(f"/api/jobs/{job['id']}/log").json()
    assert any("Imported as game" in line["text"] for line in log["lines"])


def test_generation_can_start_the_server_when_done(admin):
    cid = cfg(admin, "Solo")
    r = admin.post("/api/jobs", json={"config_ids": [cid], "host_after": True})
    job = wait_for(lambda: (j := admin.get(f"/api/jobs/{r.json()['id']}").json())["status"] == "succeeded" and j, timeout=120)
    game = wait_state(admin, job["game_id"], "running")
    assert game["address"].startswith("play.example.test:")
    admin.post(f"/api/games/{job['game_id']}/stop")


def test_seed_makes_generation_reproducible(admin):
    cid = cfg(admin, "Repeat")
    seeds = [admin.get(f"/api/games/{run_job(admin, [cid], seed=424242)['game_id']}").json()["seed_name"] for _ in range(2)]
    assert seeds[0] == seeds[1]
    other = admin.get(f"/api/games/{run_job(admin, [cid], seed=424243)['game_id']}").json()["seed_name"]
    assert other != seeds[0]


def test_spoiler_levels_and_race_mode(admin):
    cid = cfg(admin, "Spoil")
    none = admin.get(f"/api/games/{run_job(admin, [cid], spoiler=0)['game_id']}").json()
    assert not [f for f in none["shared_files"] if f["kind"] == "spoiler"]
    full = admin.get(f"/api/games/{run_job(admin, [cid], spoiler=3)['game_id']}").json()
    assert [f for f in full["shared_files"] if f["kind"] == "spoiler"]
    race = admin.get(f"/api/games/{run_job(admin, [cid], race=True)['game_id']}").json()
    assert race["race_mode"] is True


def test_meta_yaml_is_validated_and_applied(admin):
    cid = cfg(admin, "Meta")
    r = admin.post("/api/jobs", json={"config_ids": [cid], "meta": "progression_balancing: 50\n"})
    assert r.status_code == 400 and "meta_description" in r.json()["detail"]
    r = admin.post("/api/jobs", json={"config_ids": [cid], "meta": "meta_description: [broken"})
    assert r.status_code == 400
    job = run_job(admin, [cid], meta="meta_description: Test meta\nnull:\n  progression_balancing: 25\n")
    assert job["status"] == "succeeded", job["error"]
    assert job["options"]["has_meta"] is True


@pytest.mark.parametrize("payload, message", [
    ({"spoiler": 7}, "Spoiler"), ({"seed": -5}, "Seed"), ({"plando": ["everything"]}, "plando"),
    ({"config_ids": []}, "at least one"),
])
def test_job_options_are_validated(admin, payload, message):
    cid = cfg(admin, "Valid")
    body = {"config_ids": [cid], **payload}
    r = admin.post("/api/jobs", json=body)
    assert r.status_code == 400 and message.lower() in r.json()["detail"].lower(), r.text


def test_duplicate_player_names_and_repeated_configs_are_refused(admin, make_user):
    _a, alice = make_user()
    _b, bob = make_user()
    one = cfg(alice, "Twin")
    two = cfg(bob, "twin")  # names are compared case-insensitively, like the generator does
    r = admin.post("/api/jobs", json={"config_ids": [one, two]})
    assert r.status_code == 400 and "unique" in r.json()["detail"]
    assert admin.post("/api/jobs", json={"config_ids": [one, one]}).status_code == 400
    arch = cfg(admin, "Archipelago")
    assert admin.post("/api/jobs", json={"config_ids": [arch]}).status_code == 400


def test_generation_failure_is_reported_readably(admin):
    bad = cfg(admin, "Broken", content="name: Broken\ngame: APQuest\nAPQuest:\n  player_sprite: banana\n")
    job = run_job(admin, [bad])
    assert job["status"] == "failed"
    assert "banana" in job["error"], job["error"]  # says what is wrong, not just that something is
    assert "Press enter" not in job["error"] and "EOFError" not in job["error"]
    assert job["game_id"] is None
    log = [l["text"] for l in admin.get(f"/api/jobs/{job['id']}/log").json()["lines"]]
    assert any("Traceback" in l or "Exception" in l for l in log)


def test_users_need_permission_and_can_only_use_their_own_configs(admin, make_user):
    _a, alice = make_user()
    _b, bob = make_user()
    mine, theirs = cfg(alice, "Mine"), cfg(bob, "Theirs")
    assert alice.post("/api/jobs", json={"config_ids": [mine]}).status_code == 403
    admin.patch("/api/settings", json={"users_can_generate": True})
    try:
        assert alice.post("/api/jobs", json={"config_ids": [mine, theirs]}).status_code == 404
        r = alice.post("/api/jobs", json={"config_ids": [mine], "host_after": False})
        assert r.status_code == 200
        job = wait_for(lambda: (j := alice.get(f"/api/jobs/{r.json()['id']}").json())["status"] == "succeeded" and j, timeout=120)
        assert alice.get(f"/api/games/{job['game_id']}").status_code == 200
        # jobs are private to their creator (and admins)
        assert bob.get(f"/api/jobs/{job['id']}").status_code == 404
        assert bob.get(f"/api/jobs/{job['id']}/log").status_code == 404
        assert bob.get("/api/jobs").json() == []
        assert admin.get(f"/api/jobs/{job['id']}").status_code == 200
    finally:
        admin.patch("/api/settings", json={"users_can_generate": False})


def test_jobs_run_one_after_another(admin):
    ids = [cfg(admin, f"Queue{i}") for i in range(3)]
    jobs = [admin.post("/api/jobs", json={"config_ids": [i], "host_after": False}).json() for i in ids]
    final = [wait_for(lambda j=j: (x := admin.get(f"/api/jobs/{j['id']}").json())["status"] in ("succeeded", "failed") and x, timeout=240)
             for j in jobs]
    assert [j["status"] for j in final] == ["succeeded"] * 3
    started = [j["started_at"] for j in final]
    assert started == sorted(started) and all(f["finished_at"] <= final[i + 1]["started_at"] for i, f in enumerate(final[:-1]))


def test_finished_jobs_cannot_be_cancelled_and_can_be_removed(admin):
    cid = cfg(admin, "Done")
    job = run_job(admin, [cid])
    assert admin.post(f"/api/jobs/{job['id']}/cancel").status_code == 409
    assert admin.delete(f"/api/jobs/{job['id']}").status_code == 200
    assert admin.get(f"/api/jobs/{job['id']}").status_code == 404
    # the generated game stays
    assert admin.get(f"/api/games/{job['game_id']}").status_code == 200


def test_a_queued_job_can_be_cancelled(admin):
    cid = cfg(admin, "Slow")
    first = admin.post("/api/jobs", json={"config_ids": [cid], "host_after": False}).json()
    second = admin.post("/api/jobs", json={"config_ids": [cid], "host_after": False}).json()
    r = admin.post(f"/api/jobs/{second['id']}/cancel")
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    wait_for(lambda: admin.get(f"/api/jobs/{first['id']}").json()["status"] == "succeeded", timeout=120)
    assert admin.get(f"/api/jobs/{second['id']}").json()["game_id"] is None


def test_a_running_job_can_be_cancelled_and_leaves_nothing_behind(admin, tmp_path):
    import subprocess
    from helpers import build_apworld
    slow_game = "Slow Checks"
    path = build_apworld(tmp_path / "slowchecks.apworld", game=slow_game, module="slowchecks", slow_seconds=120)
    r = admin.post("/api/apworlds", files={"file": ("slowchecks.apworld", path.read_bytes())})
    assert r.status_code == 200 and r.json()["load_error"] is None, r.text
    def generator_processes():
        # the real generator is started with --player_files_path; this avoids matching a shell that merely mentions it
        out = subprocess.run(["pgrep", "-af", r"Generate\.py .*--player_files_path"], capture_output=True, text=True).stdout
        return [line for line in out.splitlines() if "pgrep" not in line]

    try:
        cid = cfg(admin, "Sleepy", slow_game)
        job = admin.post("/api/jobs", json={"config_ids": [cid], "host_after": False}).json()
        wait_for(lambda: admin.get(f"/api/jobs/{job['id']}").json()["status"] == "running", timeout=60, message="job to start")
        wait_for(generator_processes, timeout=30, message="generator process")
        r = admin.post(f"/api/jobs/{job['id']}/cancel")
        assert r.status_code == 200
        final = wait_for(lambda: (j := admin.get(f"/api/jobs/{job['id']}").json())["status"] != "running" and j, timeout=30)
        assert final["status"] == "cancelled" and final["game_id"] is None
        try:
            wait_for(lambda: not generator_processes(), timeout=20, message="generator process to be gone")
        except AssertionError:
            raise AssertionError(f"generator still running after cancel: {generator_processes()}") from None
        # the queue keeps working afterwards
        assert run_job(admin, [cfg(admin, "After")])["status"] == "succeeded"
    finally:
        for item in admin.get("/api/apworlds").json()["items"]:
            admin.delete(f"/api/apworlds/{item['id']}")
