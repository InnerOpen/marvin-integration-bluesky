"""Bluesky provider: Marvin's shared ``social.post`` capability, posted straight to the AT Protocol (no aggregator).

A provider is pure with respect to Marvin: it gets ``config``, the resolved ``secret`` (the app password), a ``logger`` and a safe
``http`` client via ``ctx``, and returns results. It keeps no session: each action signs in with ``createSession``, does its work
and lets the token lapse.
"""

from __future__ import annotations

import hashlib
import html
import re
import threading
import time
from datetime import UTC, datetime
from typing import ClassVar
from urllib.parse import quote, urlencode, urlparse

from marvin_integration_sdk import (
    CATEGORY_DESTINATION,
    CredentialField,
    ErrorPolicy,
    Handle,
    IntegrationContext,
    IntegrationError,
    IntegrationProvider,
    ProviderAction,
    Response,
    Retry,
    register_provider,
)

from .engagement import (
    BSKY_POST_URL,
    COMMENT_KINDS,
    DID,
    HANDLE,
    KIND_REPLY,
    NETWORK,
    NOTIFICATION_REASONS,
    RKEY,
    decode_cursor,
    encode_cursor,
    note_key,
    parse_at_uri,
    post_url,
    profile_ref,
    to_item,
)
from .richtext import build_facets, grapheme_count

DEFAULT_SERVICE = "https://bsky.social"
WEB = "https://bsky.app"
POST_COLLECTION = "app.bsky.feed.post"
POST_TYPE = "app.bsky.feed.post"
THREADGATE_COLLECTION = "app.bsky.feed.threadgate"
THREADGATE_TYPE = "app.bsky.feed.threadgate"

TEXT_MAX_GRAPHEMES = 300  # lexicon app.bsky.feed.post: text maxGraphemes
TEXT_MAX_BYTES = 3000  # …and maxLength (UTF-8 bytes)
MAX_IMAGES = 4  # lexicon app.bsky.embed.images: images maxLength
IMAGE_MAX_BYTES = 1_000_000  # the PDS's blob limit for post images (lexicon maxSize 1000000)
DEFAULT_COMMENTS_LIMIT = 50
MAX_COMMENTS_LIMIT = 200
NOTIFICATION_PAGE_SIZE = 100  # lexicon app.bsky.notification.listNotifications: limit maximum
MAX_NOTIFICATION_PAGES = 20
CHAT_TEXT_MAX_GRAPHEMES = 1000  # lexicon chat.bsky.convo.defs#messageInput: text maxGraphemes (verify)
CHAT_PROXY = "did:web:api.bsky.chat#bsky_chat"  # the atproto-proxy header that routes chat.bsky.* calls through the PDS (verify)
HIDDEN_REPLIES_MAX = 300  # lexicon app.bsky.feed.threadgate: hiddenReplies maxLength (verify)
SESSION_TTL = 3000  # seconds a signed-in session is reused; an access token lives about two hours (verify)
CARD_TITLE_MAX = 200
CARD_DESCRIPTION_MAX = 500
PAGE_READ_BYTES = 262_144  # how much of a linked page is searched for its title

# Stable codes a failure carries (IntegrationError.code); `error_policy` says what Marvin does about each.
CODE_AUTH = "auth"  # no/rejected handle or app password: fix the connection
CODE_RATE_LIMITED = "rate_limited"  # 429
CODE_UNAVAILABLE = "unavailable"  # Bluesky couldn't be reached or failed on its side (5xx)
CODE_UNSUPPORTED = "unsupported"  # what Bluesky can't do (too long, too many or too big images): same code as the other social plugins
CODE_INVALID = "invalid"  # Bluesky refused the request (400)
CODE_UNKNOWN = "unknown"

ERROR_POLICY: ErrorPolicy = {
    CODE_AUTH: Handle(notify=True),
    # A rate-limited request was refused outright, so trying the same post again later cannot double-post.
    CODE_RATE_LIMITED: Handle(retry=Retry(backoff=(60, 300, 900)), then=Handle(notify=True)),
}

IMAGE_SIGNATURES = ((b"\xff\xd8\xff", "image/jpeg"), (b"\x89PNG\r\n\x1a\n", "image/png"), (b"GIF8", "image/gif"))
_RKEY = re.compile(r"^[A-Za-z0-9._:~-]{1,512}$")
_META = re.compile(r"<meta\b[^>]*>", re.IGNORECASE)
_ATTR = re.compile(r"""([a-zA-Z:_-]+)\s*=\s*(?:"([^"]*)"|'([^']*)')""")
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


# createSession is rate-limited (about 300 a day per account, verify), and Marvin's engagement engine reads comments every few minutes,
# so a signed-in session is reused for SESSION_TTL in this process's memory only (never written anywhere). The key holds a digest of
# the app password, not the password; a changed password or handle simply misses the cache.
_SESSIONS: dict[tuple[str, str, str], tuple[float, dict]] = {}
_SESSIONS_LOCK = threading.Lock()


def _refusal(message: str) -> IntegrationError:
    return IntegrationError(message, code=CODE_UNSUPPORTED)


def _flag(value) -> bool:
    """A boolean argument; a workflow may pass the string "false"."""
    return value.strip().lower() in ("1", "true", "yes", "y", "on") if isinstance(value, str) else bool(value)


def _sniff_image(data: bytes) -> str | None:
    for magic, mime in IMAGE_SIGNATURES:
        if data.startswith(magic):
            return mime
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _header(resp: Response, name: str) -> str:
    return next((v for k, v in (resp.headers or {}).items() if k.lower() == name), "")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


_STR = {"type": "string"}
_COMMENT_ITEM = {
    "type": "object",
    "properties": {
        "id": {**_STR, "description": "The post's at:// URI: pass it to reply, like, hide and delete_comment."},
        "kind": {
            "type": "string",
            "enum": list(COMMENT_KINDS),
            "description": "reply: answers one of your posts. mention: a mention, or a quote of your post.",
        },
        "post_id": {
            "type": ["string", "null"],
            "description": "The post this one replies to (record.reply.parent), or the quoted post; null for a plain mention.",
        },
        "post_url": {"type": ["string", "null"], "description": "bsky.app link to that post."},
        "author": {
            "type": "object",
            "properties": {"id": {**_STR, "description": "The author's DID."}, "handle": _STR, "name": {**_STR, "description": "Display name."}},
        },
        "text": _STR,
        "created_at": {**_STR, "description": "ISO 8601, UTC."},
        "url": {"type": ["string", "null"], "description": "https://bsky.app/profile/<handle>/post/<rkey>"},
        "network": {"type": "string", "const": NETWORK},
    },
}
_COMMENTS_OUTPUT = {
    "type": "object",
    "properties": {
        "items": {"type": "array", "items": _COMMENT_ITEM, "description": "Oldest first."},
        "cursor": {**_STR, "description": "Pass as 'since' next time."},
    },
    "required": ["items", "cursor"],
}


@register_provider
class BlueskyProvider(IntegrationProvider):
    slug = "bluesky"
    name = "Bluesky"
    description = (
        "Post to Bluesky (text, up to 4 images with alt text, or a link card) straight to the AT Protocol, and read and answer its replies, "
        "mentions and chat: Marvin's shared social post and engagement capabilities."
    )
    category = CATEGORY_DESTINATION
    icon = "🦋"
    guide = "guide.md"

    credentials = (
        CredentialField(
            key="app_password",
            label="App password",
            help="Create one in Bluesky: Settings → Privacy and security → App passwords. Never your account password.",
        ),
    )
    config_schema: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "handle": {"type": "string", "title": "Handle", "description": "Your Bluesky handle, e.g. name.bsky.social."},
            "service": {
                "type": "string",
                "title": "Service",
                "default": DEFAULT_SERVICE,
                "description": "The PDS to sign in to. Leave as is unless you host your own.",
            },
        },
        "required": ["handle"],
        "additionalProperties": False,
    }

    error_policy: ClassVar[ErrorPolicy] = ERROR_POLICY

    actions = (
        ProviderAction(
            key="post",
            label="Post to Bluesky",
            description=(
                "Marvin's shared social post. The text is posted with its links made clickable; up to 4 pictures are uploaded with "
                "their alt text; a link with no pictures becomes a link card. Bluesky allows 300 characters."
            ),
            capability="social.post",
            input_schema={
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": f"Up to {TEXT_MAX_GRAPHEMES} characters (graphemes), including a link that gets appended.",
                    },
                    "images": {
                        "type": "array",
                        "items": {"type": "string"},
                        "maxItems": MAX_IMAGES,
                        "description": f"Up to {MAX_IMAGES} public https image URLs, each at most 1 MB (JPEG, PNG, WebP or GIF).",
                    },
                    "alt": {"type": "array", "items": {"type": "string"}, "description": "Alt text, one per image, in order."},
                    "link": {
                        "type": "string",
                        "description": "A URL appended to the text (unless it is already there); with no pictures it also becomes a link card.",
                    },
                    "dry_run": {
                        "type": "boolean",
                        "default": False,
                        "description": "Return the post that would be made, without signing in or posting.",
                    },
                },
                "required": ["text"],
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "properties": {
                    "posted": {"type": "boolean"},
                    "post_id": {"type": ["string", "null"], "description": "The post's record key (rkey): what delete_post takes."},
                    "url": {"type": ["string", "null"], "description": "https://bsky.app/profile/<handle>/post/<rkey>"},
                    "network": {"type": "string", "const": "bluesky"},
                    "would_post": {"type": "object", "description": "A dry run only: the record and embed plan that would be sent."},
                },
            },
            cost_hint="free",
            requires_approval=True,
        ),
        ProviderAction(
            key="delete_post",
            label="Delete a post",
            description="Delete one of this account's posts by its record key (the post_id of a post, or the last part of its link): the undo for a mistake.",
            input_schema={
                "type": "object",
                "properties": {
                    "rkey": {
                        "type": "string",
                        "description": "The post's record key, e.g. 3kabc123xyz. A bsky.app or at:// link to the post also works.",
                    },
                    "dry_run": {"type": "boolean", "default": False},
                },
                "required": ["rkey"],
                "additionalProperties": False,
            },
            requires_approval=True,
        ),
        ProviderAction(
            key="comments",
            label="Read replies and mentions",
            description=(
                "Read the replies to your posts, the posts that mention you and the posts that quote you, from your notifications, as "
                "normalised items oldest first. A reply under one of your posts is kind 'reply'; a mention is 'mention'; a quote of one of "
                "your posts is also 'mention' (Bluesky has no separate comments). Pass back the cursor from the last call as 'since' to get "
                "only what is new."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "since": {
                        **_STR,
                        "description": "The cursor this action returned last time (opaque). Omit it on a first run: you get the newest items.",
                    },
                    "limit": {"type": "integer", "minimum": 1, "maximum": MAX_COMMENTS_LIMIT, "default": DEFAULT_COMMENTS_LIMIT},
                    "kinds": {
                        "type": "array",
                        "items": {"type": "string", "enum": list(COMMENT_KINDS)},
                        "description": "Only these kinds (default: all).",
                    },
                },
                "additionalProperties": False,
            },
            capability="social.comments",
            output_schema=_COMMENTS_OUTPUT,
            cost_hint="free",
        ),
        ProviderAction(
            key="reply",
            label="Reply to a reply or mention",
            description=(
                "Reply to a reply or mention. Private (the default) is a Bluesky chat message to its author and needs an app password that "
                "allows access to direct messages; private=false posts a public reply in the thread. A dry run reports what would go out."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "comment_id": {**_STR, "description": "The at:// URI of the post to answer (an item's id from comments)."},
                    "text": {
                        **_STR,
                        "description": f"The reply, as given. Public: up to {TEXT_MAX_GRAPHEMES} characters; chat: up to {CHAT_TEXT_MAX_GRAPHEMES}.",
                    },
                    "private": {"type": "boolean", "default": True},
                    "dry_run": {"type": "boolean", "default": False},
                },
                "required": ["comment_id", "text"],
                "additionalProperties": False,
            },
            capability="social.reply",
            output_schema={
                "type": "object",
                "properties": {
                    "replied": {"type": "boolean"},
                    "reply_id": {"type": ["string", "null"], "description": "Public: the new post's at:// URI. Private: the chat message id."},
                    "private": {"type": "boolean"},
                    "url": {"type": ["string", "null"], "description": "Public replies only: the post's bsky.app link."},
                    "network": {"type": "string", "const": NETWORK},
                    "would_reply": {"type": "object", "description": "A dry run only: what would be sent."},
                },
                "required": ["replied", "network"],
            },
            cost_hint="free",
            requires_approval=True,
        ),
        ProviderAction(
            key="like",
            label="Like a post",
            description="Like a reply, mention or any post by its at:// URI. Easily undone, so it needs no approval.",
            input_schema={
                "type": "object",
                "properties": {"comment_id": {**_STR, "description": "The at:// URI of the post (an item's id from comments)."}},
                "required": ["comment_id"],
                "additionalProperties": False,
            },
            capability="social.like",
            output_schema={"type": "object", "properties": {"liked": {"type": "boolean"}, "network": {"type": "string", "const": NETWORK}}},
            cost_hint="free",
        ),
        ProviderAction(
            key="repost",
            label="Repost a post",
            description="Repost a post to your followers. Public, so a person approves it first.",
            input_schema={
                "type": "object",
                "properties": {
                    "post_id": {**_STR, "description": "The post's at:// URI, its bsky.app link, or the record key of one of your own posts."},
                },
                "required": ["post_id"],
                "additionalProperties": False,
            },
            capability="social.repost",
            output_schema={
                "type": "object",
                "properties": {
                    "reposted": {"type": "boolean"},
                    "url": {"type": ["string", "null"], "description": "The reposted post's bsky.app link."},
                    "network": {"type": "string", "const": NETWORK},
                },
            },
            cost_hint="free",
            requires_approval=True,
        ),
        ProviderAction(
            key="hide",
            label="Hide a reply under your post",
            description=(
                "Hide (or unhide) a reply in a thread that starts with one of your own posts, by adding it to that post's thread gate's hidden "
                "replies. Existing reply rules are kept. A thread that isn't yours is refused with code 'unsupported'. Easily undone."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "comment_id": {**_STR, "description": "The at:// URI of the reply."},
                    "hidden": {"type": "boolean", "default": True, "description": "false shows the reply again."},
                },
                "required": ["comment_id"],
                "additionalProperties": False,
            },
            capability="social.hide",
            output_schema={"type": "object", "properties": {"hidden": {"type": "boolean"}, "network": {"type": "string", "const": NETWORK}}},
            cost_hint="free",
        ),
        ProviderAction(
            key="delete_comment",
            label="Delete one of your replies",
            description=(
                "Delete one of your own posts (such as a reply) by its at:// URI. Bluesky can't delete other people's replies: that is refused "
                "with code 'unsupported'. Not undoable, so a person approves it first."
            ),
            input_schema={
                "type": "object",
                "properties": {"comment_id": {**_STR, "description": "The at:// URI of your own post."}},
                "required": ["comment_id"],
                "additionalProperties": False,
            },
            capability="social.delete_comment",
            output_schema={"type": "object", "properties": {"deleted": {"type": "boolean"}, "network": {"type": "string", "const": NETWORK}}},
            cost_hint="free",
            requires_approval=True,
        ),
        ProviderAction(
            key="message",
            label="Send a chat message",
            description=(
                "Send a Bluesky chat (direct) message to an account given by DID or handle. Needs an app password that allows access to direct "
                "messages, and a recipient who accepts messages from you. A dry run reports what would go out."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "to": {
                        "type": "object",
                        "properties": {
                            "id": {**_STR, "description": "The account's DID (did:plc:...)."},
                            "handle": {**_STR, "description": "name.bsky.social or a custom domain, with or without the leading @."},
                        },
                        "additionalProperties": False,
                    },
                    "text": {**_STR, "description": f"The message, as given (up to {CHAT_TEXT_MAX_GRAPHEMES} characters)."},
                    "dry_run": {"type": "boolean", "default": False},
                },
                "required": ["to", "text"],
                "additionalProperties": False,
            },
            capability="social.message",
            output_schema={
                "type": "object",
                "properties": {
                    "sent": {"type": "boolean"},
                    "message_id": {"type": ["string", "null"]},
                    "network": {"type": "string", "const": NETWORK},
                    "would_send": {"type": "object", "description": "A dry run only: the recipient and text that would go out."},
                },
                "required": ["sent", "network"],
            },
            cost_hint="free",
            requires_approval=True,
        ),
    )

    # ---- plumbing ---------------------------------------------------------------------------

    @staticmethod
    def _cfg(ctx: IntegrationContext) -> dict:
        return ctx.config or {}

    def _handle(self, ctx: IntegrationContext) -> str:
        handle = str(self._cfg(ctx).get("handle") or "").strip().removeprefix("@").lower()
        if not handle:
            raise IntegrationError("Missing Bluesky handle.", code=CODE_AUTH)
        return handle

    def _service(self, ctx: IntegrationContext) -> str:
        service = str(self._cfg(ctx).get("service") or DEFAULT_SERVICE).strip().rstrip("/")
        if urlparse(service).scheme != "https" or not urlparse(service).netloc:
            raise IntegrationError("The Bluesky service must be an https URL (the app password is sent to it).", code=CODE_INVALID)
        return service

    @staticmethod
    def _fail(resp: Response, what: str) -> IntegrationError:
        """The IntegrationError for a non-2xx answer: AT Protocol errors are ``{"error": name, "message": text}``."""
        name = message = ""
        try:
            body = resp.json()
            name, message = str(body.get("error") or ""), str(body.get("message") or "")
        except Exception:  # noqa: BLE001 — not JSON: fall back to the status
            message = resp.text[:200]
        detail = f"{name}: {message}".strip(": ") or "no detail"
        status = resp.status_code
        error = BlueskyProvider._fail_for(status, name, detail, resp, what)
        error.at_error = name  # the AT Protocol error name (RecordNotFound, ExpiredToken...), for callers that handle one specially
        return error

    @staticmethod
    def _fail_for(status: int, name: str, detail: str, resp: Response, what: str) -> IntegrationError:
        if status in (401, 403) or name in ("ExpiredToken", "InvalidToken", "AuthenticationRequired", "AccountTakedown"):
            return IntegrationError(f"Bluesky refused {what}: {detail}. Check the handle and app password.", code=CODE_AUTH)
        if status == 429:
            retry_after = None
            try:
                retry_after = float(_header(resp, "retry-after")) or None
            except ValueError:
                pass
            return IntegrationError(f"Bluesky is rate-limiting {what}; try again in a few minutes.", code=CODE_RATE_LIMITED, retry_after=retry_after)
        if status >= 500 or status == 0:
            return IntegrationError(f"Bluesky had a problem on its side during {what} (HTTP {status}): {detail}.", code=CODE_UNAVAILABLE)
        return IntegrationError(f"Bluesky refused {what} (HTTP {status}): {detail}.", code=CODE_INVALID)

    def _send(self, ctx: IntegrationContext, call, what: str) -> Response:
        try:
            return call()
        except IntegrationError:
            raise
        except Exception as e:  # a transport failure, a timeout, or the SSRF guard
            raise IntegrationError(f"Couldn't reach Bluesky for {what}: {e}", code=CODE_UNAVAILABLE) from e

    def _xrpc_post(
        self,
        ctx: IntegrationContext,
        nsid: str,
        what: str,
        *,
        token: str | None = None,
        body: dict | None = None,
        raw: bytes | None = None,
        content_type: str | None = None,
        timeout: float = 15,
        headers: dict | None = None,
    ) -> dict:
        headers = {**({"Authorization": f"Bearer {token}"} if token else {}), **(headers or {})}
        url = f"{self._service(ctx)}/xrpc/{nsid}"
        if raw is not None:
            headers["Content-Type"] = content_type or "application/octet-stream"
            resp = self._send(ctx, lambda: ctx.http.post(url, data=raw, headers=headers, timeout=timeout), what)
        else:
            resp = self._send(ctx, lambda: ctx.http.post(url, json=body or {}, headers=headers, timeout=timeout), what)
        if not resp.ok:
            raise self._fail(resp, what)
        try:
            return resp.json() if resp.content else {}
        except ValueError as e:
            raise IntegrationError(f"Bluesky sent an unreadable answer during {what}.", code=CODE_UNAVAILABLE) from e

    def _xrpc_get(
        self, ctx: IntegrationContext, nsid: str, what: str, *, token: str, params: list[tuple[str, str]] | None = None, headers: dict | None = None
    ) -> dict:
        query = f"?{urlencode(params)}" if params else ""
        url = f"{self._service(ctx)}/xrpc/{nsid}{query}"
        headers = {"Authorization": f"Bearer {token}", **(headers or {})}
        resp = self._send(ctx, lambda: ctx.http.get(url, headers=headers, timeout=15), what)
        if not resp.ok:
            raise self._fail(resp, what)
        try:
            return resp.json() if resp.content else {}
        except ValueError as e:
            raise IntegrationError(f"Bluesky sent an unreadable answer during {what}.", code=CODE_UNAVAILABLE) from e

    def _session_key(self, ctx: IntegrationContext) -> tuple[str, str, str]:
        return (self._service(ctx), self._handle(ctx), hashlib.sha256(str(ctx.secret or "").encode()).hexdigest())

    def _login(self, ctx: IntegrationContext) -> dict:
        """A fresh session: ``{"token", "did", "handle"}``. The app password never appears in an error."""
        if not ctx.secret:
            raise IntegrationError("Missing Bluesky app password.", code=CODE_AUTH)
        data = self._xrpc_post(ctx, "com.atproto.server.createSession", "sign-in", body={"identifier": self._handle(ctx), "password": ctx.secret})
        if not data.get("accessJwt") or not data.get("did"):
            raise IntegrationError("Bluesky signed in but returned no session.", code=CODE_UNAVAILABLE)
        session = {"token": data["accessJwt"], "did": data["did"], "handle": str(data.get("handle") or self._handle(ctx))}
        with _SESSIONS_LOCK:
            _SESSIONS[self._session_key(ctx)] = (time.monotonic() + SESSION_TTL, session)
        return session

    def _with_session(self, ctx: IntegrationContext, work):
        """``work(session)`` with a reused session when one is fresh enough; if Bluesky rejects that session (expired early), sign in
        again and run it once more. A rejected request wrote nothing, so the second run cannot repeat a write."""
        if not ctx.secret:
            raise IntegrationError("Missing Bluesky app password.", code=CODE_AUTH)
        key = self._session_key(ctx)
        with _SESSIONS_LOCK:
            expires, session = _SESSIONS.get(key, (0.0, {}))
        if session and expires > time.monotonic():
            try:
                return work(session)
            except IntegrationError as e:
                if e.code != CODE_AUTH:
                    raise
                with _SESSIONS_LOCK:
                    _SESSIONS.pop(key, None)
        return work(self._login(ctx))

    def _resolve_handle(self, ctx: IntegrationContext, handle: str) -> str | None:
        """The DID behind a handle, or None (unknown handle or Bluesky unreachable): a mention is never worth failing a post for."""
        try:
            resp = ctx.http.get(f"{self._service(ctx)}/xrpc/com.atproto.identity.resolveHandle?handle={quote(handle, safe='')}")
            return str(resp.json().get("did") or "") or None if resp.ok else None
        except Exception as e:  # noqa: BLE001
            ctx.logger.info("bluesky: could not resolve @%s: %s", handle, e)
            return None

    # ---- health -----------------------------------------------------------------------------

    def check(self, ctx: IntegrationContext) -> tuple[str, str | None]:
        if not ctx.secret:
            return ("unconfigured", "Missing app password.")
        if not self._cfg(ctx).get("handle"):
            return ("unconfigured", "Missing Bluesky handle.")
        try:
            self._login(ctx)
        except IntegrationError as e:
            return ("error", str(e))
        return ("ok", None)

    def run_action(self, key: str, args: dict, ctx: IntegrationContext) -> dict:
        handler = {
            "post": self._action_post,
            "delete_post": self._action_delete_post,
            "comments": self._action_comments,
            "reply": self._action_reply,
            "like": self._action_like,
            "repost": self._action_repost,
            "hide": self._action_hide,
            "delete_comment": self._action_delete_comment,
            "message": self._action_message,
        }.get(key)
        if handler is None:
            raise NotImplementedError(f"bluesky has no action '{key}'")
        return handler(args or {}, ctx)

    # ---- post -------------------------------------------------------------------------------

    @staticmethod
    def _parse_post_args(args: dict) -> tuple[str, list[str], list[str], str, bool]:
        """Validate the `social.post` arguments against what Bluesky can do; every refusal says what to change."""
        text = str(args.get("text") or "").strip()
        if not text:
            raise _refusal("Bluesky needs text for the post.")
        images = args.get("images") or []
        if not isinstance(images, list) or not all(isinstance(u, str) for u in images):
            raise _refusal("'images' must be a list of public image URLs.")
        urls = [u.strip() for u in images if u.strip()]
        if len(urls) > MAX_IMAGES:
            raise _refusal(f"Bluesky allows {MAX_IMAGES} images per post ({len(urls)} given); use per_network with fewer images.")
        if any(not u.lower().startswith("https://") for u in urls):
            raise _refusal("Every image URL must be a public https URL.")
        alt = args.get("alt") or []
        if not isinstance(alt, list) or len(alt) > len(urls):
            raise _refusal(f"'alt' is one text per image ({len(urls)} image(s) given).")
        alts = [str(a or "").strip() for a in alt] + [""] * (len(urls) - len(alt))
        link = str(args.get("link") or "").strip()
        if link and urlparse(link).scheme not in ("http", "https"):
            raise _refusal("'link' must be an http(s) URL.")
        return text, urls, alts, link, bool(args.get("dry_run", False))

    @staticmethod
    def _with_link(text: str, link: str) -> str:
        return f"{text}\n\n{link}" if link and link not in text else text

    @staticmethod
    def _check_length(text: str) -> None:
        n = grapheme_count(text)
        if n > TEXT_MAX_GRAPHEMES or len(text.encode("utf-8")) > TEXT_MAX_BYTES:
            raise _refusal(f"Bluesky allows {TEXT_MAX_GRAPHEMES} characters; use per_network with a shorter text (this one is {n}, link included).")

    def _link_card(self, ctx: IntegrationContext, link: str) -> dict:
        """An ``app.bsky.embed.external`` for ``link``: the page's own title and description when readable, else the link itself."""
        title, description = link, ""
        try:
            resp = ctx.http.get(link)
            if resp.ok and "html" in (_header(resp, "content-type") or "html").lower():
                page = resp.content[:PAGE_READ_BYTES].decode("utf-8", "replace")
                meta = {}
                for tag in _META.findall(page):
                    attrs = {m.group(1).lower(): m.group(2) if m.group(2) is not None else m.group(3) for m in _ATTR.finditer(tag)}
                    name = (attrs.get("property") or attrs.get("name") or "").lower()
                    if name and "content" in attrs:
                        meta.setdefault(name, html.unescape(attrs["content"]).strip())
                found = meta.get("og:title") or meta.get("twitter:title")
                if not found and (m := _TITLE.search(page)):
                    found = " ".join(html.unescape(m.group(1)).split())
                title = found or link
                description = meta.get("og:description") or meta.get("description") or ""
        except Exception as e:  # noqa: BLE001 — a card without the page's details is still a card
            ctx.logger.info("bluesky: could not read %s for the link card: %s", link, e)
        return {"uri": link, "title": title[:CARD_TITLE_MAX], "description": description[:CARD_DESCRIPTION_MAX]}

    def _download_image(self, ctx: IntegrationContext, index: int, url: str) -> tuple[bytes, str]:
        resp = self._send(ctx, lambda: ctx.http.get(url, timeout=30), f"downloading image {index}")
        if not resp.ok:
            raise IntegrationError(f"Image {index} could not be downloaded (HTTP {resp.status_code}): {url}", code=CODE_INVALID)
        data = resp.content
        if not data:
            raise _refusal(f"Image {index} is empty: {url}")
        if len(data) > IMAGE_MAX_BYTES:
            raise _refusal(
                f"Image {index} is {len(data) / 1_000_000:.2f} MB; Bluesky allows images up to {IMAGE_MAX_BYTES // 1_000_000} MB. Use a smaller image."
            )
        declared = _header(resp, "content-type").split(";")[0].strip().lower()
        mime = _sniff_image(data) or (declared if declared.startswith("image/") else None)
        if not mime:
            raise _refusal(f"Image {index} isn't a picture Bluesky accepts (JPEG, PNG, WebP or GIF): {url}")
        return data, mime

    def _plan(self, ctx: IntegrationContext, text: str, urls: list[str], alts: list[str], link: str) -> tuple[dict, list[dict], list[str]]:
        """The record Bluesky would get, with each image's blob still to be uploaded: ``(record, images, notes)``."""
        facets, notes = build_facets(text, lambda h: self._resolve_handle(ctx, h))
        record: dict = {"$type": POST_TYPE, "text": text, "createdAt": _now()}
        if facets:
            record["facets"] = facets
        images = [{"url": u, "alt": a} for u, a in zip(urls, alts, strict=True)]
        if urls and link:
            notes.append("Bluesky can't show a link card beside pictures, so the link is in the text only.")
        elif link:
            record["embed"] = {"$type": "app.bsky.embed.external", "external": self._link_card(ctx, link)}
        return record, images, notes

    def _action_post(self, args: dict, ctx: IntegrationContext) -> dict:
        text, urls, alts, link, dry_run = self._parse_post_args(args)
        text = self._with_link(text, link)
        self._check_length(text)
        record, images, notes = self._plan(ctx, text, urls, alts, link)
        handle = self._handle(ctx)
        if dry_run:
            preview = dict(record)
            if urls:
                preview["embed"] = {
                    "$type": "app.bsky.embed.images",
                    "images": [{"alt": i["alt"], "image": f"<blob uploaded from {i['url']} when posting>"} for i in images],
                }
            return {
                "posted": False,
                "dry_run": True,
                "post_id": None,
                "url": None,
                "network": "bluesky",
                "would_post": {
                    "repo": handle,
                    "collection": POST_COLLECTION,
                    "record": preview,
                    "images": images,
                    "characters": grapheme_count(text),
                    "notes": notes,
                },
            }
        # Download every picture first: a bad one should stop the post before anything is uploaded or sent.
        downloaded = [self._download_image(ctx, i, u) for i, u in enumerate(urls, 1)]
        session = self._login(ctx)
        if urls:
            embedded = []
            for i, ((data, mime), alt) in enumerate(zip(downloaded, alts, strict=True), 1):
                blob = self._xrpc_post(
                    ctx, "com.atproto.repo.uploadBlob", f"uploading image {i}", token=session["token"], raw=data, content_type=mime, timeout=60
                ).get("blob")
                if not blob:
                    raise IntegrationError(f"Bluesky accepted image {i} but returned no blob.", code=CODE_UNAVAILABLE)
                embedded.append({"alt": alt, "image": blob})
            record["embed"] = {"$type": "app.bsky.embed.images", "images": embedded}
        created = self._xrpc_post(
            ctx,
            "com.atproto.repo.createRecord",
            "posting",
            token=session["token"],
            body={"repo": session["did"], "collection": POST_COLLECTION, "record": record},
        )
        uri = str(created.get("uri") or "")
        rkey = uri.rsplit("/", 1)[-1] if uri else ""
        if not rkey:
            raise IntegrationError("Bluesky accepted the post but returned no record key.", code=CODE_UNAVAILABLE)
        return {
            "posted": True,
            "dry_run": False,
            "post_id": rkey,
            "url": f"{WEB}/profile/{session['handle']}/post/{rkey}",
            "network": "bluesky",
            "uri": uri,
            "cid": created.get("cid"),
            "images": len(urls),
            "notes": notes,
        }

    # ---- delete -----------------------------------------------------------------------------

    def _action_delete_post(self, args: dict, ctx: IntegrationContext) -> dict:
        raw = str(args.get("rkey") or "").strip().rstrip("/")
        rkey = raw.rsplit("/", 1)[-1]
        if not _RKEY.match(rkey) or rkey in (".", ".."):
            raise _refusal("delete_post needs the post's record key (rkey), e.g. 3kabc123xyz.")
        if bool(args.get("dry_run", False)):
            return {
                "deleted": False,
                "dry_run": True,
                "rkey": rkey,
                "network": "bluesky",
                "would_delete": {"collection": POST_COLLECTION, "rkey": rkey},
            }
        session = self._login(ctx)
        self._xrpc_post(
            ctx,
            "com.atproto.repo.deleteRecord",
            "deleting the post",
            token=session["token"],
            body={"repo": session["did"], "collection": POST_COLLECTION, "rkey": rkey},
        )
        return {"deleted": True, "dry_run": False, "rkey": rkey, "network": "bluesky"}

    # ---- engagement plumbing ----------------------------------------------------------------

    @staticmethod
    def _required_text(args: dict, key: str, what: str, hint: str) -> str:
        value = str(args.get(key) or "").strip()
        if not value:
            raise _refusal(f"{what} needs '{key}': {hint}.")
        return value

    def _to_uri(self, ctx: IntegrationContext, raw: str, key: str) -> str:
        """The at:// URI of the post ``raw`` names: an at:// URI or a bsky.app post link."""
        if raw.startswith("at://"):
            ref = parse_at_uri(raw)
            if not ref or ref[1] != POST_COLLECTION:
                raise _refusal(f"'{key}' must be the at:// URI of a post (at://did:.../{POST_COLLECTION}/<rkey>), not {raw!r}.")
            return raw
        if m := BSKY_POST_URL.match(raw):
            who, rkey = m.groups()
            did = who if DID.match(who) else self._resolve_handle(ctx, who)
            if not did:
                raise _refusal(f"Couldn't find the Bluesky account @{who} named in '{key}'.")
            return f"at://{did}/{POST_COLLECTION}/{rkey}"
        raise _refusal(f"'{key}' must be the at:// URI of a post (an id from comments), not {raw!r}.")

    def _get_post(self, ctx: IntegrationContext, session: dict, uri: str) -> dict:
        """The post at ``uri`` as the AppView returns it (``app.bsky.feed.getPosts``): its cid, author, record and the viewer's own like/repost."""
        data = self._xrpc_get(ctx, "app.bsky.feed.getPosts", "looking up the post", token=session["token"], params=[("uris", uri)])
        posts = data.get("posts") if isinstance(data.get("posts"), list) else []
        post = next((p for p in posts if isinstance(p, dict) and p.get("uri") == uri and p.get("cid")), None)
        if post is None:
            raise _refusal(f"Bluesky has no post at {uri}: it was deleted, or this account can't see it.")
        return post

    def _create_record(self, ctx: IntegrationContext, session: dict, collection: str, record: dict, what: str, rkey: str | None = None) -> dict:
        body = {"repo": session["did"], "collection": collection, "record": record}
        if rkey:
            body["rkey"] = rkey
        return self._xrpc_post(ctx, "com.atproto.repo.createRecord", what, token=session["token"], body=body)

    def _chat(
        self, ctx: IntegrationContext, session: dict, nsid: str, what: str, *, params: list[tuple[str, str]] | None = None, body: dict | None = None
    ) -> dict:
        """A ``chat.bsky.*`` call, sent through the PDS to the chat service. Chat refusals are 'unsupported', not 'auth': the sign-in worked."""
        headers = {"atproto-proxy": CHAT_PROXY}
        try:
            if body is None:
                return self._xrpc_get(ctx, nsid, what, token=session["token"], params=params, headers=headers)
            return self._xrpc_post(ctx, nsid, what, token=session["token"], body=body, headers=headers)
        except IntegrationError as e:
            if e.code == CODE_AUTH and getattr(e, "at_error", "") != "ExpiredToken":
                raise IntegrationError(
                    f"Bluesky chat refused {what}: this app password can't use direct messages. Create a new app password with 'Allow access to your "
                    f"direct messages' ticked (Settings → Privacy and security → App passwords) and paste it into the connection. ({e})",
                    code=CODE_UNSUPPORTED,
                ) from e
            if e.code == CODE_INVALID:
                raise IntegrationError(
                    f"Bluesky chat won't take this: the person may not accept messages from this account, or has blocked it. ({e})",
                    code=CODE_UNSUPPORTED,
                ) from e
            raise

    def _send_chat(self, ctx: IntegrationContext, session: dict, did: str, text: str) -> str | None:
        """Send ``text`` to ``did`` in their chat with this account (opened if needed); the message id."""
        if did == session["did"]:
            raise _refusal("That is this account's own post: Bluesky chat can't message yourself.")
        convo = self._chat(ctx, session, "chat.bsky.convo.getConvoForMembers", "opening the chat", params=[("members", did)]).get("convo")
        convo_id = str((convo or {}).get("id") or "")
        if not convo_id:
            raise IntegrationError("Bluesky opened the chat but returned no conversation id.", code=CODE_UNAVAILABLE)
        facets, _ = build_facets(text, lambda h: self._resolve_handle(ctx, h))
        message = {"text": text, **({"facets": facets} if facets else {})}
        sent = self._chat(ctx, session, "chat.bsky.convo.sendMessage", "sending the chat message", body={"convoId": convo_id, "message": message})
        return str(sent["id"]) if sent.get("id") else None

    @staticmethod
    def _check_chat_length(text: str) -> None:
        n = grapheme_count(text)
        if n > CHAT_TEXT_MAX_GRAPHEMES:
            raise _refusal(f"A Bluesky chat message allows {CHAT_TEXT_MAX_GRAPHEMES} characters; this one is {n}. Shorten it.")

    # ---- comments (social.comments) ---------------------------------------------------------

    @staticmethod
    def _parse_comments(args: dict) -> tuple[tuple | None, int, tuple[str, ...]]:
        since = args.get("since")
        boundary = None
        if since:
            try:
                boundary = decode_cursor(str(since))
            except ValueError as e:
                raise IntegrationError(str(e), code="invalid_cursor") from e
        raw = args.get("limit")
        try:
            limit = DEFAULT_COMMENTS_LIMIT if raw is None else int(raw)
        except (TypeError, ValueError):
            limit = 0
        if not 1 <= limit <= MAX_COMMENTS_LIMIT:
            raise _refusal(f"'limit' must be a whole number from 1 to {MAX_COMMENTS_LIMIT} (default {DEFAULT_COMMENTS_LIMIT}).")
        kinds = COMMENT_KINDS if args.get("kinds") is None else args["kinds"]
        if not isinstance(kinds, (list, tuple)) or any(k not in COMMENT_KINDS for k in kinds):
            raise _refusal(f"'kinds' must be a list drawn from {', '.join(COMMENT_KINDS)}.")
        return boundary, limit, tuple(kinds)

    def _read_notifications(self, ctx: IntegrationContext, session: dict, boundary, limit: int, kinds: tuple[str, ...]):
        """Reply, mention and quote notifications newer than ``boundary``, as ``(sorted [(key, item)], newest key seen)``.

        listNotifications pages newest first, so it pages back until a page holds something older than the cursor (every newer
        notification is then in hand); without a cursor it stops once ``limit`` items are in hand, so a first run doesn't replay history."""
        found: dict[str, tuple[tuple, dict]] = {}
        newest = None
        page_cursor = None
        for _ in range(MAX_NOTIFICATION_PAGES):
            params = [("limit", str(NOTIFICATION_PAGE_SIZE)), *[("reasons", r) for r in NOTIFICATION_REASONS]]
            if page_cursor:
                params.append(("cursor", page_cursor))
            data = self._xrpc_get(ctx, "app.bsky.notification.listNotifications", "reading notifications", token=session["token"], params=params)
            notes = data.get("notifications")
            if not isinstance(notes, list):
                raise IntegrationError("Bluesky sent an unreadable list of notifications.", code=CODE_UNAVAILABLE)
            passed = False
            for note in (n for n in notes if isinstance(n, dict)):
                key = note_key(note)
                passed = passed or (boundary is not None and key[0] < boundary[0])
                if note.get("reason") not in NOTIFICATION_REASONS:  # the server ignored the reasons filter
                    continue
                newest = key if newest is None or key > newest else newest
                if boundary is not None and key <= boundary:
                    continue
                item = to_item(note, session["did"], session["handle"])
                # One post can notify twice (a reply that also mentions us): keep it once, as the more specific 'reply'.
                if item and item["kind"] in kinds and not (item["id"] in found and found[item["id"]][1]["kind"] == KIND_REPLY):
                    found[item["id"]] = (key, item)
            page_cursor = data.get("cursor")
            if not notes or not page_cursor or passed or (boundary is None and len(found) >= limit):
                break
        else:
            ctx.logger.warning(
                "bluesky: more than %d new notifications; the rest are picked up on the next poll", MAX_NOTIFICATION_PAGES * NOTIFICATION_PAGE_SIZE
            )
        return sorted(found.values(), key=lambda e: e[0]), newest

    def _action_comments(self, args: dict, ctx: IntegrationContext) -> dict:
        boundary, limit, kinds = self._parse_comments(args)
        since = str(args.get("since") or "")
        entries, newest = self._with_session(ctx, lambda session: self._read_notifications(ctx, session, boundary, limit, kinds))
        if newest is None or (boundary is not None and newest <= boundary):
            return {"items": [], "cursor": since}
        if boundary is None:  # no cursor: the newest ``limit``, oldest first
            kept, cursor = entries[-limit:], newest
        elif len(entries) > limit:  # more waiting: stop after the last one handed over, the next poll starts there
            kept = entries[:limit]
            cursor = kept[-1][0]
        else:
            kept, cursor = entries, newest
        return {"items": [item for _, item in kept], "cursor": encode_cursor(cursor)}

    # ---- reply (social.reply) ---------------------------------------------------------------

    def _action_reply(self, args: dict, ctx: IntegrationContext) -> dict:
        raw = self._required_text(args, "comment_id", "reply", "the at:// URI of the post to answer")
        text = self._required_text(args, "text", "reply", "the reply")
        private = True if args.get("private") is None else _flag(args["private"])
        dry_run = _flag(args.get("dry_run", False))
        uri = self._to_uri(ctx, raw, "comment_id")
        if private:
            return self._reply_private(ctx, uri, text, dry_run)
        self._check_length(text)
        return self._with_session(ctx, lambda session: self._reply_public(ctx, session, uri, text, dry_run))

    def _reply_private(self, ctx: IntegrationContext, uri: str, text: str, dry_run: bool) -> dict:
        """Chat to the post's author (the DID is the repo of the at:// URI), so a dry run needs no sign-in."""
        self._check_chat_length(text)
        did = parse_at_uri(uri)[0]  # type: ignore[index]  # _to_uri returned a valid at:// URI
        if dry_run:
            would = {"mode": "chat", "to": did, "about": uri, "text": text, "characters": grapheme_count(text), "via": "chat.bsky.convo.sendMessage"}
            return {"replied": False, "dry_run": True, "reply_id": None, "private": True, "url": None, "network": NETWORK, "would_reply": would}
        message_id = self._with_session(ctx, lambda session: self._send_chat(ctx, session, did, text))
        return {"replied": True, "dry_run": False, "reply_id": message_id, "private": True, "url": None, "network": NETWORK}

    def _reply_public(self, ctx: IntegrationContext, session: dict, uri: str, text: str, dry_run: bool) -> dict:
        parent = self._get_post(ctx, session, uri)
        parent_ref = {"uri": parent["uri"], "cid": parent["cid"]}
        root = ((parent.get("record") or {}).get("reply") or {}).get("root") or {}
        root_ref = {"uri": root["uri"], "cid": root["cid"]} if root.get("uri") and root.get("cid") else parent_ref  # no reply: it is the root
        facets, notes = build_facets(text, lambda h: self._resolve_handle(ctx, h))
        record: dict = {"$type": POST_TYPE, "text": text, "createdAt": _now(), "reply": {"root": root_ref, "parent": parent_ref}}
        if facets:
            record["facets"] = facets
        if dry_run:
            would = {
                "mode": "public",
                "repo": session["handle"],
                "collection": POST_COLLECTION,
                "record": record,
                "characters": grapheme_count(text),
                "notes": notes,
            }
            return {"replied": False, "dry_run": True, "reply_id": None, "private": False, "url": None, "network": NETWORK, "would_reply": would}
        created = self._create_record(ctx, session, POST_COLLECTION, record, "replying")
        new_uri = str(created.get("uri") or "")
        rkey = new_uri.rsplit("/", 1)[-1]
        if not rkey:
            raise IntegrationError("Bluesky accepted the reply but returned no record key.", code=CODE_UNAVAILABLE)
        return {
            "replied": True,
            "dry_run": False,
            "reply_id": new_uri,
            "private": False,
            "url": post_url(session["handle"], rkey),
            "network": NETWORK,
        }

    # ---- like / repost (social.like, social.repost) -----------------------------------------

    def _action_like(self, args: dict, ctx: IntegrationContext) -> dict:
        raw = self._required_text(args, "comment_id", "like", "the at:// URI of the post")

        uri = self._to_uri(ctx, raw, "comment_id")

        def work(session: dict) -> dict:
            post = self._get_post(ctx, session, uri)
            if not (post.get("viewer") or {}).get("like"):  # already liked: a second like record would be a duplicate
                subject = {"uri": post["uri"], "cid": post["cid"]}
                self._create_record(
                    ctx, session, "app.bsky.feed.like", {"$type": "app.bsky.feed.like", "subject": subject, "createdAt": _now()}, "liking"
                )
            return {"liked": True, "network": NETWORK}

        return self._with_session(ctx, work)

    def _action_repost(self, args: dict, ctx: IntegrationContext) -> dict:
        raw = self._required_text(args, "post_id", "repost", "the post's at:// URI, link or record key")

        own_rkey = RKEY.match(raw) and not raw.startswith(("did:", "at:"))  # a bare record key names one of our own posts
        uri = None if own_rkey else self._to_uri(ctx, raw, "post_id")

        def work(session: dict) -> dict:
            post = self._get_post(ctx, session, uri or f"at://{session['did']}/{POST_COLLECTION}/{raw}")
            if not (post.get("viewer") or {}).get("repost"):
                subject = {"uri": post["uri"], "cid": post["cid"]}
                self._create_record(
                    ctx, session, "app.bsky.feed.repost", {"$type": "app.bsky.feed.repost", "subject": subject, "createdAt": _now()}, "reposting"
                )
            rkey = parse_at_uri(post["uri"])[2]  # type: ignore[index]
            return {"reposted": True, "url": post_url(profile_ref(post.get("author") or {}), rkey), "network": NETWORK}

        return self._with_session(ctx, work)

    # ---- hide (social.hide) -----------------------------------------------------------------

    def _threadgate(self, ctx: IntegrationContext, session: dict, rkey: str) -> dict | None:
        """The thread gate on the account's own post ``rkey`` (``{uri, cid, value}``), or None when the post has none."""
        params = [("repo", session["did"]), ("collection", THREADGATE_COLLECTION), ("rkey", rkey)]
        try:
            return self._xrpc_get(ctx, "com.atproto.repo.getRecord", "reading the thread gate", token=session["token"], params=params)
        except IntegrationError as e:
            if getattr(e, "at_error", "") == "RecordNotFound":
                return None
            raise

    def _action_hide(self, args: dict, ctx: IntegrationContext) -> dict:
        raw = self._required_text(args, "comment_id", "hide", "the at:// URI of the reply")
        hidden = True if args.get("hidden") is None else _flag(args["hidden"])

        uri = self._to_uri(ctx, raw, "comment_id")

        def work(session: dict) -> dict:
            root_uri = str((((self._get_post(ctx, session, uri).get("record") or {}).get("reply") or {}).get("root") or {}).get("uri") or "")
            root = parse_at_uri(root_uri)
            if root is None:
                raise _refusal("That post isn't a reply, so there is nothing to hide: Bluesky hides replies in a thread only.")
            if root[0] != session["did"]:
                raise _refusal("Bluesky can hide replies only in threads that start with one of your own posts: this thread is someone else's.")
            return self._set_hidden(ctx, session, uri, root_uri, root[2], hidden)

        return self._with_session(ctx, work)

    def _set_hidden(self, ctx: IntegrationContext, session: dict, uri: str, root_uri: str, root_rkey: str, hidden: bool) -> dict:
        """Add (or remove) ``uri`` in the root post's thread gate. Its reply rules (``allow``) and everything else in the record are kept; a post
        with no gate gets one with no ``allow`` at all, which means anyone may reply, as before."""
        existing = self._threadgate(ctx, session, root_rkey)
        value = dict(existing["value"]) if existing and isinstance(existing.get("value"), dict) else {"createdAt": _now()}
        value.update({"$type": THREADGATE_TYPE, "post": root_uri})
        hidden_replies = [u for u in value.get("hiddenReplies") or [] if isinstance(u, str)]
        if hidden == (uri in hidden_replies):  # nothing to change
            return {"hidden": hidden, "network": NETWORK}
        if hidden:
            if len(hidden_replies) >= HIDDEN_REPLIES_MAX:
                raise _refusal(f"Bluesky lets a post hide at most {HIDDEN_REPLIES_MAX} replies, and this one has reached it.")
            hidden_replies.append(uri)
        else:
            hidden_replies.remove(uri)
        if hidden_replies:
            value["hiddenReplies"] = hidden_replies
        else:
            value.pop("hiddenReplies", None)
        if existing:
            body = {"repo": session["did"], "collection": THREADGATE_COLLECTION, "rkey": root_rkey, "record": value}
            if existing.get("cid"):
                body["swapRecord"] = existing["cid"]  # fail rather than overwrite a change made meanwhile
            self._xrpc_post(ctx, "com.atproto.repo.putRecord", "updating the thread gate", token=session["token"], body=body)
        else:
            self._create_record(ctx, session, THREADGATE_COLLECTION, value, "creating the thread gate", rkey=root_rkey)
        return {"hidden": hidden, "network": NETWORK}

    # ---- delete_comment (social.delete_comment) ---------------------------------------------

    def _action_delete_comment(self, args: dict, ctx: IntegrationContext) -> dict:
        raw = self._required_text(args, "comment_id", "delete_comment", "the at:// URI of your own post")
        did, _, rkey = parse_at_uri(self._to_uri(ctx, raw, "comment_id"))  # type: ignore[misc]

        def work(session: dict) -> dict:
            if did != session["did"]:
                raise _refusal("Bluesky can delete only your own posts: that one belongs to someone else.")
            body = {"repo": session["did"], "collection": POST_COLLECTION, "rkey": rkey}
            self._xrpc_post(ctx, "com.atproto.repo.deleteRecord", "deleting the post", token=session["token"], body=body)
            return {"deleted": True, "network": NETWORK}

        return self._with_session(ctx, work)

    # ---- message (social.message) -----------------------------------------------------------

    def _action_message(self, args: dict, ctx: IntegrationContext) -> dict:
        to = args.get("to")
        if not isinstance(to, dict):
            raise _refusal("message needs 'to': {id} (a DID) or {handle}.")
        text = self._required_text(args, "text", "message", "the message")
        dry_run = _flag(args.get("dry_run", False))
        did = str(to.get("id") or "").strip()
        handle = str(to.get("handle") or "").strip().removeprefix("@").lower()
        if did and not DID.match(did):
            raise _refusal(f"'to.id' must be a Bluesky DID (did:plc:...), not {did!r}; use 'to.handle' for a handle.")
        if not did and not HANDLE.match(handle):
            raise _refusal("message needs 'to.id' (a DID) or 'to.handle' (name.bsky.social).")
        self._check_chat_length(text)
        if not did:
            did = self._resolve_handle(ctx, handle) or ""
        if dry_run:
            would = {
                "to": {"id": did or None, "handle": handle or None},
                "text": text,
                "characters": grapheme_count(text),
                "via": "chat.bsky.convo.sendMessage",
            }
            return {"sent": False, "dry_run": True, "message_id": None, "network": NETWORK, "would_send": would}
        if not did:
            raise _refusal(f"Couldn't find a Bluesky account for @{handle}.")
        message_id = self._with_session(ctx, lambda session: self._send_chat(ctx, session, did, text))
        return {"sent": True, "dry_run": False, "message_id": message_id, "network": NETWORK}
