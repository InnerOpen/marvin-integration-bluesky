"""A fake ``ctx.http`` that speaks just enough AT Protocol: every call is recorded, answers come from a route table."""

import json
import logging

import pytest
from marvin_integration_sdk import IntegrationContext, Response

SERVICE = "https://bsky.social"
DID = "did:plc:testdid123"
HANDLE = "jared.bsky.social"
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
PIC = "https://assets.example.test/hero.jpg"
PIC2 = "https://assets.example.test/second.png"
FAKE_PASSWORD = "fake-app-password-0000"  # a made-up value: tests never hold a real credential


def reply(status=200, body=None, headers=None, raw: bytes | None = None):
    content = raw if raw is not None else json.dumps(body if body is not None else {}).encode()
    return Response(status_code=status, headers=headers or {}, content=content)


class FakeHttp:
    """GET/POST by URL (exact match, else substring). A value may be a Response, a callable(call) -> Response, or an Exception to raise."""

    def __init__(self):
        self.calls: list[dict] = []
        self.routes: dict[str, object] = {
            f"{SERVICE}/xrpc/com.atproto.server.createSession": lambda c: reply(
                body={"accessJwt": "jwt-1", "refreshJwt": "r-1", "did": DID, "handle": HANDLE}
            ),
            f"{SERVICE}/xrpc/com.atproto.repo.createRecord": lambda c: reply(body={"uri": f"at://{DID}/app.bsky.feed.post/3krkey", "cid": "bafycid"}),
            f"{SERVICE}/xrpc/com.atproto.repo.uploadBlob": lambda c: reply(
                body={
                    "blob": {
                        "$type": "blob",
                        "ref": {"$link": f"bafyblob{sum(1 for x in self.calls if x['url'].endswith('uploadBlob'))}"},
                        "mimeType": c["headers"].get("Content-Type"),
                        "size": len(c["data"]),
                    }
                }
            ),
            f"{SERVICE}/xrpc/com.atproto.repo.deleteRecord": lambda c: reply(body={}),
            f"{SERVICE}/xrpc/com.atproto.repo.putRecord": lambda c: reply(body={"uri": "at://x/y/z", "cid": "bafyput"}),
            PIC: reply(headers={"Content-Type": "image/jpeg"}, raw=JPEG),
            PIC2: reply(headers={"content-type": "image/png"}, raw=PNG),
        }

    def _answer(self, call):
        hit = self.routes.get(call["url"]) or next((v for k, v in self.routes.items() if k in call["url"]), None)
        if hit is None:
            return reply(404, {"error": "NoRoute"})
        if isinstance(hit, Exception):
            raise hit
        return hit(call) if callable(hit) else hit

    def get(self, url, *, headers=None, timeout=15):
        call = {"method": "GET", "url": url, "headers": headers or {}, "json": None, "data": None}
        self.calls.append(call)
        return self._answer(call)

    def post(self, url, *, json=None, data=None, headers=None, timeout=15):
        call = {"method": "POST", "url": url, "headers": headers or {}, "json": json, "data": data}
        self.calls.append(call)
        return self._answer(call)

    def put(self, *a, **k):  # pragma: no cover - the plugin never PUTs
        raise AssertionError("unexpected PUT")

    def delete(self, *a, **k):  # pragma: no cover - the plugin never DELETEs over HTTP verbs
        raise AssertionError("unexpected DELETE")

    def to(self, suffix: str) -> list[dict]:
        return [c for c in self.calls if c["url"].split("?")[0].endswith(suffix)]


@pytest.fixture(autouse=True)
def _fresh_sessions():
    """The provider reuses a signed-in session within the process; every test starts without one."""
    from marvin_integration_bluesky import provider

    provider._SESSIONS.clear()
    yield
    provider._SESSIONS.clear()


@pytest.fixture
def http():
    return FakeHttp()


@pytest.fixture
def ctx(http):
    return IntegrationContext(config={"handle": HANDLE}, secret=FAKE_PASSWORD, http=http, logger=logging.getLogger("test"))


def make_ctx(http, *, config=None, secret=FAKE_PASSWORD):
    return IntegrationContext(config=config if config is not None else {"handle": HANDLE}, secret=secret, http=http, logger=logging.getLogger("test"))
