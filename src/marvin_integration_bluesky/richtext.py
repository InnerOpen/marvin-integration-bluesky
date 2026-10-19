"""Text handling for Bluesky posts: grapheme counting and rich-text facets.

Bluesky limits a post to 300 *graphemes* (what a person sees as one character: a flag, a family emoji and an "e" plus an
accent are one each), and addresses a link or mention inside the text by **UTF-8 byte offsets**, not character positions.
Both are easy to get wrong with Python's code-point indexing, so they live here, small and tested.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable

try:  # the `regex` package implements UAX #29 extended grapheme clusters (\X)
    import regex as _regex
except ImportError:  # pragma: no cover - exercised through grapheme_count_fallback in tests
    _regex = None

LINK_FEATURE = "app.bsky.richtext.facet#link"
MENTION_FEATURE = "app.bsky.richtext.facet#mention"

_ZWJ = 0x200D
_EMOJI_MODIFIERS = range(0x1F3FB, 0x1F400)
_REGIONAL_INDICATORS = range(0x1F1E6, 0x1F200)
_TAGS = range(0xE0020, 0xE0080)
_VARIATION_SELECTORS = (range(0xFE00, 0xFE10), range(0xE0100, 0xE01F0))


def _extends(prev: str, ch: str, ri_run: int) -> bool:
    """Whether ``ch`` continues the grapheme cluster that ``prev`` ends (``ri_run``: regional indicators just before ``ch``)."""
    cp, pcp = ord(ch), ord(prev)
    if pcp == 0x0D:
        return cp == 0x0A
    if pcp == _ZWJ:  # the next emoji in a ZWJ sequence
        return unicodedata.category(ch) not in ("Cc", "Zs")
    if cp == _ZWJ or cp in _EMOJI_MODIFIERS or cp in _TAGS or any(cp in r for r in _VARIATION_SELECTORS):
        return True
    if unicodedata.category(ch) in ("Mn", "Mc", "Me"):  # accents, spacing marks, enclosing keycaps
        return True
    return cp in _REGIONAL_INDICATORS and pcp in _REGIONAL_INDICATORS and ri_run % 2 == 1  # the second half of a flag


def grapheme_count_fallback(text: str) -> int:
    """A careful stand-in for ``regex``'s ``\\X``: base character plus combining marks, variation selectors, emoji skin tones,
    ZWJ sequences, tag sequences, keycaps, regional-indicator flag pairs and CRLF. Not full UAX #29 (no Hangul jamo rules)."""
    count, prev, ri_run = 0, None, 0
    for ch in text:
        if prev is None or not _extends(prev, ch, ri_run):
            count += 1
        ri_run = ri_run + 1 if ord(ch) in _REGIONAL_INDICATORS else 0
        prev = ch
    return count


def grapheme_count(text: str) -> int:
    """How many characters Bluesky counts in ``text``."""
    if _regex is not None:
        return len(_regex.findall(r"\X", text))
    return grapheme_count_fallback(text)


_URL = re.compile(r"(?<![\w@])https?://[^\s<>]+", re.IGNORECASE)
_CLOSERS = {")": "(", "]": "[", "}": "{"}
_TRAILING = ".,;:!?'\"…"
_MENTION = re.compile(r"(?<![\w@/.:\-])@((?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)")


def _trim_url(url: str) -> str:
    """Drop the sentence punctuation a URL picks up at the end of a sentence, and a closing bracket that has no opener in it."""
    while url:
        last = url[-1]
        if last in _TRAILING or (last in _CLOSERS and url.count(last) > url.count(_CLOSERS[last])):
            url = url[:-1]
        else:
            break
    return url


def find_links(text: str) -> list[tuple[int, int, str]]:
    """``(start, end, url)`` in character offsets for each http(s) link in ``text`` that has a host with a dot."""
    found = []
    for m in _URL.finditer(text):
        url = _trim_url(m.group(0))
        host = re.sub(r"^https?://", "", url, flags=re.IGNORECASE).split("/", 1)[0].split("?", 1)[0].split("#", 1)[0].rsplit("@", 1)[-1]
        if "." in host.split(":", 1)[0].strip("."):
            found.append((m.start(), m.start() + len(url), url))
    return found


def find_mentions(text: str, taken: list[tuple[int, int, str]] | None = None) -> list[tuple[int, int, str]]:
    """``(start, end, handle)`` for each ``@handle.example`` in ``text``, skipping anything inside a link (``https://x.com/@a.b``)."""
    spans = [(s, e) for s, e, _ in (taken or [])]
    return [(m.start(), m.end(), m.group(1).lower()) for m in _MENTION.finditer(text) if not any(s <= m.start() < e for s, e in spans)]


def build_facets(text: str, resolve_handle: Callable[[str], str | None] | None = None) -> tuple[list[dict], list[str]]:
    """The ``app.bsky.richtext.facet`` list for ``text`` and notes about what was skipped.

    Links become ``#link`` facets. Mentions become ``#mention`` facets only when ``resolve_handle`` maps the handle to a DID;
    otherwise the text stays as typed and a note says so. Offsets are UTF-8 byte offsets, as the lexicon requires.
    """
    links = find_links(text)
    facets: list[tuple[int, dict]] = []
    notes: list[str] = []

    def span(start: int, end: int) -> dict:
        return {"byteStart": len(text[:start].encode("utf-8")), "byteEnd": len(text[:end].encode("utf-8"))}

    for start, end, url in links:
        facets.append((start, {"index": span(start, end), "features": [{"$type": LINK_FEATURE, "uri": url}]}))
    for start, end, handle in find_mentions(text, links):
        did = resolve_handle(handle) if resolve_handle else None
        if did:
            facets.append((start, {"index": span(start, end), "features": [{"$type": MENTION_FEATURE, "did": did}]}))
        else:
            notes.append(f"@{handle} was left as plain text: the handle could not be resolved to an account.")
    return [f for _, f in sorted(facets, key=lambda p: p[0])], notes
