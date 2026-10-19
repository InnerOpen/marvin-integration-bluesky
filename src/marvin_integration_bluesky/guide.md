# Bluesky

Posts to your Bluesky account straight from Marvin, as Marvin's shared **social post** (`social.post`): the same text,
pictures and link that go to every connected network. There is no third-party service in between.

## Connect it

1. In Bluesky: **Settings → Privacy and security → App passwords → Add app password**. Copy it (it looks like `abcd-efgh-ijkl-mnop`).
   Never use your account password.
2. In Marvin: **Settings → Integrations → Bluesky**. Set **Handle** (e.g. `name.bsky.social`) and paste the app password. Leave
   **Service** alone unless you run your own server. The card should turn `ok`.

## Actions

- **Post to Bluesky** (`post`, capability `social.post`, needs approval) takes `text`, `images`, `alt`, `link` and `dry_run`. Output:
  `posted`, `post_id` (the record key), `url` (the post's link), `network` = `bluesky`; a dry run returns `would_post` instead.
- **Delete a post** (`delete_post`, needs approval) takes `rkey` (a post's `post_id`) and removes that post: the undo for a mistake.

Put an Approval step before the post: every social post needs a person's approval first.

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
- No video, no replies, quotes or threads, no hashtag links, no scheduling.

## When it fails

- "Bluesky refused sign-in": the handle or app password is wrong, or the app password was deleted. Make a new one and paste it again.
- "Bluesky is rate-limiting": too many posts too fast; Marvin retries a few times.
- "Image N is X MB": shrink that picture below 1 MB and run it again.
