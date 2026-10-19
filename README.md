# marvin-integration-bluesky

A [Marvin](https://github.com/InnerOpen/marvin) integration that posts to **Bluesky**. It provides Marvin's shared
**`social.post`** capability: a workflow says "post this", and each connected network plugin posts it. It talks to the
AT Protocol directly (your own account and an app password), with no third-party aggregator.
Category: *destination*. Version 0.1.0.

## Install

On a Marvin host:

```bash
uv pip install marvin-integration-bluesky   # or add to your Marvin image
# restart Marvin → "Bluesky" appears in Settings → Integrations
```

No changes to Marvin core or its frontend: Marvin discovers the provider through the `marvin.integrations` entry point.

## Set it up

1. In Bluesky: **Settings → Privacy and security → App passwords → Add app password**. Name it "Marvin" and copy the
   password (`xxxx-xxxx-xxxx-xxxx`). Use an app password, never your account password; you can revoke it there any time.
2. In Marvin: **Settings → Integrations → Bluesky → Configure**. Set **Handle** (e.g. `name.bsky.social`, or your custom
   domain handle) and paste the app password into **App password**. Leave **Service** at `https://bsky.social` unless you
   run your own PDS. Save: the card should turn `ok`.
3. Put an **Approval** step before any `social.post` step. Every social post needs a person's approval first; Marvin core
   only runs it after that step.

## Credential, config and actions

| | |
|---|---|
| **Credential** | `app_password`: the Bluesky app password (stored by Marvin as the integration's secret; never persisted by the plugin) |
| **Config** | `handle` (required), `service` (default `https://bsky.social`, must be https) |
| **Health** | `com.atproto.server.createSession` succeeds |
| **Action** | `post`: capability `social.post`, approval-gated (below) |
| **Action** | `delete_post`: `{ "rkey", "dry_run"? }` → `{ "deleted", "rkey", "network" }`. Approval-gated; the undo for a mistake. `rkey` is a post's `post_id`; a bsky.app or `at://` link to the post also works |

### `post`: the `social.post` contract

| Argument | Meaning on Bluesky |
|---|---|
| `text` (required) | The post text, at most **300 graphemes** (and 3,000 bytes). Links become clickable facets; `@handle.tld` mentions become mention facets when the handle resolves |
| `images` | 0 to **4** public https URLs (more is an error). Each is downloaded through `ctx.http`, at most **1 MB**, uploaded with `uploadBlob` and embedded as `app.bsky.embed.images` |
| `alt` | One alt text per image, in order (fewer is fine, more is an error) |
| `link` | Appended to the text (`\n\n<link>`) unless the text already contains it. With no images it also becomes an `app.bsky.embed.external` card (title and description from the page's Open Graph tags or `<title>`; the link itself is the title when the page can't be read). Bluesky can't show a card beside pictures, so with images the link is in the text only |
| `dry_run` (default **false**) | Builds the record and returns it as `would_post`, without signing in, downloading or uploading images, or calling `createRecord`. (It may read the linked page and resolve mentioned handles: both are plain reads) |

Output: `posted`, `post_id` (the record key, rkey), `url` (`https://bsky.app/profile/<handle>/post/<rkey>`), `network` =
`"bluesky"`, plus `uri` (the `at://` URI), `cid`, `images` and `notes` (for example a mention left as plain text). A dry run
returns `posted: false`, `dry_run: true`, `post_id`/`url` null, and `would_post` = `{ repo, collection, record, images, characters, notes }`.

What Bluesky can't do is refused with an `IntegrationError` (code `unsupported`) and a plain message, before anything is
posted: text over 300 characters ("Bluesky allows 300 characters; use per_network with a shorter text"), more than 4
images, an image over 1 MB or not a picture, or a non-https image URL. Failures from Bluesky carry the codes `auth`
(rejected handle or app password: Marvin notifies), `rate_limited` (Marvin retries after 1, 5 and 15 minutes; a rate-limited
request was refused outright, so it cannot double-post), `unavailable` and `invalid`.

Each action signs in afresh (`createSession`) and keeps no token. Images are not resized in v0.1: shrink them first.

## Not in v0.1

Hashtag facets, link-card thumbnails, image aspect ratios, video, replies, quotes and threads, and image resizing.

## Verify (AT Protocol details written from memory of the lexicons, not checked against a live server)

Nothing here was run against bsky.social: the tests use a fake HTTP client. Before the first real post, confirm:

- `createSession` takes `{identifier, password}` and returns `accessJwt`, `did` and `handle`.
- `createRecord` takes `{repo (the DID), collection, record}` and returns `{uri, cid}`; the rkey is the last segment of `uri`.
- `uploadBlob` takes the raw bytes with the image's `Content-Type` and returns `{blob: {...}}`, which goes into the embed unchanged.
- `deleteRecord` takes `{repo, collection, rkey}`.
- The 300-grapheme and 3,000-byte text limits, the 4-image limit and the **~1,000,000 byte** image blob limit (the PDS may reject
  at a slightly different size). The alt text limit (if any) is not checked here.
- `app.bsky.embed.external` needs `uri`, `title` and `description` (a `thumb` is optional and skipped).
- Facet `byteStart`/`byteEnd` are UTF-8 byte offsets, end exclusive; feature types are `app.bsky.richtext.facet#link` and `#mention`.
- Mention resolution uses the unauthenticated `com.atproto.identity.resolveHandle?handle=` on the configured service. If that
  route needs auth or a different host (e.g. the public AppView), mentions silently stay plain text, with a note.
- WebP and GIF uploads are accepted by this plugin (typed by their bytes); Bluesky may handle them differently from JPEG/PNG.
- The grapheme counter uses the `regex` package's `\X`; without it, a built-in fallback is used that handles emoji, flags,
  skin tones, ZWJ sequences and combining marks but not every script (Devanagari conjuncts count high).
- The permalink uses the account's handle as returned by `createSession` (falling back to the configured one); bsky.app also
  accepts the DID there.
- Account takedown or an expired app password both surface as `auth`.

## Develop

```bash
uv run --extra dev pytest -q
uv run --extra dev ruff check .
```

Tests use a fake `ctx.http` and a made-up password: no network, no credentials. The SDK resolves from the sibling
`../MarvinIntegrationSDK` checkout (see `[tool.uv.sources]`); released builds take it from the index.

The plugin ships no logo yet: it shows the 🦋 emoji. To add Bluesky's official mark, put it beside `provider.py` as
`logo.svg` (at most 64 KB) and set `logo = "logo.svg"` on the provider.
