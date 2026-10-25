# marvin-integration-bluesky

A [Marvin](https://github.com/InnerOpen/marvin) integration that posts to **Bluesky**. It provides Marvin's shared
**`social.post`** capability: a workflow says "post this", and each connected network plugin posts it. It talks to the
AT Protocol directly (your own account and an app password), with no third-party aggregator.
Since 0.2.0 it also takes part in Marvin's shared **social engagement** capabilities (read replies and mentions, reply
publicly or by chat, like, repost, hide, delete, message). Category: *destination*. Version 0.2.0.

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
4. **For private replies and messages** (Bluesky chat), make the app password with **Allow access to your direct
   messages** ticked (the checkbox on the "Add app password" screen). An app password made without it still posts, reads
   notifications, likes and reposts; only the chat actions fail, with code `unsupported` and a message saying so. Reading
   replies and mentions (notifications) needs no extra permission. The person you message must also accept messages from
   your account (their Bluesky chat setting).

## Credential, config and actions

| | |
|---|---|
| **Credential** | `app_password`: the Bluesky app password (stored by Marvin as the integration's secret; never persisted by the plugin) |
| **Config** | `handle` (required), `service` (default `https://bsky.social`, must be https) |
| **Health** | `com.atproto.server.createSession` succeeds |
| **Action** | `post`: capability `social.post`, approval-gated (below) |
| **Action** | `delete_post`: `{ "rkey", "dry_run"? }` → `{ "deleted", "rkey", "network" }`. Approval-gated; the undo for a mistake. `rkey` is a post's `post_id`; a bsky.app or `at://` link to the post also works |
| **Actions (0.2.0)** | `comments`, `reply`, `like`, `repost`, `hide`, `delete_comment`, `message`: the shared engagement capabilities, below |

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

`post` and `delete_post` sign in afresh (`createSession`) every time. The engagement actions below reuse a signed-in session
for up to 50 minutes, in memory only (see "Sessions"). Images are not resized: shrink them first.

## Workflow Library recipe

**Post to Bluesky when published** (`post-on-publish`): when an entry of the chosen type is published, an editor
is asked first; once approved, its title (shortened to fit 300 characters) and the entry's link as a card go to Bluesky, and the post's id and link are kept on the entry
(`bluesky_post_id`, `bluesky_url`).

## Engagement (social.comments and friends)

Each action declares one capability, so Marvin's shared auto-reply engine and workflows reach it the same way on every
network. Gated actions (`requires_approval`) need an Approval step in a workflow. `post` (`social.post`) and `delete_post`
are unchanged.

| Action | Capability | Gated | Does |
|---|---|---|---|
| `comments` | `social.comments` | no | Replies, mentions and quotes as `{items, cursor}`, oldest first |
| `reply` | `social.reply` | yes | A chat message (private, default) or a public reply in the thread |
| `like` | `social.like` | no | Like a post |
| `repost` | `social.repost` | yes | Repost a post |
| `hide` | `social.hide` | no | Hide or unhide a reply under one of your own posts (thread gate) |
| `delete_comment` | `social.delete_comment` | yes | Delete one of your own posts |
| `message` | `social.message` | yes | A chat message to an account, by DID or handle |

- **`comments`** `{since?, limit = 50, kinds?}`. Source: `app.bsky.notification.listNotifications` with the reasons `reply`,
  `mention` and `quote` (the plugin also drops any other reason in case the server ignores the filter). Bluesky pages
  newest first and has no "since", so the `cursor` is the position of the newest notification handed over (its `indexedAt`
  plus its uri as a tie-break, base64url, opaque); the plugin pages back (100 a page, up to 20 pages) until a page holds
  something older than the cursor, then returns the oldest `limit` of what is newer. If more are waiting, the cursor
  stops after the last one returned, so the next poll continues from there and nothing is lost or repeated. Without a
  cursor you get the newest `limit` (a first run doesn't replay history). `kinds` filters, and the cursor still moves past
  what was filtered out. A cursor this plugin didn't return fails with code `invalid_cursor` (the engine drops it and
  rereads). Each item is `{id, kind, post_id, post_url, author: {id, handle, name}, text, created_at, url, network: "bluesky"}`:
  - `id`: the post's **at:// URI** (what `reply`, `like`, `hide` and `delete_comment` take as `comment_id`).
  - `kind`: **`reply`** for a reply whose parent or thread root is one of your posts; **`mention`** for a mention, and
    **also for a quote** of your post (the contract has no fourth kind), and for a mention inside a reply to someone else.
    Bluesky has no separate "comment" (a comment is a reply), so `comment` is never produced and `kinds: ["comment"]`
    returns nothing. A post that notifies twice (a reply that also mentions you) is returned once, as `reply`.
  - `post_id`: the parent post's at:// URI (`record.reply.parent`); for a quote, the quoted post; `null` for a plain mention.
    `post_url`: its bsky.app link (your own posts use your handle, others the DID).
  - `author`: `id` is the DID, `handle`, `name` the display name (the handle when there is none).
  - `created_at`: the record's own `createdAt`, as ISO 8601 UTC (the `indexedAt` when it has none); items are ordered by
    `indexedAt`. `url`: `https://bsky.app/profile/<handle>/post/<rkey>` (the DID replaces an unverified `handle.invalid`).
- **`reply`** `{comment_id, text, private = true, dry_run}` → `{replied, reply_id, private, url, network}`, plus `would_reply`
  on a dry run. `comment_id` is an at:// URI (a bsky.app post link also works). The text is sent as given (no @mention added).
  - *Public* (`private: false`): fetches the parent with `app.bsky.feed.getPosts` for its `cid` and the thread root (its
    `record.reply.root`, or itself when it is top-level) and creates an `app.bsky.feed.post` with
    `reply: {root: {uri, cid}, parent: {uri, cid}}`, with the same link and mention facets and the same 300-character check as `post`.
    `reply_id` is the new post's at:// URI and `url` its bsky.app link.
  - *Private*: Bluesky chat to the post's author (their DID is the repo of the URI): `chat.bsky.convo.getConvoForMembers`
    then `chat.bsky.convo.sendMessage`, sent through your PDS with the header `atproto-proxy: did:web:api.bsky.chat#bsky_chat`.
    Up to 1,000 characters. `reply_id` is the chat message id, `url` is `null`. If chat refuses (the app password has no DM
    access, or the person doesn't accept messages from you or has blocked you) it fails with code **`unsupported`** and a message
    saying which; a private reply to your own post is refused too. The text is kept as given: it does not say which post it is about.
  - A dry run reports the plan: private needs no sign-in at all; public signs in and reads the parent (so it can show the refs) and
    creates nothing.
- **`like`** `{comment_id}` → `{liked, network}`: `app.bsky.feed.like` with `subject: {uri, cid}` (cid from `getPosts`). A post you
  already like is left alone.
- **`repost`** `{post_id}` → `{reposted, url, network}`: `app.bsky.feed.repost` with `subject: {uri, cid}`. `post_id` is an at:// URI,
  a bsky.app link, or the record key of one of your own posts (what `post` returns); `url` is the reposted post. Already reposted: left alone.
- **`hide`** `{comment_id, hidden = true}` → `{hidden, network}`: Bluesky hides a reply through the **thread gate** on the thread's root
  post, so this only works in threads that start with one of your own posts. It reads `app.bsky.feed.threadgate` (rkey = the root post's
  rkey) with `com.atproto.repo.getRecord`, adds (or removes) the reply's at:// URI in `hiddenReplies`, keeps `allow` and every other
  field as they were, and writes it back (`putRecord` guarded by the record's cid, or `createRecord` when the post had no gate). A
  new gate has **no `allow` list**, which means anyone can reply as before. Anything else (a top-level post, someone else's thread) is
  refused with code `unsupported`. Hiding what is hidden, or unhiding what isn't, writes nothing. Hiding is per thread, not a delete: the
  reply still exists and can be unhidden.
- **`delete_comment`** `{comment_id}` → `{deleted, network}`: `deleteRecord` on one of your own posts (the repo of the URI is your
  DID); anyone else's is refused with code **`unsupported`** ("Bluesky can delete only your own posts"). `delete_post` (an rkey) still exists.
- **`message`** `{to: {id?, handle?}, text, dry_run}` → `{sent, message_id, network}`: a chat message as for a private reply. `id` is a
  DID (it wins when both are given); a `handle` is resolved with `com.atproto.identity.resolveHandle` (not found: `unsupported`).
  `would_send` on a dry run, which resolves a handle but doesn't sign in.

Bad arguments, an over-long text, an unknown post and everything Bluesky can't do use code `unsupported` with a plain message;
failures from Bluesky keep `auth`, `rate_limited`, `unavailable` and `invalid`.

### Sessions

Bluesky limits `createSession` (about 300 a day per account), and Marvin's engagement engine reads comments every few minutes, so
signing in per action would run out in a day. The engagement actions reuse a session for 50 minutes in process memory (never written
anywhere; keyed by service, handle and a digest of the app password), and when Bluesky rejects a reused session they sign in again once
and retry. `post`, `delete_post` and the health check still sign in every time (which also refreshes the reusable session).

## Not yet

Hashtag facets, link-card thumbnails, image aspect ratios, video, quote posts and threads of your own, image resizing, chat
attachments, and reading chat replies (private replies are sent; what people answer in chat is not read).

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

### Engagement (0.2.0): also not run against a live server

These are from memory of the AT Protocol and Bluesky chat lexicons. Check them before relying on the feature:

- **verify** Chat: the proxy header `atproto-proxy: did:web:api.bsky.chat#bsky_chat` on `chat.bsky.convo.*` calls sent to the PDS; the
  lexicon names `chat.bsky.convo.getConvoForMembers` (a query: `?members=<did>`, answering `{convo: {id, ...}}`) and
  `chat.bsky.convo.sendMessage` (`{convoId, message: {text, facets?}}`, answering `{id, ...}`); that `members` takes just the other
  person's DID (the caller may need to be listed too); the 1,000-grapheme chat text limit.
- **verify** What a refused chat call looks like. The plugin treats an HTTP 401/403 (or `InvalidToken`, "Bad token scope") from a chat call as
  "no DM access" and any other 4xx (HTTP 400) as "the person doesn't accept messages", both as `unsupported`; `ExpiredToken` is not
  mistaken for either (it signs in again). Bluesky's real status and error names for each case are unconfirmed.
- **verify** `app.bsky.notification.listNotifications` accepts `reasons` (repeated), `limit` up to 100 and `cursor`, answers newest first by
  `indexedAt`, and goes through the PDS to the AppView when called with the PDS token (the `reasons` filter is also applied again client-side).
  Which notifications a `reply` reason covers (the parent's author, and perhaps the thread root's) decides what a `reply` item is; the plugin
  calls it `reply` when the parent or the root is yours.
- **verify** The thread gate: the record `app.bsky.feed.threadgate` has `post` (the root's at:// URI), `allow` (optional; absent means
  everyone, an empty list means nobody), `hiddenReplies` (at most 300 at:// URIs, a limit this plugin enforces from memory) and
  `createdAt`, its rkey is the root post's rkey, and `getRecord` answers HTTP 400 `RecordNotFound` when there is none. Whether hiding
  needs the gated post to allow replies (it should not) is unchecked.
- **verify** `app.bsky.feed.getPosts` through the PDS, and that its `viewer.like` / `viewer.repost` fields exist (they are used to skip a
  duplicate like or repost).
- **verify** `createSession` rate limits (about 30 per 5 minutes and 300 per day), and the access token's life (the reused session is
  dropped after 50 minutes, and replaced when Bluesky rejects it).
- The `created_at` of a notification is the author's own `createdAt` and may be wrong or backdated; ordering and the cursor use `indexedAt`.
  A notification indexed late with an older `indexedAt` than the cursor would be missed (not seen in practice; unconfirmed).

## Develop

```bash
uv run --extra dev pytest -q
uv run --extra dev ruff check .
```

Tests use a fake `ctx.http` and a made-up password: no network, no credentials. The SDK resolves from the sibling
`../MarvinIntegrationSDK` checkout (see `[tool.uv.sources]`); released builds take it from the index.

`logo.svg` (since 0.4.2) is Bluesky's butterfly logo in its blue (`#1185fe`), from
[Wikimedia Commons](https://commons.wikimedia.org/wiki/File:Bluesky_Logo.svg), unaltered. Marvin shows it on the connection's
card and in Social; the 🦋 emoji is the fallback if Marvin refuses the file.
