"""The shared engagement capabilities: social.comments (cursor, kinds, paging), reply (public refs, private chat), like, repost, hide
(thread gate), delete_comment, message, their refusals, dry runs, auth errors and declarations."""

from urllib.parse import parse_qs, urlsplit

import pytest
from conftest import DID, FAKE_PASSWORD, HANDLE, SERVICE, make_ctx, reply
from marvin_integration_sdk import IntegrationError

from marvin_integration_bluesky import BlueskyProvider
from marvin_integration_bluesky.engagement import decode_cursor, encode_cursor, note_key

P = BlueskyProvider()
SESSION = f"{SERVICE}/xrpc/com.atproto.server.createSession"
NOTIFICATIONS = f"{SERVICE}/xrpc/app.bsky.notification.listNotifications"
GET_POSTS = f"{SERVICE}/xrpc/app.bsky.feed.getPosts"
GET_RECORD = f"{SERVICE}/xrpc/com.atproto.repo.getRecord"
PUT_RECORD = f"{SERVICE}/xrpc/com.atproto.repo.putRecord"
RESOLVE = f"{SERVICE}/xrpc/com.atproto.identity.resolveHandle"
GET_CONVO = f"{SERVICE}/xrpc/chat.bsky.convo.getConvoForMembers"
SEND_MESSAGE = f"{SERVICE}/xrpc/chat.bsky.convo.sendMessage"
CHAT_PROXY = "did:web:api.bsky.chat#bsky_chat"
BAD_LOGIN = reply(401, {"error": "AuthenticationRequired", "message": "Invalid identifier or password"})

ALICE = {"did": "did:plc:alice", "handle": "alice.example.com", "displayName": "Alice A"}
BOB = {"did": "did:plc:bob", "handle": "bob.bsky.social", "displayName": ""}
MINE = f"at://{DID}/app.bsky.feed.post/3kmine"  # one of the account's own posts
ALICE_ROOT = f"at://{ALICE['did']}/app.bsky.feed.post/3kaliceroot"
ALICE_REPLY = f"at://{ALICE['did']}/app.bsky.feed.post/3kalicereply"  # a reply under MINE
MY_ROOT_REF = {"uri": MINE, "cid": "cid-root"}


def run(key, args, http, **ctx_kwargs):
    return P.run_action(key, args, make_ctx(http, **ctx_kwargs))


def refused(key, args, http, code="unsupported"):
    with pytest.raises(IntegrationError) as caught:
        run(key, args, http)
    assert caught.value.code == code
    return caught.value


def at(second: int) -> str:
    return f"2026-10-19T08:30:{second:02d}.000Z"


def note(n, reason="reply", *, author=ALICE, text=None, parent=MINE, root=None, indexed=None, subject=None, uri=None):
    """A notification; ``n`` makes the post's rkey and its indexedAt second."""
    uri = uri or f"at://{author['did']}/app.bsky.feed.post/3k{n:04d}"
    record = {"$type": "app.bsky.feed.post", "text": text if text is not None else f"text {n}", "createdAt": f"2026-10-19T08:29:{n % 60:02d}.500Z"}
    if parent:
        record["reply"] = {"root": {"uri": root or parent, "cid": "c"}, "parent": {"uri": parent, "cid": "c"}}
    return {
        "uri": uri,
        "cid": f"cid{n}",
        "author": author,
        "reason": reason,
        "reasonSubject": subject or (parent if reason in ("reply", "quote") else None),
        "record": record,
        "isRead": False,
        "indexedAt": indexed or at(n),
    }


def feed(http, notes, page=100, honour_reasons=True):
    """Serve ``notes`` newest first, ``page`` at a time, the next page named by the cursor (like listNotifications)."""
    served = []

    def handler(call):
        query = parse_qs(urlsplit(call["url"]).query)
        wanted = set(query.get("reasons", []))
        ordered = sorted(notes, key=lambda n: (n["indexedAt"], n["uri"]), reverse=True)
        if honour_reasons and wanted:
            ordered = [n for n in ordered if n["reason"] in wanted]
        start = int(query["cursor"][0]) if "cursor" in query else 0
        served.append(query)
        body = {"notifications": ordered[start : start + page]}
        if start + page < len(ordered):
            body["cursor"] = str(start + page)
        return reply(body=body)

    http.routes[NOTIFICATIONS] = handler
    return served


def post(uri, cid, *, author=ALICE, parent=None, root=None, viewer=None, text="a post"):
    record = {"$type": "app.bsky.feed.post", "text": text, "createdAt": at(1)}
    if parent:
        record["reply"] = {"root": root or parent, "parent": parent}
    return {"uri": uri, "cid": cid, "author": author, "record": record, "viewer": viewer or {}}


def posts(http, *items):
    """getPosts answers for the ``uris`` asked, from ``items``; an unknown uri is simply absent."""
    by_uri = {p["uri"]: p for p in items}

    def handler(call):
        asked = parse_qs(urlsplit(call["url"]).query).get("uris", [])
        return reply(body={"posts": [by_uri[u] for u in asked if u in by_uri]})

    http.routes[GET_POSTS] = handler


def chat(http, *, convo="convo-1", sent_id="msg-1"):
    http.routes[GET_CONVO] = lambda c: reply(body={"convo": {"id": convo, "members": []}})
    http.routes[SEND_MESSAGE] = lambda c: reply(body={"id": sent_id, "rev": "r1", "text": c["json"]["message"]["text"]})


def writes(http):
    return [c for c in http.calls if c["method"] == "POST" and not c["url"].endswith("createSession")]


# ---- comments: shape, kinds ---------------------------------------------------------------------------------------------


def test_comments_maps_a_reply_notification_to_the_contract_item(http):
    feed(http, [note(7, text="great post", parent=MINE)])
    out = run("comments", {}, http)
    assert out["items"] == [
        {
            "id": "at://did:plc:alice/app.bsky.feed.post/3k0007",
            "kind": "reply",
            "post_id": MINE,
            "post_url": f"https://bsky.app/profile/{HANDLE}/post/3kmine",
            "author": {"id": "did:plc:alice", "handle": "alice.example.com", "name": "Alice A"},
            "text": "great post",
            "created_at": "2026-10-19T08:29:07.500000+00:00",
            "url": "https://bsky.app/profile/alice.example.com/post/3k0007",
            "network": "bluesky",
        }
    ]
    assert decode_cursor(out["cursor"]) == note_key(note(7))


def test_comments_reads_the_three_reasons_with_the_session_token(http):
    served = feed(http, [note(1)])
    run("comments", {}, http)
    assert sorted(served[0]["reasons"]) == ["mention", "quote", "reply"] and served[0]["limit"] == ["100"]
    call = http.to("listNotifications")[0]
    assert call["headers"]["Authorization"] == "Bearer jwt-1"
    assert "atproto-proxy" not in call["headers"]


def test_a_reply_is_a_reply_a_mention_and_a_quote_are_mentions(http):
    feed(
        http,
        [
            note(1, "reply", parent=MINE),
            note(2, "mention", parent=None),
            note(3, "quote", parent=None, subject=MINE),
            note(4, "mention", parent=ALICE_ROOT, author=BOB),  # a reply to someone else's post that mentions us
        ],
    )
    items = run("comments", {}, http)["items"]
    assert [i["kind"] for i in items] == ["reply", "mention", "mention", "mention"]
    assert [i["post_id"] for i in items] == [MINE, None, MINE, ALICE_ROOT]
    assert [i["post_url"] for i in items] == [
        f"https://bsky.app/profile/{HANDLE}/post/3kmine",
        None,
        f"https://bsky.app/profile/{HANDLE}/post/3kmine",
        "https://bsky.app/profile/did:plc:alice/post/3kaliceroot",
    ]


def test_a_reply_deep_in_a_thread_of_ours_is_still_a_reply_with_its_own_parent(http):
    feed(http, [note(1, parent=ALICE_REPLY, root=MINE, author=BOB)])
    item = run("comments", {}, http)["items"][0]
    assert item["kind"] == "reply" and item["post_id"] == ALICE_REPLY


def test_kinds_filters_but_the_cursor_still_moves_past_what_was_skipped(http):
    feed(http, [note(1, "reply"), note(2, "mention", parent=None)])
    out = run("comments", {"kinds": ["reply"]}, http)
    assert [i["kind"] for i in out["items"]] == ["reply"]
    assert decode_cursor(out["cursor"]) == note_key(note(2, "mention", parent=None))
    assert run("comments", {"kinds": ["comment"]}, http)["items"] == []  # Bluesky has no separate comments


def test_a_post_that_notifies_twice_is_one_item_the_reply(http):
    both = [note(5, "mention", parent=MINE), note(5, "reply", parent=MINE)]
    feed(http, both)
    items = run("comments", {}, http)["items"]
    assert [(i["id"], i["kind"]) for i in items] == [(both[0]["uri"], "reply")]


def test_names_fall_back_to_the_handle_and_an_unverified_handle_links_by_did(http):
    ghost = {"did": "did:plc:ghost", "handle": "handle.invalid", "displayName": ""}
    feed(http, [note(1, author=BOB), note(2, author=ghost)])
    first, second = run("comments", {}, http)["items"]
    assert first["author"] == {"id": "did:plc:bob", "handle": "bob.bsky.social", "name": "bob.bsky.social"}
    assert second["url"] == "https://bsky.app/profile/did:plc:ghost/post/3k0002"


def test_notifications_without_a_usable_post_are_skipped_and_other_reasons_ignored(http):
    broken = note(1)
    broken["record"] = None
    like = note(3, "like", parent=None)
    feed(http, [broken, note(2), like], honour_reasons=False)
    out = run("comments", {}, http)
    assert [i["id"] for i in out["items"]] == [note(2)["uri"]]
    assert decode_cursor(out["cursor"]) == note_key(note(2))  # a like does not move the cursor


# ---- comments: cursor, order, limit, paging -----------------------------------------------------------------------------


def test_items_come_back_oldest_first_and_the_cursor_is_the_newest_notification(http):
    feed(http, [note(30), note(10), note(20)])
    out = run("comments", {}, http)
    assert [i["id"][-4:] for i in out["items"]] == ["0010", "0020", "0030"]
    assert decode_cursor(out["cursor"]) == note_key(note(30))


def test_the_cursor_returns_only_newer_items_and_an_empty_poll_keeps_it(http):
    feed(http, [note(10), note(20), note(30)])
    cursor = encode_cursor(note_key(note(20)))
    out = run("comments", {"since": cursor}, http)
    assert [i["id"][-4:] for i in out["items"]] == ["0030"]
    again = run("comments", {"since": out["cursor"]}, http)
    assert again == {"items": [], "cursor": out["cursor"]}


def test_two_polls_see_every_notification_exactly_once_as_new_ones_arrive(http):
    notes = [note(1), note(2), note(3)]
    feed(http, notes, page=2)
    first = run("comments", {}, http)
    notes += [note(4), note(5)]
    second = run("comments", {"since": first["cursor"]}, http)
    third = run("comments", {"since": second["cursor"]}, http)
    seen = [i["id"][-4:] for i in first["items"] + second["items"] + third["items"]]
    assert seen == ["0001", "0002", "0003", "0004", "0005"] and third["items"] == []


def test_it_pages_back_through_the_notification_cursor_until_it_passes_the_old_one(http):
    served = feed(http, [note(n) for n in range(1, 8)], page=2)
    out = run("comments", {"since": encode_cursor(note_key(note(3)))}, http)
    assert [i["id"][-4:] for i in out["items"]] == ["0004", "0005", "0006", "0007"]
    assert [q.get("cursor") for q in served] == [None, ["2"], ["4"]]  # the third page holds note 3 and 2: stop there


def test_notifications_indexed_in_the_same_instant_are_neither_repeated_nor_lost(http):
    same = sorted((note(n, indexed=at(9)) for n in (1, 2, 3)), key=lambda n: n["uri"])
    feed(http, same)
    # A cursor in the middle of the tie hands over exactly the one after it on the uri tie-break, and then nothing.
    out = run("comments", {"since": encode_cursor(note_key(same[1]))}, http)
    assert [i["id"] for i in out["items"]] == [same[2]["uri"]]
    assert run("comments", {"since": out["cursor"]}, http)["items"] == []
    # And a first run hands over all three, oldest (by the tie-break) first.
    assert [i["id"] for i in run("comments", {}, http)["items"]] == [n["uri"] for n in same]


def test_limit_hands_over_the_oldest_and_the_cursor_stops_there_so_nothing_is_lost(http):
    feed(http, [note(n) for n in range(1, 6)])
    cursor = encode_cursor(note_key(note(0)))
    one = run("comments", {"since": cursor, "limit": 2}, http)
    two = run("comments", {"since": one["cursor"], "limit": 2}, http)
    three = run("comments", {"since": two["cursor"], "limit": 2}, http)
    assert [[i["id"][-4:] for i in r["items"]] for r in (one, two, three)] == [["0001", "0002"], ["0003", "0004"], ["0005"]]


def test_a_first_run_returns_only_the_newest_limit_and_stops_paging_early(http):
    served = feed(http, [note(n) for n in range(1, 10)], page=3)
    out = run("comments", {"limit": 2}, http)
    assert [i["id"][-4:] for i in out["items"]] == ["0008", "0009"] and len(served) == 1
    assert decode_cursor(out["cursor"]) == note_key(note(9))


def test_the_default_limit_is_50(http):
    feed(http, [note(n % 60, indexed=f"2026-10-19T08:{n // 60:02d}:{n % 60:02d}.000Z") for n in range(1, 60)])
    assert len(run("comments", {}, http)["items"]) == 50


def test_a_foreign_cursor_is_invalid_cursor_for_the_engine_to_drop(http):
    for bad in ("12345", "not base64!", 7):
        assert refused("comments", {"since": bad}, http, code="invalid_cursor")
    assert not http.calls


@pytest.mark.parametrize("args", [{"limit": 0}, {"limit": "x"}, {"limit": 201}, {"kinds": ["like"]}, {"kinds": "reply"}])
def test_bad_comment_arguments_are_refused_before_any_request(args, http):
    refused("comments", args, http)
    assert not http.calls


def test_no_notifications_at_all_returns_an_empty_cursor(http):
    feed(http, [])
    assert run("comments", {}, http) == {"items": [], "cursor": ""}


def test_a_notifications_body_that_is_not_a_list_is_unavailable(http):
    http.routes[NOTIFICATIONS] = reply(body={"notifications": "nope"})
    refused("comments", {}, http, code="unavailable")


def test_the_cursor_round_trips_and_rejects_garbage():
    key = note_key(note(3))
    assert decode_cursor(encode_cursor(key)) == key
    with pytest.raises(ValueError):
        decode_cursor("e30")  # {}


# ---- the session is reused, and replaced when Bluesky rejects it ---------------------------------------------------------


def test_a_signed_in_session_is_reused_between_actions(http):
    feed(http, [note(1)])
    run("comments", {}, http)
    run("comments", {}, http)
    assert len(http.to("createSession")) == 1


def test_a_different_password_does_not_reuse_the_session(http):
    feed(http, [note(1)])
    run("comments", {}, http)
    run("comments", {}, http, secret="another-fake-password")
    assert len(http.to("createSession")) == 2


def test_an_expired_cached_session_signs_in_again_and_retries_once(http):
    feed(http, [note(1)])
    run("comments", {}, http)
    good = http.routes[NOTIFICATIONS]
    expired = iter([reply(400, {"error": "ExpiredToken", "message": "Token has expired"})])
    http.routes[NOTIFICATIONS] = lambda c: next(expired, None) or good(c)
    assert len(run("comments", {}, http)["items"]) == 1
    assert len(http.to("createSession")) == 2


def test_a_session_rejected_straight_after_signing_in_is_an_auth_error_not_a_loop(http):
    http.routes[NOTIFICATIONS] = reply(401, {"error": "InvalidToken", "message": "nope"})
    assert refused("comments", {}, http, code="auth")
    assert len(http.to("createSession")) == 1 and len(http.to("listNotifications")) == 1


# ---- reply: public ------------------------------------------------------------------------------------------------------


def test_a_public_reply_carries_root_and_parent_refs_from_the_parent_posts_thread(http):
    parent = {"uri": ALICE_REPLY, "cid": "cid-parent"}
    posts(http, post(ALICE_REPLY, "cid-parent", parent={"uri": MINE, "cid": "cid-mine"}, root=MY_ROOT_REF))
    http.routes[f"{SERVICE}/xrpc/com.atproto.repo.createRecord"] = reply(body={"uri": f"at://{DID}/app.bsky.feed.post/3knew", "cid": "cid-new"})
    out = run("reply", {"comment_id": ALICE_REPLY, "text": "thanks! see https://example.com", "private": False}, http)
    assert out == {
        "replied": True,
        "dry_run": False,
        "reply_id": f"at://{DID}/app.bsky.feed.post/3knew",
        "private": False,
        "url": f"https://bsky.app/profile/{HANDLE}/post/3knew",
        "network": "bluesky",
    }
    sent = http.to("createRecord")[0]["json"]
    assert sent["repo"] == DID and sent["collection"] == "app.bsky.feed.post"
    record = sent["record"]
    assert record["reply"] == {"root": {"uri": MINE, "cid": "cid-root"}, "parent": parent}
    assert record["text"] == "thanks! see https://example.com" and record["$type"] == "app.bsky.feed.post"
    assert record["facets"][0]["features"][0] == {"$type": "app.bsky.richtext.facet#link", "uri": "https://example.com"}
    assert http.to("getPosts")[0]["headers"]["Authorization"] == "Bearer jwt-1"


def test_replying_to_a_top_level_post_makes_it_both_root_and_parent(http):
    posts(http, post(ALICE_ROOT, "cid-top"))
    run("reply", {"comment_id": ALICE_ROOT, "text": "hi", "private": False}, http)
    ref = {"uri": ALICE_ROOT, "cid": "cid-top"}
    assert http.to("createRecord")[0]["json"]["record"]["reply"] == {"root": ref, "parent": ref}


def test_a_string_false_means_public_and_the_bsky_app_link_works_as_the_id(http):
    posts(http, post(ALICE_ROOT, "cid-top"))
    run("reply", {"comment_id": "https://bsky.app/profile/did:plc:alice/post/3kaliceroot", "text": "hi", "private": "false"}, http)
    assert http.to("createRecord")[0]["json"]["record"]["reply"]["parent"]["uri"] == ALICE_ROOT


def test_a_public_reply_is_checked_against_the_300_character_limit_before_anything_is_sent(http):
    refused("reply", {"comment_id": ALICE_ROOT, "text": "x" * 301, "private": False}, http)
    assert not http.calls


def test_a_reply_to_a_post_that_is_gone_is_unsupported_and_posts_nothing(http):
    posts(http)
    err = refused("reply", {"comment_id": ALICE_ROOT, "text": "hi", "private": False}, http)
    assert "no post" in str(err) and not http.to("createRecord")


# ---- reply: private (chat) ----------------------------------------------------------------------------------------------


def test_a_private_reply_is_a_chat_message_to_the_commenter_through_the_chat_proxy(http):
    chat(http, convo="convo-9", sent_id="msg-9")
    out = run("reply", {"comment_id": ALICE_REPLY, "text": "thanks, DM'd you the link"}, http)
    assert out == {"replied": True, "dry_run": False, "reply_id": "msg-9", "private": True, "url": None, "network": "bluesky"}
    convo_call, send_call = http.to("getConvoForMembers")[0], http.to("sendMessage")[0]
    assert parse_qs(urlsplit(convo_call["url"]).query) == {"members": ["did:plc:alice"]}
    assert convo_call["method"] == "GET" and send_call["method"] == "POST"
    for call in (convo_call, send_call):
        assert call["headers"]["atproto-proxy"] == CHAT_PROXY and call["headers"]["Authorization"] == "Bearer jwt-1"
    assert send_call["json"] == {"convoId": "convo-9", "message": {"text": "thanks, DM'd you the link"}}
    assert not http.to("createRecord")  # nothing public


def test_a_chat_scope_refusal_is_unsupported_and_says_how_to_fix_it(http):
    http.routes[GET_CONVO] = reply(401, {"error": "InvalidToken", "message": "Bad token scope"})
    err = refused("reply", {"comment_id": ALICE_REPLY, "text": "hi"}, http)
    assert "direct messages" in str(err) and "app password" in str(err) and FAKE_PASSWORD not in str(err)
    assert not http.to("sendMessage")


def test_a_recipient_who_does_not_accept_messages_is_unsupported(http):
    http.routes[GET_CONVO] = reply(
        400, {"error": "InvalidRequest", "message": "recipient requires incoming messages to come from someone they follow"}
    )
    err = refused("reply", {"comment_id": ALICE_REPLY, "text": "hi"}, http)
    assert "may not accept messages" in str(err) and "come from someone they follow" in str(err)


def test_a_refusal_while_sending_is_unsupported_too(http):
    chat(http)
    http.routes[SEND_MESSAGE] = reply(400, {"error": "InvalidRequest", "message": "blocked"})
    refused("reply", {"comment_id": ALICE_REPLY, "text": "hi"}, http)


def test_an_expired_session_in_chat_is_retried_not_mistaken_for_a_missing_scope(http):
    chat(http)
    run("reply", {"comment_id": ALICE_REPLY, "text": "one"}, http)
    good = http.routes[GET_CONVO]
    expired = iter([reply(400, {"error": "ExpiredToken", "message": "Token has expired"})])
    http.routes[GET_CONVO] = lambda c: next(expired, None) or good(c)
    assert run("reply", {"comment_id": ALICE_REPLY, "text": "two"}, http)["replied"] is True
    assert len(http.to("createSession")) == 2


def test_chat_rate_limits_and_outages_keep_their_own_codes(http):
    http.routes[GET_CONVO] = reply(429, {"error": "RateLimitExceeded"}, headers={"retry-after": "30"})
    assert refused("reply", {"comment_id": ALICE_REPLY, "text": "hi"}, http, code="rate_limited").retry_after == 30
    http.routes[GET_CONVO] = reply(502, raw=b"bad gateway")
    refused("reply", {"comment_id": ALICE_REPLY, "text": "hi"}, http, code="unavailable")


def test_a_private_reply_to_our_own_post_is_refused(http):
    chat(http)
    refused("reply", {"comment_id": MINE, "text": "hi"}, http)
    assert not http.to("sendMessage")


def test_a_chat_message_over_1000_characters_is_refused_before_any_request(http):
    refused("reply", {"comment_id": ALICE_REPLY, "text": "x" * 1001}, http)
    assert not http.calls


def test_mentions_and_links_in_a_chat_message_become_facets(http):
    chat(http)
    http.routes[RESOLVE] = reply(body={"did": "did:plc:bob"})
    run("message", {"to": {"id": "did:plc:alice"}, "text": "cc @bob.bsky.social https://example.com"}, http)
    facets = http.to("sendMessage")[0]["json"]["message"]["facets"]
    assert [f["features"][0]["$type"].rsplit("#", 1)[1] for f in facets] == ["mention", "link"]


# ---- reply: dry runs and arguments --------------------------------------------------------------------------------------


def test_a_private_dry_run_sends_nothing_and_needs_no_sign_in(http):
    out = run("reply", {"comment_id": ALICE_REPLY, "text": "hello", "dry_run": True}, http)
    assert out["replied"] is False and out["dry_run"] is True and out["private"] is True and out["reply_id"] is None
    assert out["would_reply"]["to"] == "did:plc:alice" and out["would_reply"]["text"] == "hello" and out["would_reply"]["mode"] == "chat"
    assert not http.calls


def test_a_public_dry_run_shows_the_refs_and_creates_nothing(http):
    posts(http, post(ALICE_REPLY, "cid-parent", parent={"uri": MINE, "cid": "cid-mine"}, root=MY_ROOT_REF))
    out = run("reply", {"comment_id": ALICE_REPLY, "text": "hello", "private": False, "dry_run": True}, http)
    assert out["replied"] is False and out["private"] is False and out["url"] is None
    would = out["would_reply"]
    assert would["mode"] == "public" and would["record"]["reply"]["parent"] == {"uri": ALICE_REPLY, "cid": "cid-parent"}
    assert would["record"]["reply"]["root"]["uri"] == MINE
    assert not writes(http)


@pytest.mark.parametrize(
    "args",
    [
        {},
        {"text": "hi"},
        {"comment_id": ALICE_REPLY},
        {"comment_id": "3kmine", "text": "hi"},
        {"comment_id": "at://did:plc:x/app.bsky.feed.like/3k", "text": "hi"},
    ],
)
def test_reply_refuses_missing_or_malformed_arguments(args, http):
    refused("reply", args, http)
    assert not http.calls


# ---- like / repost ------------------------------------------------------------------------------------------------------


def test_like_creates_a_like_record_whose_subject_is_the_posts_uri_and_cid(http):
    posts(http, post(ALICE_REPLY, "cid-r"))
    assert run("like", {"comment_id": ALICE_REPLY}, http) == {"liked": True, "network": "bluesky"}
    sent = http.to("createRecord")[0]["json"]
    assert sent["repo"] == DID and sent["collection"] == "app.bsky.feed.like"
    assert sent["record"]["subject"] == {"uri": ALICE_REPLY, "cid": "cid-r"} and sent["record"]["$type"] == "app.bsky.feed.like"
    assert sent["record"]["createdAt"].endswith("Z")


def test_liking_something_already_liked_creates_nothing_more(http):
    posts(http, post(ALICE_REPLY, "cid-r", viewer={"like": f"at://{DID}/app.bsky.feed.like/3klike"}))
    assert run("like", {"comment_id": ALICE_REPLY}, http)["liked"] is True
    assert not http.to("createRecord")


def test_repost_creates_a_repost_record_and_returns_the_posts_link(http):
    posts(http, post(ALICE_ROOT, "cid-top"))
    out = run("repost", {"post_id": ALICE_ROOT}, http)
    assert out == {"reposted": True, "url": "https://bsky.app/profile/alice.example.com/post/3kaliceroot", "network": "bluesky"}
    sent = http.to("createRecord")[0]["json"]
    assert sent["collection"] == "app.bsky.feed.repost" and sent["record"]["subject"] == {"uri": ALICE_ROOT, "cid": "cid-top"}
    assert sent["record"]["$type"] == "app.bsky.feed.repost"


def test_repost_takes_the_record_key_of_one_of_our_own_posts(http):
    posts(http, post(MINE, "cid-mine", author={"did": DID, "handle": HANDLE}))
    out = run("repost", {"post_id": "3kmine"}, http)
    assert out["url"] == f"https://bsky.app/profile/{HANDLE}/post/3kmine"
    assert http.to("createRecord")[0]["json"]["record"]["subject"]["uri"] == MINE


def test_repost_takes_a_bsky_app_link_with_a_handle(http):
    http.routes[RESOLVE] = reply(body={"did": "did:plc:alice"})
    posts(http, post(ALICE_ROOT, "cid-top"))
    run("repost", {"post_id": "https://bsky.app/profile/alice.example.com/post/3kaliceroot"}, http)
    assert http.to("createRecord")[0]["json"]["record"]["subject"]["uri"] == ALICE_ROOT


def test_reposting_something_already_reposted_creates_nothing_more(http):
    posts(http, post(ALICE_ROOT, "cid-top", viewer={"repost": f"at://{DID}/app.bsky.feed.repost/3kr"}))
    assert run("repost", {"post_id": ALICE_ROOT}, http)["reposted"] is True
    assert not http.to("createRecord")


@pytest.mark.parametrize(("key", "args"), [("like", {}), ("like", {"comment_id": "nope"}), ("repost", {}), ("repost", {"post_id": "a/b"})])
def test_like_and_repost_refuse_a_missing_or_malformed_id(key, args, http):
    refused(key, args, http)
    assert not http.calls


def test_like_of_a_missing_post_is_unsupported(http):
    posts(http)
    refused("like", {"comment_id": ALICE_REPLY}, http)
    assert not http.to("createRecord")


# ---- hide (thread gate) -------------------------------------------------------------------------------------------------


def a_reply_in_our_thread(http, gate=None, gate_cid="cid-gate"):
    posts(http, post(ALICE_REPLY, "cid-r", parent={"uri": MINE, "cid": "cid-mine"}, root=MY_ROOT_REF))
    if gate is None:
        http.routes[GET_RECORD] = reply(400, {"error": "RecordNotFound", "message": "Could not locate record"})
    else:
        http.routes[GET_RECORD] = reply(body={"uri": f"at://{DID}/app.bsky.feed.threadgate/3kmine", "cid": gate_cid, "value": gate})


def test_hiding_a_reply_on_an_ungated_post_creates_a_gate_that_still_lets_everyone_reply(http):
    a_reply_in_our_thread(http)
    assert run("hide", {"comment_id": ALICE_REPLY}, http) == {"hidden": True, "network": "bluesky"}
    sent = http.to("createRecord")[0]["json"]
    assert sent["repo"] == DID and sent["collection"] == "app.bsky.feed.threadgate" and sent["rkey"] == "3kmine"
    record = sent["record"]
    assert record["$type"] == "app.bsky.feed.threadgate" and record["post"] == MINE and record["hiddenReplies"] == [ALICE_REPLY]
    assert "allow" not in record  # no allow list means anyone can reply: adding one would lock the thread
    asked = parse_qs(urlsplit(http.to("getRecord")[0]["url"]).query)
    assert asked == {"repo": [DID], "collection": ["app.bsky.feed.threadgate"], "rkey": ["3kmine"]}


def test_hiding_on_a_gated_post_keeps_the_allow_rules_and_other_hidden_replies(http):
    allow = [
        {"$type": "app.bsky.feed.threadgate#mentionRule"},
        {"$type": "app.bsky.feed.threadgate#listRule", "list": "at://did:plc:l/app.bsky.graph.list/3kl"},
    ]
    gate = {
        "$type": "app.bsky.feed.threadgate",
        "post": MINE,
        "allow": allow,
        "createdAt": "2026-01-01T00:00:00.000Z",
        "hiddenReplies": ["at://did:plc:z/app.bsky.feed.post/3kz"],
    }
    a_reply_in_our_thread(http, gate)
    run("hide", {"comment_id": ALICE_REPLY, "hidden": True}, http)
    assert not http.to("createRecord")
    sent = http.to("putRecord")[0]["json"]
    assert (sent["repo"], sent["collection"], sent["rkey"], sent["swapRecord"]) == (DID, "app.bsky.feed.threadgate", "3kmine", "cid-gate")
    assert sent["record"] == {**gate, "hiddenReplies": ["at://did:plc:z/app.bsky.feed.post/3kz", ALICE_REPLY]}
    assert sent["record"]["allow"] == allow


def test_unhiding_removes_only_that_reply_and_keeps_the_allow_rules(http):
    allow = [{"$type": "app.bsky.feed.threadgate#followingRule"}]
    other = "at://did:plc:z/app.bsky.feed.post/3kz"
    gate = {
        "$type": "app.bsky.feed.threadgate",
        "post": MINE,
        "allow": allow,
        "createdAt": "2026-01-01T00:00:00.000Z",
        "hiddenReplies": [other, ALICE_REPLY],
    }
    a_reply_in_our_thread(http, gate)
    assert run("hide", {"comment_id": ALICE_REPLY, "hidden": False}, http) == {"hidden": False, "network": "bluesky"}
    assert http.to("putRecord")[0]["json"]["record"] == {**gate, "hiddenReplies": [other]}


def test_unhiding_the_last_reply_drops_the_list_but_keeps_the_allow_rules(http):
    gate = {
        "$type": "app.bsky.feed.threadgate",
        "post": MINE,
        "allow": [{"$type": "app.bsky.feed.threadgate#mentionRule"}],
        "createdAt": "c",
        "hiddenReplies": [ALICE_REPLY],
    }
    a_reply_in_our_thread(http, gate)
    run("hide", {"comment_id": ALICE_REPLY, "hidden": "false"}, http)
    record = http.to("putRecord")[0]["json"]["record"]
    assert "hiddenReplies" not in record and record["allow"] == gate["allow"]


def test_hiding_what_is_already_hidden_and_unhiding_what_is_not_write_nothing(http):
    a_reply_in_our_thread(http, {"$type": "app.bsky.feed.threadgate", "post": MINE, "createdAt": "c", "hiddenReplies": [ALICE_REPLY]})
    assert run("hide", {"comment_id": ALICE_REPLY}, http)["hidden"] is True
    a_reply_in_our_thread(http)  # no gate at all
    assert run("hide", {"comment_id": ALICE_REPLY, "hidden": False}, http)["hidden"] is False
    assert not writes(http)


def test_hiding_in_a_thread_that_is_not_ours_is_unsupported_and_writes_nothing(http):
    posts(http, post(ALICE_REPLY, "cid-r", author=BOB, parent={"uri": ALICE_ROOT, "cid": "c"}, root={"uri": ALICE_ROOT, "cid": "c"}))
    err = refused("hide", {"comment_id": ALICE_REPLY}, http)
    assert "your own posts" in str(err)
    assert not writes(http) and not http.to("getRecord")


def test_hiding_a_top_level_post_is_unsupported(http):
    posts(http, post(ALICE_ROOT, "cid-top"))
    assert "isn't a reply" in str(refused("hide", {"comment_id": ALICE_ROOT}, http))


def test_a_thread_gate_that_cannot_be_read_surfaces_the_failure(http):
    a_reply_in_our_thread(http)
    http.routes[GET_RECORD] = reply(400, {"error": "InvalidRequest", "message": "something else"})
    refused("hide", {"comment_id": ALICE_REPLY}, http, code="invalid")
    assert not writes(http)


# ---- delete_comment -----------------------------------------------------------------------------------------------------


def test_delete_comment_deletes_one_of_our_own_posts(http):
    own = f"at://{DID}/app.bsky.feed.post/3kmyreply"
    assert run("delete_comment", {"comment_id": own}, http) == {"deleted": True, "network": "bluesky"}
    assert http.to("deleteRecord")[0]["json"] == {"repo": DID, "collection": "app.bsky.feed.post", "rkey": "3kmyreply"}


def test_delete_comment_refuses_someone_elses_post_as_unsupported_and_deletes_nothing(http):
    err = refused("delete_comment", {"comment_id": ALICE_REPLY}, http)
    assert "only your own" in str(err) and not http.to("deleteRecord")


@pytest.mark.parametrize("comment_id", ["", "3kmyreply", "at://did:plc:x/app.bsky.feed.like/3k", "not a uri"])
def test_delete_comment_needs_a_post_uri(comment_id, http):
    refused("delete_comment", {"comment_id": comment_id}, http)
    assert not http.calls


# ---- message ------------------------------------------------------------------------------------------------------------


def test_message_by_handle_resolves_the_did_then_chats(http):
    chat(http, convo="c-7", sent_id="m-7")
    http.routes[RESOLVE] = reply(body={"did": "did:plc:alice"})
    out = run("message", {"to": {"handle": "@Alice.Example.com"}, "text": "hello"}, http)
    assert out == {"sent": True, "dry_run": False, "message_id": "m-7", "network": "bluesky"}
    assert parse_qs(urlsplit(http.to("resolveHandle")[0]["url"]).query) == {"handle": ["alice.example.com"]}
    assert parse_qs(urlsplit(http.to("getConvoForMembers")[0]["url"]).query) == {"members": ["did:plc:alice"]}
    send = http.to("sendMessage")[0]
    assert send["json"] == {"convoId": "c-7", "message": {"text": "hello"}} and send["headers"]["atproto-proxy"] == CHAT_PROXY


def test_message_by_did_does_not_resolve_anything_and_prefers_it_over_a_handle(http):
    chat(http)
    run("message", {"to": {"id": "did:plc:bob", "handle": "alice.example.com"}, "text": "hi"}, http)
    assert not http.to("resolveHandle")
    assert parse_qs(urlsplit(http.to("getConvoForMembers")[0]["url"]).query) == {"members": ["did:plc:bob"]}


def test_message_to_a_handle_that_does_not_resolve_is_unsupported_and_sends_nothing(http):
    http.routes[RESOLVE] = reply(400, {"error": "InvalidRequest", "message": "Unable to resolve handle"})
    assert "alice.example.com" in str(refused("message", {"to": {"handle": "alice.example.com"}, "text": "hi"}, http))
    assert not http.to("sendMessage")


def test_message_dry_run_resolves_but_signs_in_and_sends_nothing(http):
    http.routes[RESOLVE] = reply(body={"did": "did:plc:alice"})
    out = run("message", {"to": {"handle": "alice.example.com"}, "text": "hi", "dry_run": True}, http)
    assert out["sent"] is False and out["dry_run"] is True and out["message_id"] is None
    assert out["would_send"]["to"] == {"id": "did:plc:alice", "handle": "alice.example.com"} and out["would_send"]["text"] == "hi"
    assert not http.to("createSession") and not writes(http)


def test_message_refusals_by_the_chat_service_are_unsupported(http):
    http.routes[GET_CONVO] = reply(401, {"error": "InvalidToken", "message": "Bad token scope"})
    assert "direct messages" in str(refused("message", {"to": {"id": "did:plc:alice"}, "text": "hi"}, http))


def test_message_to_ourselves_is_refused(http):
    chat(http)
    refused("message", {"to": {"id": DID}, "text": "hi"}, http)
    assert not http.to("sendMessage")


@pytest.mark.parametrize(
    "args",
    [
        {},
        {"to": "alice", "text": "hi"},
        {"to": {}, "text": "hi"},
        {"to": {"handle": "not a handle"}, "text": "hi"},
        {"to": {"id": "alice"}, "text": "hi"},
        {"to": {"id": "did:plc:alice"}},
        {"to": {"id": "did:plc:alice"}, "text": "x" * 1001},
    ],
)
def test_message_refuses_bad_arguments_before_any_request(args, http):
    refused("message", args, http)
    assert not http.calls


# ---- auth errors --------------------------------------------------------------------------------------------------------

ACTIONS = [
    ("comments", {}),
    ("reply", {"comment_id": ALICE_REPLY, "text": "hi"}),
    ("reply", {"comment_id": ALICE_REPLY, "text": "hi", "private": False}),
    ("like", {"comment_id": ALICE_REPLY}),
    ("repost", {"post_id": ALICE_ROOT}),
    ("hide", {"comment_id": ALICE_REPLY}),
    ("delete_comment", {"comment_id": ALICE_REPLY}),
    ("message", {"to": {"id": "did:plc:alice"}, "text": "hi"}),
]


@pytest.mark.parametrize(("key", "args"), ACTIONS)
def test_rejected_credentials_are_an_auth_error_and_nothing_is_sent(key, args, http):
    http.routes[SESSION] = BAD_LOGIN
    err = refused(key, args, http, code="auth")
    assert "Check the handle and app password" in str(err) and FAKE_PASSWORD not in str(err)
    assert not writes(http) and len(http.calls) == 1


@pytest.mark.parametrize(("key", "args"), ACTIONS)
def test_no_app_password_is_an_auth_error_before_any_request(key, args, http):
    with pytest.raises(IntegrationError) as caught:
        run(key, args, http, secret=None)
    assert caught.value.code == "auth" and not http.calls


def test_an_unreachable_bluesky_is_unavailable(http):
    http.routes[SESSION] = TimeoutError("timed out")
    refused("comments", {}, http, code="unavailable")


def test_a_rate_limited_notification_read_carries_the_retry_hint(http):
    http.routes[NOTIFICATIONS] = reply(429, {"error": "RateLimitExceeded"}, headers={"retry-after": "90"})
    assert refused("comments", {}, http, code="rate_limited").retry_after == 90


# ---- declarations -------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "capability", "gated"),
    [
        ("post", "social.post", True),
        ("comments", "social.comments", False),
        ("reply", "social.reply", True),
        ("like", "social.like", False),
        ("repost", "social.repost", True),
        ("hide", "social.hide", False),
        ("delete_comment", "social.delete_comment", True),
        ("message", "social.message", True),
    ],
)
def test_each_action_declares_its_capability_and_approval(key, capability, gated):
    action = P.get_action(key)
    assert action.capability == capability and action.requires_approval is gated


def test_capabilities_are_unique_and_delete_post_is_unchanged():
    caps = [a.capability for a in P.actions if a.capability]
    assert len(caps) == len(set(caps)) == 8
    delete = P.get_action("delete_post")
    assert delete.capability is None and delete.requires_approval is True and set(delete.input_schema["properties"]) == {"rkey", "dry_run"}


def test_the_input_schemas_follow_the_contract():
    props = {k: set(P.get_action(k).input_schema["properties"]) for k in ("comments", "reply", "like", "repost", "hide", "delete_comment", "message")}
    assert props == {
        "comments": {"since", "limit", "kinds"},
        "reply": {"comment_id", "text", "private", "dry_run"},
        "like": {"comment_id"},
        "repost": {"post_id"},
        "hide": {"comment_id", "hidden"},
        "delete_comment": {"comment_id"},
        "message": {"to", "text", "dry_run"},
    }
    assert P.get_action("reply").input_schema["properties"]["private"]["default"] is True
    assert P.get_action("hide").input_schema["properties"]["hidden"]["default"] is True
    assert P.get_action("comments").input_schema["properties"]["limit"]["default"] == 50
    assert set(P.get_action("comments").output_schema["required"]) == {"items", "cursor"}


def test_an_unknown_action_is_not_implemented(http):
    with pytest.raises(NotImplementedError):
        run("nope", {}, http)


# ── direct messages (0.4.0): chat read with the notifications when `message` is asked for ──────────────────────────

CONVOS = f"{SERVICE}/xrpc/chat.bsky.convo.listConvos"
MESSAGES = f"{SERVICE}/xrpc/chat.bsky.convo.getMessages"


def chats(http, messages, *, last=None):
    """One conversation with Alice; ``messages`` newest first as getMessages returns them."""
    convo = {"id": "convo1", "members": [{"did": DID, "handle": HANDLE}, {"did": ALICE["did"], "handle": ALICE["handle"], "displayName": "Alice A"}],
             "lastMessage": {"sentAt": last or (messages[0]["sentAt"] if messages else at(0))}}  # fmt: skip
    http.routes[CONVOS] = reply(body={"convos": [convo]})
    http.routes[MESSAGES] = reply(body={"messages": messages})


def dm(n, *, sender=None, text=None):
    return {"id": f"msg{n}", "text": text or f"dm {n}", "sender": {"did": sender or ALICE["did"]}, "sentAt": at(n)}


def test_messages_come_with_the_notifications_by_time_and_skip_the_accounts_own(http):
    feed(http, [note(3, text="great post", parent=MINE)])
    chats(http, [dm(5, text="Hello there"), dm(4, sender=DID, text="our answer"), dm(2, text="Hi")])

    out = run("comments", {"kinds": ["comment", "reply", "mention", "message"]}, http)

    assert [(i["kind"], i["text"]) for i in out["items"]] == [("message", "Hi"), ("reply", "great post"), ("message", "Hello there")]
    message = out["items"][-1]
    assert message["id"] == "convo1/msg5" and message["author"] == {"id": ALICE["did"], "handle": ALICE["handle"], "name": "Alice A"}
    assert all(c["headers"].get("atproto-proxy") for c in http.calls if "chat.bsky" in c["url"])


def test_a_cursor_hands_over_only_newer_messages(http):
    feed(http, [])
    chats(http, [dm(5), dm(2)])
    first = run("comments", {"kinds": ["message"]}, http)
    chats(http, [dm(7, text="later"), dm(5), dm(2)])

    again = run("comments", {"kinds": ["message"], "since": first["cursor"]}, http)

    assert [i["text"] for i in again["items"]] == ["later"]


def test_without_message_asked_for_no_chat_is_read(http):
    feed(http, [note(3)])

    run("comments", {"kinds": ["reply"]}, http)

    assert http.to("listConvos") == []


def test_an_app_password_without_chat_access_still_reads_the_comments(http):
    feed(http, [note(3, text="great post", parent=MINE)])
    http.routes[CONVOS] = reply(401, {"error": "AuthMissing", "message": "Bad token scope"})

    out = run("comments", {"kinds": ["reply", "message"]}, http)

    assert [i["kind"] for i in out["items"]] == ["reply"]
