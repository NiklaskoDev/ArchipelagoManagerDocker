"""The audit log of administrators' changes, and the backup of the whole data folder."""
import io
import sqlite3
import zipfile

from helpers import player_yaml, upload_yaml


def setup(server):
    client = server.client()
    assert client.post("/api/auth/setup", json={"username": "boss", "password": "bosspass123",
                                                "code": server.setup_code()}).status_code == 200
    return client


def log(admin, **params):
    r = admin.get("/api/audit", params=params)
    assert r.status_code == 200, r.text
    return r.json()["entries"]


def test_administrators_changes_are_logged(plain_server):
    admin = setup(plain_server)
    assert log(admin) == []
    r = admin.post("/api/users", json={"username": "ann", "password": "password123", "role": "user"})
    ann_id = r.json()["id"]
    ann = plain_server.client("ann", "password123")
    assert ann.get("/api/audit").status_code == 403

    # who, what, on what, and what changed (never a password)
    admin.patch(f"/api/users/{ann_id}", json={"role": "admin", "password": "newpassword1"})
    admin.patch(f"/api/users/{ann_id}", json={"role": "user"})
    admin.patch("/api/settings", json={"allow_registration": True, "public_host": "games.example.test"})
    entries = log(admin)
    assert [e["action"] for e in entries] == ["settings.change", "user.change", "user.change", "user.add"]
    assert all(e["username"] == "boss" for e in entries)
    added = entries[-1]
    assert added["target"] == {"kind": "user", "id": ann_id, "name": "ann"} and added["details"] == {"role": "user"}
    assert entries[2]["details"] == {"role": "admin", "password": "changed"}
    assert "newpassword1" not in str(entries)
    assert entries[0]["details"] == {"changed": ["allow_registration", "public_host"]}

    # what a player does for themselves, and what fails, is not in it
    ann = plain_server.client("ann", "newpassword1")
    upload_yaml(ann, "mine", player_yaml("Ann"))
    assert admin.post("/api/users", json={"username": "ann", "password": "password123", "role": "user"}).status_code == 409
    assert len(log(admin)) == 4

    # a deleted one keeps its name
    signup = admin.post("/api/signups", json={"name": "Weekend round"}).json()
    admin.delete(f"/api/signups/{signup['id']}")
    deleted, created = log(admin)[:2]
    assert (created["action"], created["target"]["name"]) == ("signup.add", "Weekend round")
    assert (deleted["action"], deleted["target"]) == ("signup.delete", {"kind": "signup", "id": signup["id"],
                                                                         "name": "Weekend round"})
    # filters, and pages of older entries
    assert [e["action"] for e in log(admin, action="signup")] == ["signup.delete", "signup.add"]
    assert log(admin, user_id=ann_id) == []
    assert [e["action"] for e in log(admin, before=deleted["id"])][:1] == ["signup.add"]


def test_a_backup_of_everything(plain_server):
    admin = setup(plain_server)
    admin.post("/api/users", json={"username": "ann", "password": "password123", "role": "user"})
    ann = plain_server.client("ann", "password123")
    config = upload_yaml(ann, "kept", player_yaml("Ann")).json()["created"][0]
    assert ann.post("/api/system/backup").status_code == 403

    r = admin.post("/api/system/backup")
    assert r.status_code == 200, r.text
    made = r.json()
    assert made["size"] > 0 and made["url"].startswith("/api/system/backup/")
    assert ann.get(made["url"]).status_code == 403
    download = admin.get(made["url"])
    assert download.status_code == 200 and download.headers["content-type"] == "application/zip"
    assert "archipelago-manager-" in download.headers["content-disposition"]
    archive = zipfile.ZipFile(io.BytesIO(download.content))
    names = set(archive.namelist())
    assert "manager.sqlite3" in names and not any(n.startswith(("tmp/", "templates/")) for n in names)
    assert f"configs/{config['user_id']}/{config['id']}.yaml" in names
    # the database is a working copy
    copy = plain_server.data_dir.parent / "restored.sqlite3"
    copy.write_bytes(archive.read("manager.sqlite3"))
    conn = sqlite3.connect(copy)
    assert {row[0] for row in conn.execute("SELECT username FROM users")} == {"boss", "ann"}
    conn.close()
    # it is fetched once, and does not stay behind
    assert admin.get(made["url"]).status_code == 404
    assert not list((plain_server.data_dir / "tmp").glob("backup-*"))
    assert log(admin, action="system")[0]["action"] == "system.backup"
