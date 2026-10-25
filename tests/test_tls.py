"""Encrypted game connections: with a certificate set, the game servers speak wss:// only, the manager's own connection
to them (the text client) follows, and a renewed certificate is taken over once nobody is connected. A minute lasts a
second on the manager of its own here (SLEEP_MINUTE_SECONDS), so it looks at the certificate every five seconds."""
import shutil
import ssl
import subprocess
import time

import pytest
from websockets.sync.client import connect

from helpers import APClient, make_game, wait_for, wait_state

HOST = "play.example.test"  # the public address the test servers have (conftest)

needs_openssl = pytest.mark.skipif(not shutil.which("openssl"), reason="needs the openssl command")


def make_certificate(folder, name="cert", *, host=HOST, days=30):
    """A self-signed certificate for host (and 127.0.0.1, so a test client can check it), and its key."""
    cert, key = folder / f"{name}.pem", folder / f"{name}.key"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out", str(cert),
                    "-days", str(days), "-subj", f"/CN={host}", "-addext", f"subjectAltName=DNS:{host},IP:127.0.0.1"],
                   check=True, capture_output=True)
    return cert, key


def trusting(cert) -> ssl.SSLContext:
    """A client that trusts the test certificate, as a player's client trusts Let's Encrypt."""
    return ssl.create_default_context(cafile=str(cert))


@needs_openssl
def test_a_certificate_is_checked_before_it_is_kept(plain_server, tmp_path):
    admin = plain_server.client()
    assert admin.post("/api/auth/setup", json={"username": "admin", "password": "adminpass1"}).status_code == 200
    cert, key = make_certificate(tmp_path)
    other_cert, other_key = make_certificate(tmp_path, "other", host="elsewhere.example.test", days=5)

    assert admin.get("/api/settings").json()["tls_status"] == {"enabled": False}
    for body, says in (
        ({"tls_cert": "fullchain.pem"}, "full path"),
        ({"tls_cert": str(tmp_path / "missing.pem")}, "no file"),
        ({"tls_cert": str(key), "tls_key": str(key)}, "not a certificate"),
        ({"tls_cert": str(cert), "tls_key": str(other_key)}, "not the private key"),
        ({"tls_key": str(key)}, "as well as its key"),
    ):
        r = admin.patch("/api/settings", json=body)
        assert r.status_code == 400 and says in r.json()["detail"], (body, r.text)
    assert admin.get("/api/settings").json()["tls_cert"] == ""

    r = admin.patch("/api/settings", json={"tls_cert": str(cert), "tls_key": str(key)})
    assert r.status_code == 200, r.text
    status = r.json()["tls_status"]
    assert status["ok"] and status["names"] == [HOST, "127.0.0.1"] and status["self_signed"]
    assert status["covers_public_host"] and not status["expires_soon"]
    assert 29 * 86400 < status["expires_at"] - time.time() < 31 * 86400
    assert admin.get("/api/system").json()["tls"]["ok"]

    # one that is not for the public address, and about to run out, is kept, but the admin hears about both
    status = admin.patch("/api/settings", json={"tls_cert": str(other_cert), "tls_key": str(other_key)}).json()["tls_status"]
    assert status["ok"] and status["covers_public_host"] is False and status["expires_soon"] and not status["expired"]

    # a certificate that breaks later is reported, and other settings can still be saved
    other_cert.write_text("not a certificate")
    r = admin.patch("/api/settings", json={"remind_days": 4})
    assert r.status_code == 200 and not r.json()["tls_status"]["ok"]
    assert "not a certificate" in r.json()["tls_status"]["error"]

    assert admin.patch("/api/settings", json={"tls_cert": "", "tls_key": ""}).json()["tls_status"] == {"enabled": False}


@pytest.fixture(scope="module")
def secure(tmp_path_factory):
    """A manager of its own, with a short minute."""
    from conftest import Server
    server = Server(tmp_path_factory.mktemp("secure") / "data", ap=True, extra_env={"SLEEP_MINUTE_SECONDS": "1"})
    server.start()
    assert server.client().post("/api/auth/setup", json={"username": "admin", "password": "adminpass1"}
                                ).status_code == 200
    yield server
    server.stop()


def log_lines(admin, gid):
    return [line["text"] for line in admin.get(f"/api/games/{gid}/log").json()["lines"]]


def presented(port) -> str:
    """The certificate a server presents, as PEM."""
    return ssl.get_server_certificate(("127.0.0.1", port))


@pytest.mark.ap
@needs_openssl
def test_games_are_served_encrypted_and_take_renewed_certificates_over(secure, tmp_path):
    admin = secure.client("admin", "adminpass1")
    assert admin.post("/api/users", json={"username": "ann", "password": "password123", "role": "user"}
                      ).status_code == 200
    ann = secure.client("ann", "password123")
    cert, key = make_certificate(tmp_path)
    assert admin.patch("/api/settings", json={"tls_cert": str(cert), "tls_key": str(key)}).status_code == 200

    gid, _job = make_game(admin, names=("Lock", "Key"), owners={"Lock": ann})
    assert admin.get(f"/api/games/{gid}").json()["tls"]  # as it will start
    admin.post(f"/api/games/{gid}/start")
    game = wait_state(admin, gid, "running")
    port = game["port"]
    assert game["tls"] and game["address"] == f"{HOST}:{port}"
    assert any("with encrypted connections (wss)" in line for line in log_lines(admin, gid))

    # unencrypted, the server does not answer; encrypted, a client that checks the certificate connects
    with pytest.raises(Exception):  # noqa: B017 - whatever the websockets library makes of a TLS answer
        APClient(port, "Lock", "ChecksFinder")
    with connect(f"wss://127.0.0.1:{port}", ssl=trusting(cert), open_timeout=15) as ws:
        assert '"RoomInfo"' in ws.recv(timeout=15)

    # the text client in the browser connects over wss too
    slot = next(s["slot"] for s in admin.get(f"/api/games/{gid}").json()["slots"] if s["name"] == "Lock")
    url = f"/api/games/{gid}/slots/{slot}/client"
    wait_for(lambda: ann.post(url, json={}).json()["state"] == "connected", timeout=30, message="the text client")
    assert ann.delete(url).status_code == 200

    # a renewed certificate: once nobody is connected, the server restarts with it
    first = presented(port)
    renewed, _ = make_certificate(tmp_path, "renewed")
    shutil.copy(renewed, cert)
    shutil.copy(tmp_path / "renewed.key", key)
    wait_for(lambda: any("certificate for encrypted connections changed" in line for line in log_lines(admin, gid)),
             timeout=30, message="the restart for the renewed certificate")
    wait_state(admin, gid, "running")
    assert presented(port) != first
    assert ssl.PEM_cert_to_DER_cert(presented(port)) == ssl.PEM_cert_to_DER_cert(renewed.read_text())

    # a game whose clients cannot encrypt runs without it, also after changing over by itself
    options = admin.get(f"/api/games/{gid}").json()["options"]
    assert admin.patch(f"/api/games/{gid}", json={"options": {**options, "tls": False}}).status_code == 200
    wait_for(lambda: not admin.get(f"/api/games/{gid}").json()["tls"]
             and admin.get(f"/api/games/{gid}").json()["state"] == "running", timeout=30, message="unencrypted")
    with APClient(port, "Lock", "ChecksFinder") as client:
        assert client.connected
    assert admin.get("/api/settings").json()["tls_status"]["games_without"] == 1
    assert admin.patch(f"/api/games/{gid}", json={"options": {**options, "tls": True}}).status_code == 200
    wait_for(lambda: admin.get(f"/api/games/{gid}").json()["tls"], timeout=30, message="encrypted again")

    # with a certificate that cannot be used, an encrypted game does not start rather than start unencrypted
    admin.post(f"/api/games/{gid}/stop")
    cert.write_text("broken")
    r = admin.post(f"/api/games/{gid}/start")
    assert r.status_code >= 400 and "cannot be used" in r.text, r.text
    assert not admin.get("/api/system").json()["tls"]["ok"]

    assert admin.patch("/api/settings", json={"tls_cert": "", "tls_key": ""}).status_code == 200
    admin.post(f"/api/games/{gid}/start")
    assert not wait_state(admin, gid, "running")["tls"]
    admin.delete(f"/api/games/{gid}")
