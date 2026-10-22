"""Server process lifecycle: crashes, console exits, and the manager itself restarting (a container restart)."""
import os
import signal
import time

import pytest

from helpers import APClient, make_game, server_pid, wait_state

pytestmark = pytest.mark.ap


def test_a_crashed_server_is_restarted_automatically(admin, ap_server):
    gid, _ = make_game(admin, names=("Crashy",))
    port = admin.get(f"/api/games/{gid}").json()["port"]
    admin.post(f"/api/games/{gid}/start")
    wait_state(admin, gid, "running")
    first_pid = server_pid(gid, ap_server)
    assert first_pid
    os.kill(first_pid, signal.SIGKILL)
    crashed = wait_state(admin, gid, "crashed", timeout=20)
    assert crashed["error"]
    # the manager notices, waits a few seconds and starts it again
    running = wait_state(admin, gid, "running", timeout=60)
    assert running["error"] is None
    new_pid = server_pid(gid, ap_server)
    assert new_pid and new_pid != first_pid
    with APClient(port, "Crashy", "ChecksFinder") as client:
        assert client.connected
    log = [l["text"] for l in admin.get(f"/api/games/{gid}/log").json()["lines"]]
    assert any("restarting in" in l for l in log)
    admin.delete(f"/api/games/{gid}")


def test_crash_restarts_can_be_turned_off(admin, ap_server):
    gid, _ = make_game(admin, names=("Fragile",))
    admin.patch("/api/settings", json={"auto_restart_crashed": False})
    try:
        admin.post(f"/api/games/{gid}/start")
        wait_state(admin, gid, "running")
        os.kill(server_pid(gid, ap_server), signal.SIGKILL)
        wait_state(admin, gid, "crashed", timeout=20)
        time.sleep(8)
        assert admin.get(f"/api/games/{gid}").json()["state"] == "crashed"
        # a manual start still works and clears the error
        assert admin.post(f"/api/games/{gid}/start").status_code == 200
        assert wait_state(admin, gid, "running")["error"] is None
    finally:
        admin.patch("/api/settings", json={"auto_restart_crashed": True})
        admin.delete(f"/api/games/{gid}")


def test_the_exit_command_stops_the_server_for_good(admin, ap_server):
    gid, _ = make_game(admin, names=("Leaver",))
    admin.post(f"/api/games/{gid}/start")
    wait_state(admin, gid, "running")
    assert admin.post(f"/api/games/{gid}/command", json={"command": "/exit"}).status_code == 200
    wait_state(admin, gid, "stopped", timeout=30)
    time.sleep(7)  # longer than the crash-restart delay
    assert admin.get(f"/api/games/{gid}").json()["state"] == "stopped"
    assert server_pid(gid, ap_server) is None
    admin.delete(f"/api/games/{gid}")


def test_stopping_the_manager_saves_progress_and_resumes_running_games(ap_server, admin):
    running_id, _ = make_game(admin, names=("Runner",))
    stopped_id, _ = make_game(admin, names=("Sleeper",))
    port = admin.get(f"/api/games/{running_id}").json()["port"]
    admin.post(f"/api/games/{running_id}/start")
    wait_state(admin, running_id, "running")
    admin.patch(f"/api/games/{running_id}", json={"password": "keepme"})
    time.sleep(1)
    with APClient(port, "Runner", "ChecksFinder", "keepme") as client:
        client.check(5)
    pid = server_pid(running_id, ap_server)

    # container stop: SIGTERM to the manager, which must stop the game servers cleanly (and save) before exiting
    ap_server.stop()
    assert not os.path.exists(f"/proc/{pid}") or open(f"/proc/{pid}/stat").read().split()[2] == "Z"
    assert server_pid(running_id, ap_server) is None, "game server outlived the manager"

    ap_server.start()
    fresh = ap_server.client("admin", "adminpass1")
    # the game that was running is running again, the one that was stopped stays stopped
    assert wait_state(fresh, running_id, "running", timeout=90)["state"] == "running"
    assert fresh.get(f"/api/games/{stopped_id}").json()["state"] == "stopped"
    with APClient(port, "Runner", "ChecksFinder", "keepme") as client:
        assert client.connected and len(client.response["checked_locations"]) == 5  # saved on shutdown, no manual /save
    log = [l["text"] for l in fresh.get(f"/api/games/{running_id}/log").json()["lines"]]
    assert any("starting server" in l for l in log) and any("exited with code" in l for l in log)  # log survives restarts
    fresh.delete(f"/api/games/{running_id}")
    fresh.delete(f"/api/games/{stopped_id}")


def test_a_game_whose_port_is_taken_after_a_restart_reports_it(ap_server, admin):
    import socket
    gid, _ = make_game(admin, names=("Squatted",))
    port = admin.get(f"/api/games/{gid}").json()["port"]
    admin.post(f"/api/games/{gid}/start")
    wait_state(admin, gid, "running")
    ap_server.stop()
    blocker = socket.socket()
    blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # the stopped server's connections may linger in TIME_WAIT
    blocker.bind(("0.0.0.0", port))
    blocker.listen()
    try:
        ap_server.start()
        fresh = ap_server.client("admin", "adminpass1")
        game = wait_state(fresh, gid, "crashed", timeout=30)
        assert "already in use" in game["error"]
    finally:
        blocker.close()
    assert fresh.post(f"/api/games/{gid}/start").status_code == 200
    wait_state(fresh, gid, "running")
    fresh.delete(f"/api/games/{gid}")
