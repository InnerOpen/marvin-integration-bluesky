"""Sign-in, health, failures Bluesky reports, and delete_post."""

import pytest
from conftest import DID, FAKE_PASSWORD, HANDLE, SERVICE, make_ctx, reply
from marvin_integration_sdk import IntegrationError

from marvin_integration_bluesky import BlueskyProvider

P = BlueskyProvider()
SESSION = f"{SERVICE}/xrpc/com.atproto.server.createSession"
BAD_LOGIN = reply(401, {"error": "AuthenticationRequired", "message": "Invalid identifier or password"})


# ---- health -------------------------------------------------------------------------------


def test_health_ok_when_the_session_is_created(http, ctx):
    assert P.check(ctx) == ("ok", None)
    assert http.calls[0]["json"] == {"identifier": HANDLE, "password": FAKE_PASSWORD}


def test_health_error_on_rejected_credentials_and_the_password_is_not_echoed(http, ctx):
    http.routes[SESSION] = BAD_LOGIN
    status, message = P.check(ctx)
    assert status == "error"
    assert "Invalid identifier or password" in message
    assert FAKE_PASSWORD not in message


def test_health_error_when_bluesky_is_unreachable(http, ctx):
    http.routes[SESSION] = TimeoutError("timed out")
    status, message = P.check(ctx)
    assert status == "error" and "timed out" in message


@pytest.mark.parametrize(
    ("config", "secret", "fragment"),
    [({"handle": HANDLE}, None, "app password"), ({}, FAKE_PASSWORD, "handle")],
)
def test_health_unconfigured_without_secret_or_handle(http, config, secret, fragment):
    status, message = P.check(make_ctx(http, config=config, secret=secret))
    assert status == "unconfigured" and fragment in message
    assert not http.calls


# ---- failures -----------------------------------------------------------------------------


def test_auth_failure_stops_the_post_with_an_auth_error(http, ctx):
    http.routes[SESSION] = BAD_LOGIN
    with pytest.raises(IntegrationError) as e:
        P.run_action("post", {"text": "hi"}, ctx)
    assert e.value.code == "auth"
    assert "Check the handle and app password" in str(e.value)
    assert FAKE_PASSWORD not in str(e.value)
    assert not http.to("createRecord")


def test_a_post_without_a_secret_is_an_auth_error_and_sends_nothing(http):
    with pytest.raises(IntegrationError) as e:
        P.run_action("post", {"text": "hi"}, make_ctx(http, secret=None))
    assert e.value.code == "auth"
    assert not http.calls


def test_rate_limit_carries_the_retry_hint(http, ctx):
    http.routes[f"{SERVICE}/xrpc/com.atproto.repo.createRecord"] = reply(429, {"error": "RateLimitExceeded"}, headers={"retry-after": "120"})
    with pytest.raises(IntegrationError) as e:
        P.run_action("post", {"text": "hi"}, ctx)
    assert e.value.code == "rate_limited" and e.value.retry_after == 120


def test_server_error_is_unavailable(http, ctx):
    http.routes[f"{SERVICE}/xrpc/com.atproto.repo.createRecord"] = reply(502, raw=b"<html>bad gateway</html>")
    with pytest.raises(IntegrationError) as e:
        P.run_action("post", {"text": "hi"}, ctx)
    assert e.value.code == "unavailable"


def test_transport_failure_is_unavailable(http, ctx):
    http.routes[f"{SERVICE}/xrpc/com.atproto.repo.createRecord"] = ConnectionError("refused")
    with pytest.raises(IntegrationError) as e:
        P.run_action("post", {"text": "hi"}, ctx)
    assert e.value.code == "unavailable" and "refused" in str(e.value)


def test_bluesky_refusal_surfaces_its_message(http, ctx):
    http.routes[f"{SERVICE}/xrpc/com.atproto.repo.createRecord"] = reply(
        400, {"error": "InvalidRequest", "message": "Record/text must not be longer"}
    )
    with pytest.raises(IntegrationError) as e:
        P.run_action("post", {"text": "hi"}, ctx)
    assert e.value.code == "invalid" and "must not be longer" in str(e.value)


def test_a_created_record_without_a_uri_is_an_error_not_a_silent_success(http, ctx):
    http.routes[f"{SERVICE}/xrpc/com.atproto.repo.createRecord"] = reply(body={})
    with pytest.raises(IntegrationError):
        P.run_action("post", {"text": "hi"}, ctx)


def test_unknown_action_is_not_implemented(http, ctx):
    with pytest.raises(NotImplementedError):
        P.run_action("nope", {}, ctx)


# ---- delete_post --------------------------------------------------------------------------


def test_delete_post_removes_the_record_from_this_account(http, ctx):
    out = P.run_action("delete_post", {"rkey": "3krkey"}, ctx)
    assert out == {"deleted": True, "dry_run": False, "rkey": "3krkey", "network": "bluesky"}
    (call,) = http.to("deleteRecord")
    assert call["json"] == {"repo": DID, "collection": "app.bsky.feed.post", "rkey": "3krkey"}
    assert call["headers"]["Authorization"] == "Bearer jwt-1"


@pytest.mark.parametrize("given", [f"https://bsky.app/profile/{HANDLE}/post/3krkey", f"at://{DID}/app.bsky.feed.post/3krkey"])
def test_delete_post_accepts_a_link_to_the_post(http, ctx, given):
    assert P.run_action("delete_post", {"rkey": given}, ctx)["rkey"] == "3krkey"


@pytest.mark.parametrize("bad", ["", "..", "has space", "a?b"])
def test_delete_post_refuses_a_bad_key(http, ctx, bad):
    with pytest.raises(IntegrationError):
        P.run_action("delete_post", {"rkey": bad}, ctx)
    assert not http.calls


def test_delete_post_dry_run_deletes_nothing(http, ctx):
    out = P.run_action("delete_post", {"rkey": "3krkey", "dry_run": True}, ctx)
    assert out["deleted"] is False and out["would_delete"]["rkey"] == "3krkey"
    assert not http.calls
