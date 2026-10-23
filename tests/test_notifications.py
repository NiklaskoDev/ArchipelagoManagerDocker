"""Discord notifications. A local fake stands in for Discord, so nothing leaves the machine."""
import ast
import json
import os
import signal
import string
import time
from pathlib import Path

import pytest

from helpers import (APClient, FakeWebhook, build_apworld, make_game, player_yaml, run_job, save_now, server_pid,
                     upload_yaml, wait_for, wait_state)

ALL_EVENTS = ["game_online", "game_crashed", "player_goal", "hint_found", "generation", "signup", "apworld_pending",
              "apworld_update", "ap_update"]
CHECKS_FINDER_BASE = 81000  # ChecksFinder's locations are "Tile 1" (81000) to "Tile 25"


def setup_admin(server):
    client = server.client()
    assert client.post("/api/auth/setup", json={"username": "boss", "password": "bosspass123"}).status_code == 200
    return client


@pytest.fixture
def hook():
    fake = FakeWebhook()
    yield fake
    fake.close()


def new_round(client, name="Round", **extra):
    r = client.post("/api/signups", json={"name": name, **extra})
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------------------------------------------------------------
# settings and delivery
# ---------------------------------------------------------------------------------------------------------------------
def test_notification_settings_are_validated(plain_server):
    admin = setup_admin(plain_server)
    settings = admin.get("/api/settings").json()
    assert settings["discord_webhook_url"] == "" and settings["notify_events"] == ALL_EVENTS
    assert settings["notify_status"]["last_error"] is None
    for bad in ("ftp://example.test/hook", "javascript:alert(1)", "https://", "not a url", "https://x/" + "a" * 600):
        assert admin.patch("/api/settings", json={"discord_webhook_url": bad}).status_code in (400, 422), bad
    assert admin.patch("/api/settings", json={"site_url": "tower.local:8080"}).status_code == 400
    assert admin.patch("/api/settings", json={"notify_events": ["game_online", "fireworks"]}).status_code == 400
    r = admin.patch("/api/settings", json={"discord_webhook_url": "  https://discord.com/api/webhooks/1/abc  ",
                                           "notify_events": ["signup", "game_online", "signup"]})
    assert r.status_code == 200
    assert r.json()["discord_webhook_url"] == "https://discord.com/api/webhooks/1/abc"
    assert r.json()["notify_events"] == ["game_online", "signup"]  # cleaned up and in a fixed order
    assert admin.patch("/api/settings", json={"discord_webhook_url": ""}).json()["discord_webhook_url"] == ""


def test_a_test_message_reaches_the_webhook(plain_server, hook):
    admin = setup_admin(plain_server)
    r = admin.post("/api/settings/test-notification", json={})
    assert r.status_code == 400 and "webhook address first" in r.json()["detail"]

    # the address in the form can be tried before it is saved
    r = admin.post("/api/settings/test-notification", json={"discord_webhook_url": hook.url})
    assert r.status_code == 200 and r.json()["notify_status"]["last_ok_at"]
    post = hook.posts[0]
    assert post["path"] == "/api/webhooks/1/secret-token"
    assert post["user_agent"].startswith("DiscordBot (")  # Discord's edge turns away Python's default user agent
    assert post["body"]["allowed_mentions"] == {"parse": []}  # names in messages can never ping anyone
    assert post["body"]["embeds"][0]["title"] == "Test message"
    assert admin.get("/api/settings").json()["discord_webhook_url"] == ""  # trying it did not save it

    hook.answers.append((404, {"message": "Unknown Webhook", "code": 10015}))
    r = admin.post("/api/settings/test-notification", json={"discord_webhook_url": hook.url})
    assert r.status_code == 400 and "Unknown Webhook" in r.json()["detail"]

    admin.post("/api/users", json={"username": "player", "password": "password123", "role": "user"})
    user = plain_server.client("player", "password123")
    assert user.post("/api/settings/test-notification", json={"discord_webhook_url": hook.url}).status_code == 403


def test_rate_limits_are_waited_out_and_failures_are_reported(plain_server, hook):
    admin = setup_admin(plain_server)
    admin.patch("/api/settings", json={"discord_webhook_url": hook.url})

    hook.answers.append((429, {"message": "You are being rate limited.", "retry_after": 0.3, "global": False}))
    new_round(admin, "Rate limited round")
    message = hook.wait_for("Sign-ups open: Rate limited round", timeout=20)
    assert len(hook.posts) == 2 and hook.posts[0]["status"] == 429  # tried again after the pause Discord asked for
    assert "url" not in message  # no address of the web UI is set, so there is nothing to link to

    hook.answers.append((404, {"message": "Unknown Webhook", "code": 10015}))
    new_round(admin, "Lost round")
    status = wait_for(lambda: (s := admin.get("/api/settings").json()["notify_status"])["last_error"] and s,
                      timeout=20, message="the failure to be reported")
    assert "Unknown Webhook" in status["last_error"]
    assert len(hook.posts) == 3  # a 404 is final: no retries

    # switched-off kinds of events send nothing
    admin.patch("/api/settings", json={"notify_events": ["game_online"]})
    new_round(admin, "Quiet round")
    time.sleep(2)
    assert "Sign-ups open: Quiet round" not in hook.titles()
    # and without an address nothing is sent at all
    admin.patch("/api/settings", json={"notify_events": ALL_EVENTS, "discord_webhook_url": ""})
    new_round(admin, "Silent round")
    time.sleep(2)
    assert len(hook.posts) == 3


# ---------------------------------------------------------------------------------------------------------------------
# messages in German
# ---------------------------------------------------------------------------------------------------------------------
def tr_texts() -> dict[str, str]:
    """The English texts the code hands to notify.tr(), and where."""
    package = Path(__file__).resolve().parents[1] / "manager" / "archipelago_manager"
    texts = {}
    for path in sorted(package.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not (isinstance(node, ast.Call) and node.args
                    and "tr" in (getattr(node.func, "id", None), getattr(node.func, "attr", None))):
                continue
            for part in ast.walk(node.args[0]):  # both texts of tr("..." if one else "...")
                if isinstance(part, ast.Constant) and isinstance(part.value, str):
                    texts.setdefault(part.value, f"{path.name}:{node.lineno}")
    return texts


def fields(text: str) -> set[str]:
    return {name for _literal, name, _spec, _conversion in string.Formatter().parse(text) if name is not None}


def test_the_german_texts_fit_the_code():
    from archipelago_manager.notify_de import MESSAGES
    wrong = [f"{key!r} -> {value!r}" for key, value in MESSAGES.items() if fields(key) != fields(value)]
    assert not wrong, "Other placeholders than the English text:\n" + "\n".join(wrong)
    used = tr_texts()
    missing = [f"{text!r} ({where})" for text, where in used.items() if text not in MESSAGES]
    assert not missing, "Not in notify_de.py:\n" + "\n".join(missing)
    unused = [key for key in MESSAGES if key not in used]
    assert not unused, "No longer used: " + ", ".join(map(repr, unused))


def test_messages_can_be_in_german(plain_server, hook):
    admin = setup_admin(plain_server)
    assert admin.get("/api/settings").json()["notify_language"] == "en"
    assert admin.patch("/api/settings", json={"notify_language": "fr"}).status_code == 400
    r = admin.patch("/api/settings", json={"notify_language": "de", "discord_webhook_url": hook.url})
    assert r.status_code == 200 and r.json()["notify_language"] == "de"

    assert admin.post("/api/settings/test-notification", json={}).status_code == 200
    assert hook.embeds[0]["title"] == "Testnachricht"
    assert hook.embeds[0]["description"] == \
        "Benachrichtigungen von diesem Archipelago Manager kommen in diesem Kanal an."

    deadline = int(time.time()) + 3 * 86400
    signup = new_round(admin, "Freitags*runde", deadline=deadline, allowed_games=["Clique", "A_Game"])
    opened = hook.wait_for("Anmeldung geöffnet: Freitags*runde", timeout=20)
    assert opened["description"] == f"Reiche deine YAML bis <t:{deadline}:F> ein (<t:{deadline}:R>)."
    assert {"name": "Spiele", "value": "A\\_Game, Clique", "inline": False} in opened["fields"]  # still escaped
    assert {"name": "Configs pro Spieler", "value": "1", "inline": True} in opened["fields"]
    assert admin.post(f"/api/signups/{signup['id']}/close").status_code == 200
    closed = hook.wait_for("Anmeldung geschlossen: Freitags*runde", timeout=20)
    assert closed["description"] == "0 Anmeldungen. Niemand hat sich angemeldet."

    # and back to English
    assert admin.patch("/api/settings", json={"notify_language": "en"}).json()["notify_language"] == "en"
    new_round(admin, "Back in English")
    english = hook.wait_for("Sign-ups open: Back in English", timeout=20)
    assert english["description"] == "Enter your YAML until an administrator closes the sign-ups."


# ---------------------------------------------------------------------------------------------------------------------
# events from games
# ---------------------------------------------------------------------------------------------------------------------
@pytest.mark.ap
def test_a_game_announces_itself_its_finishers_and_its_crashes(admin, make_user, discord, ap_server):
    star_name, star = make_user("star")
    gid, _job = make_game(admin, names=("St*r", "Plain"), owners={"St*r": star})
    ready = discord.wait_for("New game ready")
    assert ready["url"] == f"http://manager.test:8080/#/games/{gid}"
    assert "start it from the game page" in ready["description"]
    players = next(f["value"] for f in ready["fields"] if f["name"] == "Players")
    assert "St\\*r (ChecksFinder) · " + star_name in players  # markdown in names is escaped
    assert "Generated by admin" in ready["description"]

    # a start by a person is announced once the server accepts connections
    assert star.post(f"/api/games/{gid}/start").status_code == 200
    game = wait_state(admin, gid, "running")
    online = discord.wait_for("is online", timeout=20)
    assert {"name": "Server", "value": f"`play.example.test:{game['port']}`", "inline": True} in online["fields"]
    # a restart is not news
    admin.post(f"/api/games/{gid}/restart")
    wait_state(admin, gid, "running")
    time.sleep(1)
    assert discord.titles().count(online["title"]) == 1

    # a hinted item is announced when it is found; a hint for a location checked before is not news
    port = game["port"]
    tile = lambda location: f"Tile {location - CHECKS_FINDER_BASE + 1}"  # noqa: E731
    log_lines = lambda: [line["text"] for line in admin.get(f"/api/games/{gid}/log").json()["lines"]]  # noqa: E731
    with APClient(port, "St*r", "ChecksFinder") as client:
        assert client.connected
        early, later = client.missing[0], client.missing[1]
        client.ws.send(json.dumps([{"cmd": "LocationChecks", "locations": [early]}]))
        time.sleep(1)
        save_now(admin, gid)
        time.sleep(22)  # one look at the save in between, so the check counts as earlier than the hint
        # a client can turn a location it already checked into a hint (the server keeps those)
        client.ws.send(json.dumps([{"cmd": "LocationScouts", "locations": [early], "create_as_hint": 2}]))
        r = admin.post(f"/api/games/{gid}/command", json={"command": f"/hint_location St*r {tile(later)}"})
        assert r.status_code == 200
        # console commands are queued by the server: check the location only once the hint exists
        wait_for(lambda: any(f"is at {tile(later)} in" in line for line in log_lines()), timeout=15, message="the hint")
        client.ws.send(json.dumps([{"cmd": "LocationChecks", "locations": [later]}]))
        time.sleep(1)
    save_now(admin, gid)
    found = discord.wait_for("Hinted item found")
    assert f"at {tile(later)} in" in found["description"] and found["description"].startswith("**St\\*r** found ")
    assert found["url"] == f"http://manager.test:8080/#/games/{gid}"
    assert len([t for t in discord.titles() if "hinted item" in t.lower()]) == 1  # not the early one

    # goals are read from the save file
    for name, game_name in (("St*r", "ChecksFinder"), ("Plain", "APQuest")):
        with APClient(port, name, game_name) as client:
            assert client.connected
            client.ws.send(json.dumps([{"cmd": "StatusUpdate", "status": 30}]))
            time.sleep(1)
        save_now(admin, gid)
        if name == "St*r":
            goal = discord.wait_for("St*r reached their goal")
            assert {"name": "Done", "value": "1 of 2 players", "inline": True} in goal["fields"]
    discord.wait_for("Plain reached their goal")
    assert discord.wait_for("Everyone finished")["title"] == f"Everyone finished {game['name']}!"

    # a crash says what happens next
    os.kill(server_pid(gid, ap_server), signal.SIGKILL)
    crash = discord.wait_for("crashed", timeout=20)
    assert "exit code -9" in crash["description"] and "Restarting automatically in 5 seconds" in crash["description"]
    wait_state(admin, gid, "running", timeout=60)

    # an admin can switch kinds of messages off
    events = [e for e in ALL_EVENTS if e != "game_online"]
    admin.patch("/api/settings", json={"notify_events": events})
    admin.post(f"/api/games/{gid}/stop")
    wait_state(admin, gid, "stopped")
    admin.post(f"/api/games/{gid}/start")
    wait_state(admin, gid, "running")
    time.sleep(1)
    assert discord.titles().count(online["title"]) == 1
    admin.post(f"/api/games/{gid}/stop")
    admin.delete(f"/api/games/{gid}")


@pytest.mark.ap
def test_a_game_can_post_to_its_own_channel(admin, make_user, discord, hook):
    _ann_name, ann = make_user("ann")
    gid, _job = make_game(admin, names=("Hook", "Line"), owners={"Hook": ann})
    discord.wait_for("New game ready")
    # like a password: only administrators see and set it
    assert admin.get(f"/api/games/{gid}").json()["discord_webhook_url"] == ""
    assert "discord_webhook_url" not in ann.get(f"/api/games/{gid}").json()
    assert ann.patch(f"/api/games/{gid}", json={"discord_webhook_url": hook.url}).status_code == 403
    r = admin.patch(f"/api/games/{gid}", json={"discord_webhook_url": "not a link"})
    assert r.status_code == 400 and "http" in r.json()["detail"]
    r = admin.patch(f"/api/games/{gid}", json={"discord_webhook_url": f" {hook.url} "})
    assert r.status_code == 200 and r.json()["discord_webhook_url"] == hook.url
    assert "discord_webhook_url" not in ann.get(f"/api/games/{gid}").json()

    # a test message says which game the channel is for
    assert ann.post(f"/api/games/{gid}/test-notification", json={}).status_code == 403
    r = admin.post(f"/api/games/{gid}/test-notification", json={})
    assert r.status_code == 200 and r.json()["notify_status"]["last_ok_at"]
    test = hook.wait_for("Test message", timeout=10)
    name = admin.get(f"/api/games/{gid}").json()["name"]
    assert test["description"] == f"Messages about {name} arrive in this channel."
    assert test["url"] == f"http://manager.test:8080/#/games/{gid}"

    # messages about the game go to its channel instead of the main one, also without a main one
    admin.patch("/api/settings", json={"discord_webhook_url": ""})
    assert ann.post(f"/api/games/{gid}/start").status_code == 200
    port = wait_state(admin, gid, "running")["port"]
    hook.wait_for("is online", timeout=20)
    with APClient(port, "Hook", "ChecksFinder") as client:
        assert client.connected
        client.ws.send(json.dumps([{"cmd": "StatusUpdate", "status": 30}]))
        time.sleep(1)
    save_now(admin, gid)
    hook.wait_for("Hook reached their goal")
    assert not [t for t in discord.titles() if "is online" in t or "reached their goal" in t]
    assert admin.get(f"/api/games/{gid}").json()["notify_status"]["last_ok_at"]

    # a channel that cannot be reached is reported on the game, not on the main webhook
    main_before = admin.get("/api/settings").json()["notify_status"]
    admin.patch(f"/api/games/{gid}", json={"discord_webhook_url": "http://127.0.0.1:9/nothing-listens-here"})
    assert admin.get(f"/api/games/{gid}").json()["notify_status"] is None  # a new address starts over
    admin.post(f"/api/games/{gid}/stop")
    wait_state(admin, gid, "stopped")
    admin.post(f"/api/games/{gid}/start")
    wait_state(admin, gid, "running")
    failed = wait_for(lambda: admin.get(f"/api/games/{gid}").json()["notify_status"], timeout=60,
                      message="the failed delivery")
    assert failed["last_error"].startswith("Could not reach the webhook") and not failed["last_ok_at"]
    main_after = admin.get("/api/settings").json()["notify_status"]
    assert (main_after["last_error"], main_after["last_error_at"]) == (main_before["last_error"],
                                                                       main_before["last_error_at"])

    # without its own channel, the game's messages are back in the main one
    admin.patch("/api/settings", json={"discord_webhook_url": discord.url})
    admin.patch(f"/api/games/{gid}", json={"discord_webhook_url": ""})
    admin.post(f"/api/games/{gid}/stop")
    wait_state(admin, gid, "stopped")
    admin.post(f"/api/games/{gid}/start")
    wait_state(admin, gid, "running")
    discord.wait_for("is online", timeout=20)
    admin.post(f"/api/games/{gid}/stop")
    admin.delete(f"/api/games/{gid}")


@pytest.mark.ap
def test_failed_generation_and_waiting_custom_games_are_reported(admin, make_user, discord, tmp_path):
    r = upload_yaml(admin, f"broken-{time.time_ns()}", "name: Broken\ngame: APQuest\nAPQuest:\n  player_sprite: banana\n")
    job = run_job(admin, [r.json()["created"][0]["id"]], name="Doomed")
    assert job["status"] == "failed"
    failed = discord.wait_for("Generation failed: Doomed")
    assert failed["url"] == f"http://manager.test:8080/#/jobs/{job['id']}" and "banana" in failed["description"]

    name, user = make_user("submitter")
    path = build_apworld(tmp_path / "waiting.apworld", game="Waiting Game", module="waitinggame")
    with open(path, "rb") as f:
        r = user.post("/api/apworlds", files={"file": ("waiting.apworld", f.read())})
    assert r.status_code == 200 and r.json()["status"] == "pending"
    waiting = discord.wait_for("Custom game waiting for approval: Waiting Game")
    assert name in waiting["description"] and waiting["url"] == "http://manager.test:8080/#/worlds"
    admin.delete(f"/api/apworlds/{r.json()['id']}")

    # an admin's own upload installs right away: nothing to approve, nothing to announce
    discord.clear()
    with open(path, "rb") as f:
        r = admin.post("/api/apworlds", files={"file": ("waiting.apworld", f.read())})
    assert r.json()["status"] == "installed"
    time.sleep(1)
    assert discord.titles() == []
    admin.delete(f"/api/apworlds/{r.json()['id']}")


@pytest.mark.ap
def test_an_unreachable_webhook_does_not_get_in_the_way(admin, discord):
    admin.patch("/api/settings", json={"discord_webhook_url": "http://127.0.0.1:9/nothing-listens-here"})
    r = upload_yaml(admin, f"calm-{time.time_ns()}", player_yaml("Calm"))
    job = run_job(admin, [r.json()["created"][0]["id"]])
    assert job["status"] == "succeeded"
    admin.delete(f"/api/games/{job['game_id']}")
