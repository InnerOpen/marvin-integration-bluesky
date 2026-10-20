"""Pure helpers for the shared engagement capabilities (``social.comments`` and friends): at:// URIs, the comments cursor, and
notifications mapped to normalised items. Nothing here touches the network."""

from __future__ import annotations

import base64
import json
import re
from datetime import UTC, datetime

WEB = "https://bsky.app"
NETWORK = "bluesky"
POST_COLLECTION = "app.bsky.feed.post"

KIND_COMMENT, KIND_REPLY, KIND_MENTION = "comment", "reply", "mention"
COMMENT_KINDS = (KIND_COMMENT, KIND_REPLY, KIND_MENTION)
# listNotifications reasons that carry a post worth reading. A quote is reported as a mention (it names the account's post in someone
# else's post, and there is no fourth kind in the shared contract).
NOTIFICATION_REASONS = ("reply", "mention", "quote")

_DID = r"did:[a-z]+:[A-Za-z0-9._:%-]+"
_RKEY = r"[A-Za-z0-9._:~-]{1,512}"
AT_URI = re.compile(rf"^at://({_DID})/([A-Za-z][A-Za-z0-9.]*)/({_RKEY})$")
DID = re.compile(rf"^{_DID}$")
HANDLE = re.compile(r"^(?=.{1,253}$)([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
BSKY_POST_URL = re.compile(rf"^https://(?:www\.)?bsky\.app/profile/([^/?#]+)/post/({_RKEY})/?(?:[?#].*)?$")
RKEY = re.compile(rf"^{_RKEY}$")


def parse_at_uri(uri: str) -> tuple[str, str, str] | None:
    """``(did, collection, rkey)`` for an ``at://`` record URI, else None."""
    m = AT_URI.match(uri or "")
    return (m.group(1), m.group(2), m.group(3)) if m else None


def post_url(handle_or_did: str, rkey: str) -> str:
    return f"{WEB}/profile/{handle_or_did}/post/{rkey}"


def profile_ref(author: dict) -> str:
    """What bsky.app wants in the profile part of a link: the handle, or the DID when the handle is unverified (``handle.invalid``)."""
    handle = str(author.get("handle") or "")
    return handle if handle and handle != "handle.invalid" else str(author.get("did") or "")


def parse_time(value) -> datetime | None:
    try:
        when = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=UTC)


def iso_utc(value) -> str | None:
    when = parse_time(value)
    return when.astimezone(UTC).isoformat() if when else (str(value) if value else None)


# ---- the cursor ---------------------------------------------------------------------------------------------------------------------
# Notifications arrive newest first and Bluesky has no "since" parameter, so the cursor is the position of the newest notification
# handed over: its indexedAt plus its uri (a tie-break for notifications indexed in the same millisecond). It is opaque to callers.


def note_key(note: dict) -> tuple[datetime, str]:
    return (parse_time(note.get("indexedAt")) or datetime.min.replace(tzinfo=UTC), str(note.get("uri") or ""))


def encode_cursor(key: tuple[datetime, str]) -> str:
    raw = json.dumps({"t": key[0].astimezone(UTC).isoformat(timespec="microseconds"), "u": key[1]}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, str]:
    """The key a cursor stands for; ValueError when it is not one this plugin returned."""
    try:
        data = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        when = parse_time(data["t"])
        uri = data["u"]
        if when is None or not isinstance(uri, str):
            raise ValueError
        return when, uri
    except (ValueError, KeyError, TypeError):
        raise ValueError("'since' must be the cursor this plugin returned last time.") from None


# ---- notification -> item -----------------------------------------------------------------------------------------------------------


def to_item(note: dict, me_did: str, me_handle: str) -> dict | None:
    """One reply / mention / quote notification as a ``social.comments`` item (None when it has no usable post)."""
    uri, reason = str(note.get("uri") or ""), note.get("reason")
    parsed = parse_at_uri(uri)
    record = note.get("record")
    author = note.get("author")
    if not parsed or reason not in NOTIFICATION_REASONS or not isinstance(record, dict) or not isinstance(author, dict):
        return None
    refs = record.get("reply") if isinstance(record.get("reply"), dict) else {}
    parent = str((refs.get("parent") or {}).get("uri") or "") or None
    root = str((refs.get("root") or {}).get("uri") or "") or None

    def mine(u: str | None) -> bool:
        ref = parse_at_uri(u) if u else None
        return ref is not None and ref[0] == me_did

    # A reply under one of our posts (its parent or its thread's root) is a "reply"; a mention, and a quote of our post, are "mention"s.
    kind = KIND_REPLY if reason == "reply" and (mine(parent) or mine(root)) else KIND_MENTION
    if parent is None and reason == "quote":
        parent = str(note.get("reasonSubject") or "") or None  # the quoted post
    parent_ref = parse_at_uri(parent) if parent else None
    if parent_ref:
        owner = me_handle if parent_ref[0] == me_did else parent_ref[0]
        parent_url = post_url(owner, parent_ref[2])
    else:
        parent_url = None
    handle = str(author.get("handle") or "")
    did = str(author.get("did") or "")
    return {
        "id": uri,
        "kind": kind,
        "post_id": parent,
        "post_url": parent_url,
        "author": {"id": did, "handle": handle, "name": str(author.get("displayName") or "") or handle},
        "text": str(record.get("text") or ""),
        "created_at": iso_utc(record.get("createdAt") or note.get("indexedAt")),
        "url": post_url(profile_ref(author), parsed[2]),
        "network": NETWORK,
    }
