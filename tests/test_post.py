"""The `social.post` action: text, facets, the 300-grapheme limit, images and alt text, the link card, dry run."""

import pytest
from conftest import DID, FAKE_PASSWORD, HANDLE, JPEG, PIC, PIC2, SERVICE, make_ctx, reply
from marvin_integration_sdk import IntegrationError

from marvin_integration_bluesky import BlueskyProvider

P = BlueskyProvider()


def post(ctx, **args):
    return P.run_action("post", args, ctx)


def record_of(http):
    (call,) = http.to("createRecord")
    return call["json"]["record"]


# ---- text ---------------------------------------------------------------------------------


def test_text_post_signs_in_then_creates_the_record(http, ctx):
    out = post(ctx, text="Hello Bluesky")

    assert out == {
        "posted": True,
        "dry_run": False,
        "post_id": "3krkey",
        "url": f"https://bsky.app/profile/{HANDLE}/post/3krkey",
        "network": "bluesky",
        "uri": f"at://{DID}/app.bsky.feed.post/3krkey",
        "cid": "bafycid",
        "images": 0,
        "notes": [],
    }
    session, create = http.calls
    assert session["url"] == f"{SERVICE}/xrpc/com.atproto.server.createSession"
    assert session["json"] == {"identifier": HANDLE, "password": FAKE_PASSWORD}
    assert create["url"] == f"{SERVICE}/xrpc/com.atproto.repo.createRecord"
    assert create["headers"]["Authorization"] == "Bearer jwt-1"
    body = create["json"]
    assert (body["repo"], body["collection"]) == (DID, "app.bsky.feed.post")
    rec = body["record"]
    assert rec["$type"] == "app.bsky.feed.post"
    assert rec["text"] == "Hello Bluesky"
    assert rec["createdAt"].endswith("Z") and "T" in rec["createdAt"]
    assert "facets" not in rec and "embed" not in rec
    assert not http.to("uploadBlob")


def test_each_call_signs_in_afresh_and_keeps_no_token(http, ctx):
    post(ctx, text="one")
    post(ctx, text="two")
    assert len(http.to("createSession")) == 2


def test_handle_with_at_sign_and_capitals_is_normalised_and_custom_service_used(http):
    custom = "https://pds.example.test"
    http.routes[f"{custom}/xrpc/com.atproto.server.createSession"] = lambda c: reply(body={"accessJwt": "j", "did": DID})
    http.routes[f"{custom}/xrpc/com.atproto.repo.createRecord"] = lambda c: reply(body={"uri": f"at://{DID}/app.bsky.feed.post/abc"})
    out = post(make_ctx(http, config={"handle": "@Name.Example.Test", "service": custom + "/"}), text="hi")
    assert http.to("createSession")[0]["json"]["identifier"] == "name.example.test"
    assert out["url"] == "https://bsky.app/profile/name.example.test/post/abc"  # no handle in the session reply: the configured one


def test_the_service_must_be_https(http):
    with pytest.raises(IntegrationError) as e:
        post(make_ctx(http, config={"handle": HANDLE, "service": "http://bsky.social"}), text="hi")
    assert "https" in str(e.value)
    assert not http.calls  # the app password never left


@pytest.mark.parametrize("text", ["", "   ", None])
def test_text_is_required(http, ctx, text):
    with pytest.raises(IntegrationError) as e:
        post(ctx, text=text)
    assert e.value.code == "unsupported"
    assert not http.calls


# ---- facets -------------------------------------------------------------------------------


def test_links_become_facets_with_byte_offsets_after_emoji_and_multibyte_text(http, ctx):
    text = "Café ☕ 日本語 👨‍👩‍👧‍👦 read https://example.com/ünï?q=1, now"
    post(ctx, text=text)
    rec = record_of(http)
    (facet,) = rec["facets"]
    raw = text.encode("utf-8")
    assert raw[facet["index"]["byteStart"] : facet["index"]["byteEnd"]].decode() == "https://example.com/ünï?q=1"
    assert facet["features"][0] == {"$type": "app.bsky.richtext.facet#link", "uri": "https://example.com/ünï?q=1"}
    assert len(raw) > len(text)  # the offsets are not character positions


def test_mention_is_resolved_into_a_facet(http, ctx):
    http.routes["com.atproto.identity.resolveHandle?handle=alice.bsky.social"] = reply(body={"did": "did:plc:alice"})
    out = post(ctx, text="cc @alice.bsky.social")
    (facet,) = record_of(http)["facets"]
    assert facet["features"] == [{"$type": "app.bsky.richtext.facet#mention", "did": "did:plc:alice"}]
    assert out["notes"] == []


def test_unresolvable_mention_is_left_as_text_with_a_note(http, ctx):
    out = post(ctx, text="cc @nobody.bsky.social")  # the fake answers 404
    assert "facets" not in record_of(http)
    assert "@nobody.bsky.social" in out["notes"][0]


# ---- the 300-character limit --------------------------------------------------------------


def test_exactly_300_graphemes_is_allowed_even_when_code_points_exceed_it(http, ctx):
    text = "é" * 150 + "e\u0301" * 150  # 450 code points, 300 graphemes
    post(ctx, text=text)
    assert record_of(http)["text"] == text


def test_bluesky_also_caps_the_text_at_3000_bytes(http, ctx):
    with pytest.raises(IntegrationError):
        post(ctx, text="👨‍👩‍👧‍👦" * 300)  # 300 graphemes, but 7,200 bytes
    assert not http.calls


def test_301_graphemes_is_refused_with_the_per_network_hint(http, ctx):
    with pytest.raises(IntegrationError) as e:
        post(ctx, text="a" * 301)
    assert e.value.code == "unsupported"
    assert "Bluesky allows 300 characters; use per_network with a shorter text" in str(e.value)
    assert "301" in str(e.value)
    assert not http.calls  # refused before signing in


def test_emoji_clusters_count_once_toward_the_limit(http, ctx):
    post(ctx, text="🇺🇸" * 280 + "a" * 20)  # 300 graphemes, 600 code points of flags
    assert len(http.to("createRecord")) == 1


def test_the_appended_link_counts_toward_the_limit(http, ctx):
    with pytest.raises(IntegrationError) as e:
        post(ctx, text="a" * 290, link="https://example.com/some/long/path")
    assert "300 characters" in str(e.value)


# ---- images and alt text ------------------------------------------------------------------


def test_images_are_downloaded_uploaded_and_embedded_with_alt_text(http, ctx):
    out = post(ctx, text="Look", images=[PIC, PIC2], alt=["A hero", "Second"])

    assert out["images"] == 2
    assert [c["url"] for c in http.calls[:2]] == [PIC, PIC2]  # downloaded before signing in
    uploads = http.to("uploadBlob")
    assert [u["headers"]["Content-Type"] for u in uploads] == ["image/jpeg", "image/png"]
    assert uploads[0]["data"] == JPEG and uploads[0]["headers"]["Authorization"] == "Bearer jwt-1"
    embed = record_of(http)["embed"]
    assert embed["$type"] == "app.bsky.embed.images"
    assert [i["alt"] for i in embed["images"]] == ["A hero", "Second"]
    assert [i["image"]["ref"]["$link"] for i in embed["images"]] == ["bafyblob1", "bafyblob2"]
    assert all(i["image"]["$type"] == "blob" for i in embed["images"])


def test_missing_alt_text_is_an_empty_string_for_the_rest(http, ctx):
    post(ctx, text="Look", images=[PIC, PIC2], alt=["Only the first"])
    assert [i["alt"] for i in record_of(http)["embed"]["images"]] == ["Only the first", ""]


def test_the_type_comes_from_the_bytes_not_a_wrong_header(http, ctx):
    http.routes[PIC] = reply(headers={"Content-Type": "application/octet-stream"}, raw=JPEG)
    post(ctx, text="Look", images=[PIC])
    assert http.to("uploadBlob")[0]["headers"]["Content-Type"] == "image/jpeg"


def test_four_images_are_allowed(http, ctx):
    post(ctx, text="Look", images=[PIC, PIC2, PIC, PIC2])
    assert len(record_of(http)["embed"]["images"]) == 4


def test_five_images_are_refused_before_anything_is_fetched(http, ctx):
    with pytest.raises(IntegrationError) as e:
        post(ctx, text="Look", images=[PIC] * 5)
    assert e.value.code == "unsupported"
    assert "4 images" in str(e.value) and "per_network" in str(e.value)
    assert not http.calls


def test_more_alt_texts_than_images_is_refused(http, ctx):
    with pytest.raises(IntegrationError) as e:
        post(ctx, text="Look", images=[PIC], alt=["a", "b"])
    assert "one text per image" in str(e.value)


def test_an_image_over_one_megabyte_is_refused_and_nothing_is_posted(http, ctx):
    http.routes[PIC2] = reply(headers={"Content-Type": "image/jpeg"}, raw=JPEG + b"\x00" * 1_000_000)
    with pytest.raises(IntegrationError) as e:
        post(ctx, text="Look", images=[PIC, PIC2])
    assert e.value.code == "unsupported"
    assert "Image 2" in str(e.value) and "1 MB" in str(e.value)
    assert not http.to("uploadBlob") and not http.to("createSession") and not http.to("createRecord")


def test_a_download_that_is_not_a_picture_is_refused(http, ctx):
    http.routes[PIC] = reply(headers={"Content-Type": "text/html"}, raw=b"<html>nope</html>")
    with pytest.raises(IntegrationError) as e:
        post(ctx, text="Look", images=[PIC])
    assert "isn't a picture" in str(e.value)


def test_a_failed_download_is_a_clear_error(http, ctx):
    http.routes[PIC] = reply(404, {"error": "gone"})
    with pytest.raises(IntegrationError) as e:
        post(ctx, text="Look", images=[PIC])
    assert "Image 1 could not be downloaded (HTTP 404)" in str(e.value)


def test_image_urls_must_be_https(http, ctx):
    with pytest.raises(IntegrationError):
        post(ctx, text="Look", images=["http://assets.example.test/a.jpg"])
    assert not http.calls


# ---- link and link card -------------------------------------------------------------------

PAGE = (
    b'<html><head><title>Fallback title</title><meta property="og:title" content="The &amp; Title">'
    b"<meta name='description' content='plain desc'><meta property='og:description' content='About this page'></head></html>"
)


def test_a_link_with_no_images_adds_a_card_and_appends_the_link(http, ctx):
    http.routes["https://example.com/post"] = reply(headers={"Content-Type": "text/html; charset=utf-8"}, raw=PAGE)
    post(ctx, text="Worth a read", link="https://example.com/post")
    rec = record_of(http)
    assert rec["text"] == "Worth a read\n\nhttps://example.com/post"
    assert rec["embed"] == {
        "$type": "app.bsky.embed.external",
        "external": {"uri": "https://example.com/post", "title": "The & Title", "description": "About this page"},
    }
    assert rec["facets"][0]["features"][0]["uri"] == "https://example.com/post"  # the appended link is clickable too


def test_a_link_already_in_the_text_is_not_appended(http, ctx):
    post(ctx, text="Read https://example.com/post now", link="https://example.com/post")
    assert record_of(http)["text"] == "Read https://example.com/post now"


def test_the_card_title_falls_back_to_the_page_title_then_the_link(http, ctx):
    http.routes["https://example.com/a"] = reply(headers={"Content-Type": "text/html"}, raw=b"<title>\n Plain   title </title>")
    post(ctx, text="x", link="https://example.com/a")
    assert record_of(http)["embed"]["external"] == {"uri": "https://example.com/a", "title": "Plain title", "description": ""}


def test_an_unreadable_page_still_gets_a_card_titled_with_the_link(http, ctx):
    http.routes["https://example.com/down"] = ConnectionError("blocked")
    post(ctx, text="x", link="https://example.com/down")
    assert record_of(http)["embed"]["external"] == {"uri": "https://example.com/down", "title": "https://example.com/down", "description": ""}


def test_a_link_with_images_goes_in_the_text_only(http, ctx):
    out = post(ctx, text="Look", images=[PIC], link="https://example.com/post")
    rec = record_of(http)
    assert rec["embed"]["$type"] == "app.bsky.embed.images"
    assert rec["text"].endswith("https://example.com/post")
    assert any("link card" in n for n in out["notes"])
    assert not [c for c in http.calls if c["url"] == "https://example.com/post"]


def test_the_link_must_be_http(http, ctx):
    with pytest.raises(IntegrationError):
        post(ctx, text="x", link="javascript:alert(1)")


# ---- dry run ------------------------------------------------------------------------------


def test_dry_run_builds_the_full_record_without_signing_in_or_posting(http, ctx):
    http.routes["https://example.com/post"] = reply(headers={"Content-Type": "text/html"}, raw=PAGE)
    out = post(ctx, text="Hi 👋 日本 see", link="https://example.com/post", dry_run=True)

    assert out["posted"] is False and out["dry_run"] is True
    assert out["post_id"] is None and out["url"] is None and out["network"] == "bluesky"
    would = out["would_post"]
    assert would["repo"] == HANDLE and would["collection"] == "app.bsky.feed.post"
    rec = would["record"]
    assert rec["text"] == "Hi 👋 日本 see\n\nhttps://example.com/post"
    assert rec["facets"][0]["features"][0]["uri"] == "https://example.com/post"
    assert rec["embed"]["external"]["title"] == "The & Title"
    assert would["characters"] == len("Hi 👋 日本 see\n\nhttps://example.com/post")
    assert not http.to("createSession") and not http.to("createRecord") and not http.to("uploadBlob")


def test_dry_run_with_images_plans_the_embed_without_downloading_or_uploading(http, ctx):
    out = post(ctx, text="Look", images=[PIC, PIC2], alt=["A", "B"], dry_run=True)
    would = out["would_post"]
    assert would["images"] == [{"url": PIC, "alt": "A"}, {"url": PIC2, "alt": "B"}]
    assert [i["alt"] for i in would["record"]["embed"]["images"]] == ["A", "B"]
    assert not http.calls


def test_dry_run_still_refuses_what_a_real_post_would(http, ctx):
    with pytest.raises(IntegrationError):
        post(ctx, text="a" * 301, dry_run=True)
    with pytest.raises(IntegrationError):
        post(ctx, text="x", images=[PIC] * 5, dry_run=True)


def test_dry_run_works_without_a_secret(http):
    out = post(make_ctx(http, secret=None), text="hi", dry_run=True)
    assert out["would_post"]["record"]["text"] == "hi"
