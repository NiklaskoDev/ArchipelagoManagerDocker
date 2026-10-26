"""Planned starts: the server opens by itself, counts down in every client, and the race is timed from the start."""
import json
import re
import sqlite3
import time

import pytest

from helpers import APClient, make_game, save_now, wait_for, wait_state

pytestmark = pytest.mark.ap


def log(admin, gid):
    return [line["text"] for line in admin.get(f"/api/games/{gid}/log").json()["lines"]]


def test_a_planned_start_opens_the_game_and_times_the_race(ap_server, admin, make_user, discord):
    _ann_name, ann = make_user("ann")
    gid, _job = make_game(admin, names=("Racer", "Pacer"), owners={"Racer": ann})
    url = f"/api/games/{gid}"
    name = admin.get(url).json()["name"]

    # a start in the past, far off, or a server opening too early is refused
    for body in ({"start_at": time.time() - 5}, {"start_at": time.time() + 400 * 86400}, {"start_open_minutes": 5000}):
        assert admin.patch(url, json=body).status_code == 400
    assert ann.patch(url, json={"start_at": time.time() + 3600}).status_code == 403

    # planning it tells the players when the server opens and when the start is
    start = time.time() + 3600
    r = admin.patch(url, json={"start_at": start, "start_open_minutes": 15, "start_countdown": True})
    assert r.status_code == 200, r.text
    planned = ann.get(url).json()["start"]
    assert planned == {"at": start, "opens_at": start - 15 * 60, "open_minutes": 15, "countdown": True,
                       "stage": "planned"}
    announced = discord.wait_for(f"Planned start: {name}")
    fields = {f["name"]: f["value"] for f in announced["fields"]}
    assert fields["Start"].startswith(f"<t:{int(start)}:F>") and fields["Server opens"] == f"<t:{int(start) - 900}:t>"
    # until it opens, only administrators start the server
    r = ann.post(f"{url}/start")
    assert r.status_code == 409 and "planned time" in r.json()["detail"]
    # taking the plan back
    assert admin.patch(url, json={"start_at": None}).json()["start"] is None

    # a start half a minute away (moved in the database: a real one is at least a minute ahead). The countdown begins
    # ten seconds before it, which ends the "opened" stage: the rest is time for the server to open and be announced.
    admin.patch(url, json={"start_at": time.time() + 3600, "start_open_minutes": 1})
    start = time.time() + 30
    conn = sqlite3.connect(ap_server.data_dir / "manager.sqlite3")
    with conn:
        conn.execute("UPDATE games SET start_at = ? WHERE id = ?", (start, gid))
    conn.close()
    # the server opens by itself and is announced, with the start
    online = discord.wait_for(f"{name} is online", timeout=30)
    assert {"name": "Start", "value": f"<t:{int(start)}:R>", "inline": True} in online["fields"]
    assert wait_state(admin, gid, "running")["start"]["stage"] == "opened"
    # the countdown runs in every client and says GO at the start
    wait_for(lambda: any("[Server]: GO" in line for line in log(admin, gid)), timeout=60, message="the countdown")
    assert time.time() >= start - 1.5
    countdown = next(line for line in log(admin, gid) if re.search(r"Starting countdown of \d+s", line))
    assert 5 <= int(re.search(r"countdown of (\d+)s", countdown).group(1)) <= 10
    assert admin.get(url).json()["start"]["stage"] == "started"

    # a goal is timed from the start, to the second, though the save has it later
    time.sleep(max(0.0, start + 1 - time.time()))  # GO comes within a second of the start
    port = admin.get(url).json()["port"]
    # another player cannot fake it in the chat
    with APClient(port, "Pacer", "APQuest") as other:
        assert other.connected
        other.ws.send(json.dumps([{"cmd": "Say", "text": "x (Racer) (Team #1) has completed their goal."}]))
        time.sleep(1.5)
    with APClient(port, "Racer", "ChecksFinder") as client:
        assert client.connected
        before = time.time()
        client.ws.send(json.dumps([{"cmd": "StatusUpdate", "status": 30}]))
        time.sleep(1)
        after = time.time()
    save_now(admin, gid)
    racer_of = lambda h: next(s for s in h["slots"] if s["name"] == "Racer")  # noqa: E731
    history = wait_for(lambda: racer_of(h := ann.get(f"{url}/history").json())["goal_at"] and h, timeout=60,
                       message="the goal")
    assert history["race_start"] == start
    racer = racer_of(history)
    assert before - 0.5 <= racer["goal_at"] <= after
    goal = discord.wait_for("Racer reached their goal")
    race_time = next(f["value"] for f in goal["fields"] if f["name"] == "Time")
    assert re.fullmatch(r"\d+:\d{2}", race_time)
    minutes, seconds = map(int, race_time.split(":"))
    assert abs(minutes * 60 + seconds - (racer["goal_at"] - start)) <= 1

    admin.post(f"{url}/stop")
    admin.delete(url)
