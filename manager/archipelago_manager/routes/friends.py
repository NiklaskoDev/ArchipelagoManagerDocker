"""Friends (accounts/friends.py): asking, answering, and the list of a player's friends."""
from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..accounts import friends
from ..core.security import current_user

router = APIRouter(prefix="/api")


def _call(fn, *args):
    try:
        return fn(*args)
    except friends.FriendError as e:
        raise HTTPException(e.status, e.message) from None


@router.get("/friends")
def list_friends(user: sqlite3.Row = Depends(current_user)) -> dict:
    """The user's friends, the requests waiting for them and for others, and people they played with."""
    return friends.overview(user["id"])


class FriendRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)


@router.post("/friends")
def ask_friend(body: FriendRequest, user: sqlite3.Row = Depends(current_user)) -> dict:
    """Asks someone, by their user name, to be friends; accepts instead when they asked the user already."""
    status, other = _call(friends.ask, user["id"], body.username)
    return {"status": status, "user": {"id": other["id"], "username": other["username"]}}


@router.post("/friends/{other_id}/accept")
def accept_friend(other_id: int, user: sqlite3.Row = Depends(current_user)) -> dict:
    _call(friends.accept, user["id"], other_id)
    return {"ok": True}


@router.delete("/friends/{other_id}")
def remove_friend(other_id: int, user: sqlite3.Row = Depends(current_user)) -> dict:
    """Ends a friendship, turns a request down or takes one back."""
    return {"removed": _call(friends.remove, user["id"], other_id)}
