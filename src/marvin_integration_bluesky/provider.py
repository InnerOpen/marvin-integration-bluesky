"""Bluesky provider: Marvin's shared ``social.post`` capability, posted straight to the AT Protocol (no aggregator).

A provider is pure with respect to Marvin: it gets ``config``, the resolved ``secret`` (the app password), a ``logger`` and a safe
``http`` client via ``ctx``, and returns results. It keeps no session: each action signs in with ``createSession``, does its work
and lets the token lapse.
"""

from __future__ import annotations

import html
import re
from datetime import UTC, datetime
from typing import ClassVar
from urllib.parse import quote, urlparse

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

from .richtext import build_facets, grapheme_count

DEFAULT_SERVICE = "https://bsky.social"
WEB = "https://bsky.app"
POST_COLLECTION = "app.bsky.feed.post"
POST_TYPE = "app.bsky.feed.post"

TEXT_MAX_GRAPHEMES = 300  # lexicon app.bsky.feed.post: text maxGraphemes
TEXT_MAX_BYTES = 3000  # …and maxLength (UTF-8 bytes)
MAX_IMAGES = 4  # lexicon app.bsky.embed.images: images maxLength
IMAGE_MAX_BYTES = 1_000_000  # the PDS's blob limit for post images (lexicon maxSize 1000000)
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


def _refusal(message: str) -> IntegrationError:
    return IntegrationError(message, code=CODE_UNSUPPORTED)


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


@register_provider
class BlueskyProvider(IntegrationProvider):
    slug = "bluesky"
    name = "Bluesky"
    description = "Post to Bluesky: Marvin's shared social post (text, up to 4 images with alt text, or a link card) straight to the AT Protocol."
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
    ) -> dict:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
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

    def _login(self, ctx: IntegrationContext) -> dict:
        """A fresh session: ``{"token", "did", "handle"}``. Never persisted; the app password never appears in an error."""
        if not ctx.secret:
            raise IntegrationError("Missing Bluesky app password.", code=CODE_AUTH)
        data = self._xrpc_post(ctx, "com.atproto.server.createSession", "sign-in", body={"identifier": self._handle(ctx), "password": ctx.secret})
        if not data.get("accessJwt") or not data.get("did"):
            raise IntegrationError("Bluesky signed in but returned no session.", code=CODE_UNAVAILABLE)
        return {"token": data["accessJwt"], "did": data["did"], "handle": str(data.get("handle") or self._handle(ctx))}

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
        handler = {"post": self._action_post, "delete_post": self._action_delete_post}.get(key)
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
