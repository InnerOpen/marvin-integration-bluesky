"""Grapheme counting and facets: the two places where code points, graphemes and UTF-8 bytes disagree."""

import pytest

from marvin_integration_bluesky.richtext import build_facets, find_links, find_mentions, grapheme_count, grapheme_count_fallback

FAMILY = "👨‍👩‍👧‍👦"  # 7 code points, one grapheme
CASES = [
    ("abc", 3),
    ("é", 1),  # e + combining acute
    (FAMILY, 1),
    ("🇺🇸", 1),
    ("🇺🇸🇫🇷", 2),  # two flags, not one four-code-point blob
    ("👍🏽", 1),  # skin tone
    ("1️⃣", 1),  # keycap
    ("🏳️‍🌈", 1),
    ("❤️", 1),  # variation selector
    ("日本語", 3),
    ("\r\n", 1),
    ("", 0),
]


@pytest.mark.parametrize(("text", "expected"), CASES)
def test_grapheme_count(text, expected):
    assert grapheme_count(text) == expected


@pytest.mark.parametrize(("text", "expected"), CASES)
def test_fallback_counts_the_same_without_the_regex_package(text, expected):
    assert grapheme_count_fallback(text) == expected


def _slice(text: str, facet: dict) -> str:
    idx = facet["index"]
    return text.encode("utf-8")[idx["byteStart"] : idx["byteEnd"]].decode("utf-8")


def test_link_offsets_are_utf8_bytes_after_emoji_and_accents():
    text = "héllo 👋 日本 see https://example.com/path?x=1 ok"
    facets, notes = build_facets(text)
    assert notes == []
    assert len(facets) == 1
    assert _slice(text, facets[0]) == "https://example.com/path?x=1"
    # a code-point index would land somewhere else entirely
    assert facets[0]["index"]["byteStart"] != text.index("https://")
    assert facets[0]["features"] == [{"$type": "app.bsky.richtext.facet#link", "uri": "https://example.com/path?x=1"}]


def test_link_loses_sentence_punctuation_but_keeps_balanced_brackets():
    text = "Read https://example.com/a_(b). Then (https://example.org/x), and https://example.net!"
    assert [u for _, _, u in find_links(text)] == ["https://example.com/a_(b)", "https://example.org/x", "https://example.net"]


def test_not_a_link_without_a_host_with_a_dot():
    assert find_links("see https://localhost and https:// nothing") == []


def test_several_facets_in_order_with_a_resolved_mention():
    text = "👋 @alice.bsky.social meet https://a.example/x and https://b.example/y"
    facets, notes = build_facets(text, lambda h: "did:plc:alice" if h == "alice.bsky.social" else None)
    assert notes == []
    assert [_slice(text, f) for f in facets] == ["@alice.bsky.social", "https://a.example/x", "https://b.example/y"]
    assert facets[0]["features"] == [{"$type": "app.bsky.richtext.facet#mention", "did": "did:plc:alice"}]


def test_unresolved_mention_is_skipped_with_a_note():
    facets, notes = build_facets("hi @ghost.bsky.social", lambda h: None)
    assert facets == []
    assert notes and "@ghost.bsky.social" in notes[0]


def test_mentions_are_not_found_in_emails_or_links():
    text = "mail me a@b.example or visit https://x.example/@a.b.c"
    assert find_mentions(text, find_links(text)) == []


def test_no_resolver_means_no_mention_facet_and_plain_text_has_no_facets():
    assert build_facets("hello @a.bsky.social")[0] == []
    assert build_facets("just words")[0] == []
