# Bluesky

Posts to your Bluesky account straight from Marvin, as Marvin's shared **social post** (`social.post`): the same text,
pictures and link that go to every connected network. It also reads the replies and mentions on your account and answers
them (Marvin's shared **engagement** capabilities). There is no third-party service in between.

## Connect it

1. In Bluesky: **Settings → Privacy and security → App passwords → Add app password**. Copy it (it looks like `abcd-efgh-ijkl-mnop`).
   Never use your account password.
2. In Marvin: **Settings → Integrations → Bluesky**. Set **Handle** (e.g. `name.bsky.social`) and paste the app password. Leave
   **Service** alone unless you run your own server. The card should turn `ok`.
3. For **private replies and messages** (Bluesky chat), make the app password with **Allow access to your direct messages**
   ticked. Without it everything else works and only the chat actions fail ("this app password can't use direct messages").
   Reading replies and mentions needs no extra permission. The person you write to must accept messages from your account.

## Actions

- **Post to Bluesky** (`post`, capability `social.post`, needs approval) takes `text`, `images`, `alt`, `link` and `dry_run`. Output:
  `posted`, `post_id` (the record key), `url` (the post's link), `network` = `bluesky`; a dry run returns `would_post` instead.
- **Delete a post** (`delete_post`, needs approval) takes `rkey` (a post's `post_id`) and removes that post: the undo for a mistake.

- **Read replies and mentions** (`comments`, capability `social.comments`): args `since` (the cursor returned last time), `limit`
  (default 50), `kinds`. Returns `items` (oldest first) and `cursor`. Each item has `id` (the post's `at://` URI), `kind`, `post_id`
  (the post it answers), `post_url`, `author` (`id` is the DID, `handle`, `name`), `text`, `created_at`, `url` and `network`. A reply
  under your post is kind `reply`; a mention is `mention`, and so is a quote of your post. `comment` is never produced.
- **Reply** (`reply`, `social.reply`, needs approval): args `comment_id` (an item's `id`), `text`, `private` (default true), `dry_run`.
  Private is a Bluesky chat message to the person (needs the DM permission above); `private: false` replies publicly in the thread.
  Returns `replied`, `reply_id`, `private`, `url` (public only), `network` and, on a dry run, `would_reply`. The text goes out as
  given; it doesn't name the post it is about.
- **Like** (`like`, `social.like`): arg `comment_id`. Returns `liked`, `network`.
- **Repost** (`repost`, `social.repost`, needs approval): arg `post_id` (an `at://` URI, a bsky.app link, or one of your own posts'
  `post_id`). Returns `reposted`, `url`, `network`.
- **Hide** (`hide`, `social.hide`): args `comment_id`, `hidden` (default true; false shows it again). Works only on replies in threads
  that start with one of your own posts (it uses that post's thread settings, and keeps your reply rules). Returns `hidden`, `network`.
- **Delete a reply** (`delete_comment`, `social.delete_comment`, needs approval): arg `comment_id`. Only your own posts; someone else's
  fails with code `unsupported`. Returns `deleted`, `network`.
- **Send a message** (`message`, `social.message`, needs approval): args `to` (`{handle}` or `{id}` with a DID), `text`, `dry_run`. A
  chat message. Returns `sent`, `message_id`, `network`, and `would_send` on a dry run.

Put an Approval step before the post, a reply, a message, a repost and a delete: each needs a person's approval first.

## What it does with your arguments

- `text`: up to **300 characters** (what a person sees as one character, so an emoji counts once). Links in it become clickable.
  `@handle.bsky.social` mentions are linked when the handle resolves to an account.
- `images`: up to **4**, public https URLs, each **at most 1 MB** (JPEG, PNG, WebP or GIF). Marvin does not resize them. `alt`
  gives each picture its alt text, in order.
- `link`: added to the end of the text unless the text already contains it. With no pictures it also becomes a link card
  (the page's title and description). Bluesky can't show a card beside pictures, so then the link is in the text only.
- `dry_run`: returns the post that would be made, without signing in or posting.

## What it can't do

- More than 4 pictures, a picture over 1 MB, or text over 300 characters: refused with a message, nothing is posted.
  Use `per_network` to give Bluesky a shorter text or fewer pictures.
- No video, no quote posts or threads of your own, no hashtag links, no scheduling.
- **Chat** (private replies and messages) needs the DM permission on the app password and a recipient who accepts messages from you;
  otherwise it fails with code `unsupported` and says which. Chat replies from people aren't read.
- **Hide** works only under your own posts. **Delete a reply** works only on your own posts. Both refuse with `unsupported` otherwise.
- A reply, like or hide on a post that was deleted fails with `unsupported` ("Bluesky has no post at ...").

## When it fails

- "Bluesky refused sign-in": the handle or app password is wrong, or the app password was deleted. Make a new one and paste it again.
- "Bluesky is rate-limiting": too many posts too fast; Marvin retries a few times.
- "Image N is X MB": shrink that picture below 1 MB and run it again.
- "Bluesky chat refused ...: this app password can't use direct messages": make a new app password with **Allow access to your
  direct messages** ticked and paste it in.
- "Bluesky chat won't take this: the person may not accept messages from this account": they have chat restricted (for example to people
  they follow) or have blocked the account; reply publicly (`private: false`) instead.
- `invalid_cursor`: `since` is not a cursor this plugin returned; start again without it.
