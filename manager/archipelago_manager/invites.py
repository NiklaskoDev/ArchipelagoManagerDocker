"""Invite links and password-reset links: what an administrator hands out instead of an account or a password.

- An invite lets whoever opens it make an account while it has uses left and has not expired: a regular user or an
  organizer, never an administrator (that stays a deliberate step under Users). It works while registration is closed,
  and with Discord when signing in with Discord is set up.
- A reset link lets one user set a new password, once and within a day, without the old one: for someone who forgot
  theirs, as there is no email to send a reset to. Using it signs them out everywhere and revokes their API tokens, as
  when an administrator sets a password.

Both carry a random 256-bit token, of which only a hash is stored, so neither the database nor a backup of it gives a
link away. The token travels in the fragment of the link (#/invite/...), which browsers never send to a server, and
reaches the server in the body of a request, so it is in no address and no log.
"""
from __future__ import annotations

import hashlib
import secrets
import sqlite3
import time

from . import db

ROLES = ("user", "organizer")
DAYS = (1, 7, 30)  # how long an invite can be valid
MAX_USES = 100
NOTE_LENGTH = 100
RESET_TTL = 24 * 3600


class InviteError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.message, self.status = message, status


def _new_token() -> tuple[str, str]:
    token = secrets.token_urlsafe(32)
    return token, _hash(token)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# ----------------------------------------------------------------------------------------------------------------------
# invites
# ----------------------------------------------------------------------------------------------------------------------
def create(creator: sqlite3.Row, role: str, max_uses: int, days: int, note: str) -> tuple[str, int]:
    """A new invite. Returns its token, which is not kept, and its id."""
    if role not in ROLES:
        raise InviteError("An invite makes a user or an organizer. Administrators are made under Users.")
    if not 1 <= max_uses <= MAX_USES:
        raise InviteError(f"An invite can be used 1 to {MAX_USES} times")
    if days not in DAYS:
        raise InviteError("An invite is valid for 1, 7 or 30 days")
    note = " ".join(note.split())
    if len(note) > NOTE_LENGTH:
        raise InviteError(f"Keep the note to {NOTE_LENGTH} characters")
    token, token_hash = _new_token()
    now = time.time()
    invite_id = db.execute("INSERT INTO invites(token_hash, role, note, max_uses, created_by, created_at, expires_at) "
                           "VALUES (?,?,?,?,?,?,?)",
                           (token_hash, role, note or None, max_uses, creator["id"], now, now + days * 86400))
    return token, invite_id


def listing() -> list[dict]:
    """Every invite, newest first, with who made it and who joined with it."""
    joined: dict[int, list[str]] = {}
    for row in db.query("SELECT invite_id, username FROM users WHERE invite_id IS NOT NULL ORDER BY created_at"):
        joined.setdefault(row["invite_id"], []).append(row["username"])
    now = time.time()
    return [{"id": r["id"], "role": r["role"], "note": r["note"], "max_uses": r["max_uses"], "uses": r["uses"],
             "created_at": r["created_at"], "expires_at": r["expires_at"], "created_by": r["creator"],
             "joined": joined.get(r["id"], []),
             "state": "used_up" if r["uses"] >= r["max_uses"] else "expired" if r["expires_at"] <= now else "active"}
            for r in db.query("SELECT i.*, u.username AS creator FROM invites i LEFT JOIN users u ON u.id = i.created_by "
                              "ORDER BY i.created_at DESC")]


def delete(invite_id: int) -> sqlite3.Row | None:
    """Revokes an invite; the accounts made with it stay. Returns what it was, if there was one."""
    with db.connection() as conn:
        row = conn.execute("SELECT * FROM invites WHERE id = ?", (invite_id,)).fetchone()
        if row is not None:
            conn.execute("DELETE FROM invites WHERE id = ?", (invite_id,))
        return row


def find(token: str) -> sqlite3.Row | None:
    """The invite behind a token, if it can still be used."""
    return db.query_one("SELECT i.*, u.username AS creator FROM invites i LEFT JOIN users u ON u.id = i.created_by "
                        "WHERE i.token_hash = ? AND i.uses < i.max_uses AND i.expires_at > ?", (_hash(token), time.time()))


def take_use(invite_id: int) -> sqlite3.Row | None:
    """Takes one use of an invite, if it has one left and has not expired: one statement, so two people cannot both
    take the last one. Hand it back with give_back if no account comes of it."""
    with db.connection() as conn:
        taken = conn.execute("UPDATE invites SET uses = uses + 1 WHERE id = ? AND uses < max_uses AND expires_at > ?",
                             (invite_id, time.time())).rowcount
        return conn.execute("SELECT * FROM invites WHERE id = ?", (invite_id,)).fetchone() if taken else None


def give_back(invite_id: int) -> None:
    db.execute("UPDATE invites SET uses = uses - 1 WHERE id = ? AND uses > 0", (invite_id,))


# ----------------------------------------------------------------------------------------------------------------------
# password-reset links
# ----------------------------------------------------------------------------------------------------------------------
def create_reset(user_id: int, creator: sqlite3.Row) -> tuple[str, float]:
    """A reset link for a user, in place of any they had. Returns its token, which is not kept, and when it expires."""
    token, token_hash = _new_token()
    expires_at = time.time() + RESET_TTL
    with db.connection() as conn:
        conn.execute("DELETE FROM password_resets WHERE user_id = ? OR expires_at <= ?", (user_id, time.time()))
        conn.execute("INSERT INTO password_resets(token_hash, user_id, created_by, created_at, expires_at) "
                     "VALUES (?,?,?,?,?)", (token_hash, user_id, creator["id"], time.time(), expires_at))
    return token, expires_at


def drop_resets(user_id: int) -> None:
    """Ends a user's reset link: their password was set some other way, or the account was disabled."""
    db.execute("DELETE FROM password_resets WHERE user_id = ?", (user_id,))


def find_reset(token: str) -> sqlite3.Row | None:
    """The user a reset link is for, if it can still be used and the account is not disabled."""
    return db.query_one("SELECT u.* FROM password_resets r JOIN users u ON u.id = r.user_id "
                        "WHERE r.token_hash = ? AND r.expires_at > ? AND u.disabled = 0", (_hash(token), time.time()))


def use_reset(token: str, password_hash: str) -> int | None:
    """Sets the new password and spends the link, in one step, so a link works once. Returns the user's id, or None if
    the link cannot be used (any more)."""
    with db.connection() as conn:
        row = conn.execute("SELECT r.user_id FROM password_resets r JOIN users u ON u.id = r.user_id "
                           "WHERE r.token_hash = ? AND r.expires_at > ? AND u.disabled = 0",
                           (_hash(token), time.time())).fetchone()
        if row is None:
            return None
        conn.execute("DELETE FROM password_resets WHERE user_id = ?", (row["user_id"],))
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (password_hash, row["user_id"]))
        return row["user_id"]
