"""Reminders for players who stopped playing while the others play on. A day lasts a second here
(REMINDER_DAY_SECONDS)."""
import sqlite3
import time

import pytest

from helpers import FakeWebhook, make_game, save_now, wait_for, wait_state

pytestmark = pytest.mark.ap

DAY = 1


@pytest.fixture(scope="module")
def reminding(tmp_path_factory):
    from conftest import Server
    server = Server(tmp_path_factory.mktemp("reminding") / "data", ap=True, extra_env={"REMINDER_DAY_SECONDS": str(DAY)})
    server.start()
    assert server.client().post("/api/auth/setup", json={"username": "admin", "password": "adminpass1",
                                                         "code": server.setup_code()}).status_code == 200
    hook = FakeWebhook()
    server.hook = hook
    yield server
    hook.close()
    server.stop()


def user(server, admin, name, discord_id=None):
    assert admin.post("/api/users", json={"username": name, "password": "password123", "role": "user"}
                      ).status_code == 200
    if discord_id:  # as if they linked Discord and asked to be reminded
        conn = sqlite3.connect(server.data_dir / "manager.sqlite3")
        with conn:
            conn.execute("UPDATE users SET discord_id = ?, discord_pings = '[\"player_inactive\"]' WHERE username = ?",
                         (discord_id, name))
        conn.close()
    return server.client(name, "password123")


def played(server, gid, slot, ago):
    """As if the player of a slot checked a location `ago` seconds ago, and the game began before that."""
    conn = sqlite3.connect(server.data_dir / "manager.sqlite3")
    with conn:
        conn.execute("INSERT INTO game_progress(game_id, slot, at, checked) VALUES (?,?,?,1)",
                     (gid, slot, time.time() - ago))
        conn.execute("UPDATE games SET play_started_at = MIN(COALESCE(play_started_at, ?), ?) WHERE id = ?",
                     (time.time() - ago, time.time() - ago, gid))
    conn.close()


def reminders_for(hook, player):
    return [m for m in hook.messages if m["embeds"][0]["title"] == f"Reminder for {player}"]


def test_a_player_who_stopped_playing_is_reminded_while_the_others_play_on(reminding):
    admin = reminding.client("admin", "adminpass1")
    hook = reminding.hook
    events = admin.get("/api/settings").json()["notify_events"]
    assert "player_inactive" in events  # on for a new server
    assert admin.patch("/api/settings", json={"discord_webhook_url": hook.url, "site_url": "http://manager.test",
                                              "remind_days": 0}).status_code == 200
    ann = user(reminding, admin, "ann", discord_id="111")
    bob = user(reminding, admin, "bob")
    gid, _job = make_game(admin, names=("Napper", "Runner"), owners={"Napper": ann, "Runner": bob})
    slots = {s["name"]: s["slot"] for s in admin.get(f"/api/games/{gid}").json()["slots"]}

    # some of the others' items wait in the napper's world: the reminder says how many
    admin.post(f"/api/games/{gid}/start")
    wait_state(admin, gid, "running")
    for tile in range(1, 8):
        admin.post(f"/api/games/{gid}/command", json={"command": f"/hint_location Napper Tile {tile}"})
    save_now(admin, gid)
    admin.post(f"/api/games/{gid}/stop")
    tracker = admin.get(f"/api/games/{gid}/slots/{slots['Napper']}/tracker").json()
    waiting = sum(1 for h in tracker["hints"] if h["in_my_world"] and not h["for_me"] and not h["found"])

    # the napper stopped a while ago; the runner played since
    played(reminding, gid, slots["Napper"], ago=30)
    played(reminding, gid, slots["Runner"], ago=1)
    time.sleep(3 * DAY)
    assert not reminders_for(hook, "Napper")  # the server's default is never
    assert admin.patch(f"/api/games/{gid}", json={"remind_days": 2}).json()["remind_days"] == 2

    message = wait_for(lambda: reminders_for(hook, "Napper"), timeout=20)[0]
    embed = message["embeds"][0]
    assert "has not played" in embed["description"] and "hand their slot over" in embed["description"]
    assert message["content"] == "<@111>" and message["allowed_mentions"]["users"] == ["111"]
    if waiting:
        assert embed["fields"][0]["value"] == f"Runner: {waiting}"
    else:
        assert not embed.get("fields")
    # the napper sees it on their dashboard and game page; the runner was not reminded
    mine = next(g for g in ann.get("/api/games").json() if g["id"] == gid)["my_slots"][0]
    assert mine["idle_since"] and abs(mine["idle_since"] - (time.time() - 30)) < 10
    assert next(g for g in bob.get("/api/games").json() if g["id"] == gid)["my_slots"][0]["idle_since"] is None
    assert not reminders_for(hook, "Runner")

    # again after as many days, three times at most
    wait_for(lambda: len(reminders_for(hook, "Napper")) == 3, timeout=30)
    time.sleep(5 * DAY)
    assert len(reminders_for(hook, "Napper")) == 3

    # playing again starts over
    played(reminding, gid, slots["Napper"], ago=0)
    wait_for(lambda: next(g for g in ann.get("/api/games").json() if g["id"] == gid)["my_slots"][0]["idle_since"] is None,
             timeout=20)
    hook.clear()
    played(reminding, gid, slots["Runner"], ago=0)
    assert wait_for(lambda: reminders_for(hook, "Napper"), timeout=20)

    # a player on a break is not reminded, nor are the players of a finished game
    hook.clear()
    assert ann.put(f"/api/games/{gid}/slots/{slots['Napper']}/status", json={"status": "away"}).status_code == 200
    played(reminding, gid, slots["Runner"], ago=0)
    time.sleep(5 * DAY)
    assert not reminders_for(hook, "Napper")
    assert ann.put(f"/api/games/{gid}/slots/{slots['Napper']}/status", json={"status": None}).status_code == 200
    assert admin.patch(f"/api/games/{gid}", json={"finished": True}).status_code == 200
    played(reminding, gid, slots["Runner"], ago=0)
    time.sleep(5 * DAY)
    assert not reminders_for(hook, "Napper")

    # a game of its own: never
    assert admin.patch(f"/api/games/{gid}", json={"finished": False, "remind_days": 0}).status_code == 200
    time.sleep(5 * DAY)
    assert not reminders_for(hook, "Napper")
    assert admin.patch(f"/api/games/{gid}", json={"remind_days": 61}).status_code == 400
    assert admin.patch("/api/settings", json={"remind_days": -1}).status_code == 400
