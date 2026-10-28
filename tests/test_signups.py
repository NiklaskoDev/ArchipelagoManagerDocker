"""Sign-up rounds: players enter configs until a deadline, then one game is generated from all entries."""
import sqlite3
import time

import pytest

from helpers import player_yaml, upload_yaml, wait_for

pytestmark = pytest.mark.ap


def new_round(admin, name=None, **extra):
    r = admin.post("/api/signups", json={"name": name or f"Round {time.time_ns()}", **extra})
    assert r.status_code == 200, r.text
    return r.json()


def config(client, player, game="ChecksFinder", content=None):
    r = upload_yaml(client, f"{player}-{time.time_ns()}", content or player_yaml(player, game))
    assert r.status_code == 200, r.text
    return r.json()["created"][0]["id"]


def enter(client, signup_id, config_id):
    return client.post(f"/api/signups/{signup_id}/entries", json={"config_id": config_id})


def wait_round(client, signup_id, statuses=("done", "closed"), timeout=180):
    return wait_for(lambda: (s := client.get(f"/api/signups/{signup_id}").json())["status"] in statuses and s,
                    timeout=timeout, message=f"round {signup_id} to reach {statuses}")


def test_a_round_from_sign_up_to_game(admin, make_user, discord):
    ann_name, ann = make_user("ann")
    bo_name, bo = make_user("bo")
    signup = new_round(admin, "Weekend async", description="Bring your **best**", deadline=time.time() + 3 * 86400,
                       allowed_games=["ChecksFinder", "APQuest"], options={"spoiler": 2})
    sid = signup["id"]
    assert signup["status"] == "open" and signup["open"] and signup["allowed_games"] == ["APQuest", "ChecksFinder"]
    assert signup["options"]["spoiler"] == 2 and signup["options"]["host_after"] is True
    opened = discord.wait_for("Sign-ups open: Weekend async")
    assert opened["url"] == f"http://manager.test:8080/#/signups/{sid}"
    assert f"<t:{int(signup['deadline'])}:R>" in opened["description"]  # shown in every reader's own time zone
    assert {"name": "Games", "value": "APQuest, ChecksFinder", "inline": False} in opened["fields"]

    # the deadline as a calendar file, in the language of the reader's web UI, with a reminder a day before
    r = bo.get(f"/api/signups/{sid}/deadline.ics?lang=de")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/calendar")
    assert r.headers["content-disposition"] == 'inline; filename="Weekend-async.ics"'
    deadline = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(signup["deadline"]))
    for line in (f"UID:signup-{sid}-deadline@manager.test", f"DTSTART:{deadline}", "SUMMARY:Anmeldung für Weekend async schließt",
                 "DESCRIPTION:Bring your **best**", f"URL:http://manager.test:8080/#/signups/{sid}", "TRIGGER:-PT1440M"):
        assert f"\r\n{line}\r\n" in r.text, (line, r.text)
    assert "SUMMARY:Sign-ups for Weekend async close\r\n" in bo.get(f"/api/signups/{sid}/deadline.ics?lang=xx").text

    # everyone sees the round; players enter their own configs
    assert [s["id"] for s in bo.get("/api/signups").json() if s["id"] == sid] == [sid]
    assert enter(ann, sid, config(ann, "Annie")).status_code == 200
    r = enter(bo, sid, config(bo, "Bobo", "APQuest"))
    assert r.status_code == 200 and r.json()["entry_count"] == 2 and r.json()["my_entry_count"] == 1
    view = bo.get(f"/api/signups/{sid}").json()
    others = [e for e in view["entries"] if not e["mine"]]
    assert [(e["username"], e["player_names"], e["games"]) for e in others] == [(ann_name, ["Annie"], ["ChecksFinder"])]
    assert "name" not in others[0] and "config_id" not in others[0]  # others see who plays what, not the file

    # closing generates the game; every entrant owns their slot
    assert bo.post(f"/api/signups/{sid}/close").status_code == 403
    r = admin.post(f"/api/signups/{sid}/close")
    assert r.status_code == 200 and r.json()["status"] in ("generating", "done")
    closed = discord.wait_for("Sign-ups closed: Weekend async")
    assert "Generating the game now" in closed["description"]
    done = wait_round(admin, sid)
    assert done["status"] == "done" and done["error"] is None
    game = ann.get(f"/api/games/{done['game_id']}").json()
    assert game["name"] == "Weekend async"
    assert sorted((s["name"], s["owner"]) for s in game["slots"]) == sorted([("Annie", ann_name), ("Bobo", bo_name)])
    ready = discord.wait_for("New game ready: Weekend async")
    assert "From the sign-up round **Weekend async**" in ready["description"]
    assert any(f["name"] == "Server" for f in ready["fields"])  # host_after: the server was started
    assert enter(ann, sid, config(ann, "Late")).status_code == 409
    admin.post(f"/api/games/{done['game_id']}/stop")
    admin.delete(f"/api/games/{done['game_id']}")


def test_the_rules_of_a_round(admin, make_user):
    _a, ann = make_user("ann")
    _b, bo = make_user("bo")
    sid = new_round(admin, allowed_games=["ChecksFinder"], slots_per_user=1)["id"]
    assert ann.get(f"/api/signups/{sid}/deadline.ics").status_code == 404  # closed by hand: no date for a calendar
    first = config(ann, "Taken")
    assert enter(ann, sid, first).status_code == 200
    assert enter(ann, sid, first).status_code == 409  # the same config twice
    r = enter(ann, sid, config(ann, "Second"))
    assert r.status_code == 409 and "1 slot in this round. Withdraw an entry first." in r.json()["detail"]
    r = enter(bo, sid, config(bo, "taken"))  # names clash regardless of case
    assert r.status_code == 409 and "already taken" in r.json()["detail"]
    r = enter(bo, sid, config(bo, "Quester", "APQuest"))
    assert r.status_code == 400 and "only allows ChecksFinder" in r.json()["detail"]
    r = enter(bo, sid, config(bo, "Broken", content="name: Broken\ngame: ChecksFinder\n"))
    assert r.status_code == 400 and "cannot be generated" in r.json()["detail"]
    assert enter(bo, sid, first).status_code == 404  # someone else's config
    assert enter(bo, 999999, config(bo, "Nowhere")).status_code == 404

    # only admins run rounds
    assert bo.post("/api/signups", json={"name": "Mine"}).status_code == 403
    assert bo.patch(f"/api/signups/{sid}", json={"name": "Mine"}).status_code == 403
    assert bo.post(f"/api/signups/{sid}/generate").status_code == 403
    assert bo.delete(f"/api/signups/{sid}").status_code == 403
    for body, message in (({"name": " "}, "name"), ({"name": "x", "deadline": time.time() - 60}, "past"),
                          ({"name": "x", "slots_per_user": 0}, "between"),
                          ({"name": "x", "options": {"spoiler": 7}}, "Spoiler"),
                          ({"name": "x", "options": {"plando": ["everything"]}}, "plando"),
                          ({"name": "x", "options": {"turbo": True}}, "Unknown option")):
        r = admin.post("/api/signups", json=body)
        assert r.status_code == 400 and message in r.json()["detail"], (body, r.text)

    # an admin can generate only once sign-ups are closed
    r = admin.post(f"/api/signups/{sid}/generate")
    assert r.status_code == 409 and "Close the sign-ups" in r.json()["detail"]
    admin.delete(f"/api/signups/{sid}")


def test_a_file_with_several_players_takes_a_slot_for_each(admin, make_user):
    """A round limits slots, not files: two players in one file do not get past a round of one per player."""
    _a, ann = make_user("ann")
    pair = player_yaml("AnnOne") + "---\n" + player_yaml("AnnTwo")
    single = new_round(admin, slots_per_user=1)
    assert single["slots_per_user"] == 1 and single["configs_per_user"] == 1  # its old name, for scripts
    r = enter(ann, single["id"], config(ann, "Pair", content=pair))
    assert r.status_code == 400 and "1 slot in this round, and 'Pair-" in r.json()["detail"]
    assert "has 2: one for each player in the file" in r.json()["detail"]
    assert ann.get(f"/api/signups/{single['id']}").json()["entries"] == []

    # in a round of three, the pair takes two: room for one more player, not for another pair
    sid = new_round(admin, slots_per_user=3)["id"]
    r = enter(ann, sid, config(ann, "Pair", content=pair))
    assert r.status_code == 200 and r.json()["my_slot_count"] == 2 and r.json()["my_entry_count"] == 1
    assert r.json()["entries"][0]["slots"] == 2 and r.json()["entries"][0]["player_names"] == ["AnnOne", "AnnTwo"]
    r = enter(ann, sid, config(ann, "Pair two", content=player_yaml("AnnThree") + "---\n" + player_yaml("AnnFour")))
    assert r.status_code == 409 and "has 2: there is room for 1 more" in r.json()["detail"]
    alone = config(ann, "AnnFive")
    r = enter(ann, sid, alone)
    assert r.status_code == 200 and r.json()["my_slot_count"] == 3
    r = enter(ann, sid, config(ann, "AnnSix"))
    assert r.status_code == 409 and "3 slots in this round. Withdraw an entry first." in r.json()["detail"]

    # an updated entry counts as its new version
    entry = next(e for e in ann.get(f"/api/signups/{sid}").json()["entries"] if e["player_names"] == ["AnnFive"])
    content = player_yaml("AnnFive") + "---\n" + player_yaml("AnnSeven")
    assert ann.put(f"/api/configs/{alone}", json={"content": content}).status_code == 200
    r = ann.post(f"/api/signups/{sid}/entries/{entry['id']}/refresh")
    assert r.status_code == 409 and "room for 1 more" in r.json()["detail"]
    # scripts can still set the limit by its old name
    r = admin.patch(f"/api/signups/{sid}", json={"configs_per_user": 4})
    assert r.status_code == 200 and r.json()["slots_per_user"] == 4
    r = ann.post(f"/api/signups/{sid}/entries/{entry['id']}/refresh")
    assert r.status_code == 200 and r.json()["my_slot_count"] == 4
    admin.delete(f"/api/signups/{sid}")
    admin.delete(f"/api/signups/{single['id']}")


def test_entries_are_copies_that_players_can_update_or_withdraw(admin, make_user, ap_server):
    _a, ann = make_user("ann")
    sid = new_round(admin, auto_generate=False)["id"]
    cid = config(ann, "Original")
    entry = enter(ann, sid, cid).json()["entries"][0]
    assert entry["outdated"] is False and entry["player_names"] == ["Original"]

    # editing the library copy does not change the entry until the player says so
    assert ann.put(f"/api/configs/{cid}", json={"content": player_yaml("Renamed")}).status_code == 200
    entry = ann.get(f"/api/signups/{sid}").json()["entries"][0]
    assert entry["outdated"] is True and entry["player_names"] == ["Original"]
    assert b"Original" in ann.get(f"/api/signups/{sid}/entries/{entry['id']}/download").content
    r = ann.post(f"/api/signups/{sid}/entries/{entry['id']}/refresh")
    entry = r.json()["entries"][0]
    assert r.status_code == 200 and entry["outdated"] is False and entry["player_names"] == ["Renamed"]
    assert b"Renamed" in ann.get(f"/api/signups/{sid}/entries/{entry['id']}/download").content

    # deleting the library config keeps the entry: it is a copy
    ann.delete(f"/api/configs/{cid}")
    entry = ann.get(f"/api/signups/{sid}").json()["entries"][0]
    assert entry["config_id"] is None and entry["outdated"] is False
    assert ann.post(f"/api/signups/{sid}/entries/{entry['id']}/refresh").status_code == 410

    # others cannot touch it; the owner can withdraw while the round is open
    _b, bo = make_user("bo")
    assert bo.delete(f"/api/signups/{sid}/entries/{entry['id']}").status_code == 404
    assert bo.get(f"/api/signups/{sid}/entries/{entry['id']}/download").status_code == 404
    assert ann.delete(f"/api/signups/{sid}/entries/{entry['id']}").status_code == 200
    assert ann.get(f"/api/signups/{sid}").json()["entries"] == []

    # after closing, players cannot withdraw any more but an admin can still tidy up
    e2 = enter(ann, sid, config(ann, "Stays"))
    assert e2.status_code == 200
    admin.post(f"/api/signups/{sid}/close")
    entry_id = e2.json()["entries"][0]["id"]
    assert ann.delete(f"/api/signups/{sid}/entries/{entry_id}").status_code == 409
    assert admin.get(f"/api/signups/{sid}").json()["status"] == "closed"  # auto_generate is off
    assert admin.delete(f"/api/signups/{sid}/entries/{entry_id}").status_code == 200
    r = admin.post(f"/api/signups/{sid}/generate")
    assert r.status_code == 400 and "Nobody signed up" in r.json()["detail"]
    assert "Nobody signed up" in admin.get(f"/api/signups/{sid}").json()["error"]

    # reopening takes entries again; deleting the round removes its copies
    assert admin.post(f"/api/signups/{sid}/reopen", json={"deadline": time.time() - 5}).status_code == 400
    assert admin.post(f"/api/signups/{sid}/reopen", json={"deadline": None}).json()["open"] is True
    assert enter(ann, sid, config(ann, "Back")).status_code == 200
    folder = ap_server.data_dir / "signups" / str(sid)
    assert len(list(folder.glob("*.yaml"))) == 1
    assert admin.delete(f"/api/signups/{sid}").status_code == 200
    assert not folder.exists() and admin.get(f"/api/signups/{sid}").status_code == 404


def test_a_failed_generation_can_be_fixed_and_retried(admin, make_user, discord):
    _a, ann = make_user("ann")
    sid = new_round(admin, "Needs a world")["id"]
    r = enter(ann, sid, config(ann, "Wanderer", "Some Uninstalled Game",
                               content="name: Wanderer\ngame: Some Uninstalled Game\nSome Uninstalled Game: {}\n"))
    assert r.status_code == 200 and "Not installed on this server yet" in r.json()["warnings"][0]
    assert r.json()["entries"][0]["missing_games"] == ["Some Uninstalled Game"]
    signup = admin.post(f"/api/signups/{sid}/close").json()  # tries to generate right away and cannot
    assert signup["status"] == "closed" and "not installed" in signup["error"]
    discord.wait_for("Sign-ups closed: Needs a world")
    time.sleep(1)
    assert "Could not generate Needs a world" not in discord.titles()  # the admin sees it on the page
    r = admin.post(f"/api/signups/{sid}/generate")
    assert r.status_code == 409 and r.json()["missing_games"] == ["Some Uninstalled Game"]
    # the admin removes the entry that cannot work, adds a good one and tries again
    admin.delete(f"/api/signups/{sid}/entries/{signup['entries'][0]['id']}")
    assert enter(admin, sid, config(ann, "Fixed")).status_code == 200  # an admin can enter a player's config
    assert admin.post(f"/api/signups/{sid}/generate").status_code == 200
    done = wait_round(admin, sid)
    assert done["status"] == "done" and done["error"] is None
    assert done["entries"][0]["username"] == ann.get("/api/auth/status").json()["user"]["username"]
    admin.post(f"/api/games/{done['game_id']}/stop")
    admin.delete(f"/api/games/{done['game_id']}")


def test_a_round_can_be_tested_before_it_is_generated(admin, make_user, discord):
    _a, ann = make_user("ann")
    _b, bo = make_user("bo")
    sid = new_round(admin, "Dry run", options={"spoiler": 0})["id"]
    r = admin.post(f"/api/signups/{sid}/test")
    assert r.status_code == 400 and r.json()["detail"] == "Nobody signed up for this round"
    assert enter(ann, sid, config(ann, "Tessa")).status_code == 200
    # accepted when entered: only the generator knows every option of every game
    banana = "name: Brock\ngame: APQuest\nAPQuest:\n  player_sprite: banana\n"
    r = enter(bo, sid, config(bo, "Brock", "APQuest", content=banana))
    assert r.status_code == 200 and r.json()["test"] is None
    bad_entry = next(e["id"] for e in r.json()["entries"] if e["mine"])

    assert bo.post(f"/api/signups/{sid}/test").status_code == 403
    r = admin.post(f"/api/signups/{sid}/test")
    assert r.status_code == 200 and r.json()["test"]["status"] in ("queued", "running")
    assert admin.post(f"/api/signups/{sid}/test").status_code == 409  # one test at a time
    failed = wait_for(lambda: (s := admin.get(f"/api/signups/{sid}").json())["test"]["status"] == "failed" and s,
                      timeout=180, message="the test to fail")
    assert failed["status"] == "open" and failed["open"] and failed["error"] is None  # a test leaves the round alone
    assert failed["test"]["current"] is True
    errors = {e["player_names"][0]: e["generation_error"] for e in failed["entries"]}
    assert "banana" in errors["Brock"] and errors["Tessa"] is None
    # bo sees what is wrong with his entry; ann sees neither his error nor the test
    mine = next(e for e in bo.get(f"/api/signups/{sid}").json()["entries"] if e["mine"])
    assert "banana" in mine["generation_error"]
    view = ann.get(f"/api/signups/{sid}").json()
    assert view["test"] is None
    assert "generation_error" not in next(e for e in view["entries"] if not e["mine"])
    # the test is a job like others, only it never becomes a game and Discord is not told
    job = admin.get(f"/api/jobs/{failed['test']['job_id']}").json()
    assert job["test"] is True and job["signup"] == {"id": sid, "name": "Dry run"}
    assert "Generation failed: Dry run" not in discord.titles()

    # bo swaps in a working config: nothing is marked any more, and the test no longer matches the entries
    bo.delete(f"/api/signups/{sid}/entries/{bad_entry}")
    assert enter(bo, sid, config(bo, "Brock", "APQuest")).status_code == 200
    view = admin.get(f"/api/signups/{sid}").json()
    assert view["test"]["current"] is False
    assert [e["generation_error"] for e in view["entries"]] == [None, None]
    assert admin.post(f"/api/signups/{sid}/test").status_code == 200
    passed = wait_for(lambda: (s := admin.get(f"/api/signups/{sid}").json())["test"]["status"] == "succeeded" and s,
                      timeout=180, message="the test to pass")
    assert passed["status"] == "open" and passed["test"]["current"] is True and passed["game_id"] is None
    job = admin.get(f"/api/jobs/{passed['test']['job_id']}").json()
    assert job["game_id"] is None and "Dry run" not in [g["name"] for g in admin.get("/api/games").json()]
    log = [l["text"] for l in admin.get(f"/api/jobs/{job['id']}/log").json()["lines"]]
    assert any(l.startswith("Test passed") for l in log)
    # other options than the tested ones make the test out of date too
    r = admin.patch(f"/api/signups/{sid}", json={"options": {"spoiler": 1}})
    assert r.json()["test"]["current"] is False
    admin.delete(f"/api/signups/{sid}")


def test_deadlines_close_rounds_and_remind_players(admin, make_user, discord, ap_server):
    """The clock runs every 20 seconds; the test moves the times in the database instead of waiting days."""
    _a, ann = make_user("ann")
    due = new_round(admin, "Due round", deadline=time.time() + 3600, options={"host_after": False})
    soon = new_round(admin, "Soon round", deadline=time.time() + 3600)
    doomed = new_round(admin, "Doomed round", deadline=time.time() + 3600)
    assert enter(ann, due["id"], config(ann, "Punctual")).status_code == 200
    assert enter(ann, doomed["id"], config(ann, "Lost", content="name: Lost\ngame: Not Here\nNot Here: {}\n")).status_code == 200
    conn = sqlite3.connect(ap_server.data_dir / "manager.sqlite3")
    with conn:
        conn.execute("UPDATE signups SET deadline = ? WHERE id IN (?, ?)", (time.time() - 1, due["id"], doomed["id"]))
        # open for days and closing within a day: time for a reminder
        conn.execute("UPDATE signups SET created_at = ?, deadline = ? WHERE id = ?",
                     (time.time() - 3 * 86400, time.time() + 6 * 3600, soon["id"]))
    conn.close()
    closed = discord.wait_for("Sign-ups closed: Due round", timeout=60)
    assert "Punctual" in closed["fields"][0]["value"]
    reminder = discord.wait_for("Sign-ups for Soon round close soon", timeout=60)
    # nobody watches the page at the deadline, so a failure to generate is announced
    assert "Not Here" in discord.wait_for("Could not generate Doomed round", timeout=60)["description"]
    assert "0 entries so far" in reminder["description"]
    done = wait_round(admin, due["id"])
    assert done["status"] == "done"
    assert admin.get(f"/api/games/{done['game_id']}").json()["state"] == "stopped"  # host_after was off
    time.sleep(21)  # one more turn of the clock: the reminder is sent once
    assert discord.titles().count("Sign-ups for Soon round close soon") == 1
    assert admin.get(f"/api/signups/{soon['id']}").json()["status"] == "open"
    admin.delete(f"/api/games/{done['game_id']}")
    for signup in (soon, doomed):
        admin.delete(f"/api/signups/{signup['id']}")
