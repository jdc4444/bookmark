# X Bookmark Reporter

Fetch your X bookmarks through the official X API, save a complete local archive,
and generate a Markdown report with themes, authors, links, engagement signals,
and a bookmark index.

The utility uses OAuth 2.0 Authorization Code Flow with PKCE. It does not scrape
the X website and it does not need third-party Python packages.

## Public Repo Contents

This public checkout includes the app, the local bookmark archive/artifacts,
generated articles/modules, reports, longform support files, and
`data/media-cache/`. It excludes credential-bearing local files such as OAuth
tokens, API key files, `.env` files, and browser automation profiles/cookies.

## What It Writes

- `data/x-bookmarks.json`: full bookmark archive with normalized fields and raw
  API post payloads.
- `data/x-bookmarks.csv`: spreadsheet-friendly bookmark index.
- `reports/x-bookmark-report.md`: readable analysis report.

## X App Setup

1. Create or open an app in the X Developer Console.
2. Enable OAuth 2.0.
3. Add this callback URL exactly:

   ```text
   http://127.0.0.1:8765/callback
   ```

4. Make sure the app can request these scopes:

   ```text
   tweet.read users.read bookmark.read offline.access
   ```

5. Copy your OAuth 2.0 Client ID. If your app is a confidential client, copy the
   Client Secret too.

X references:

- [Bookmarks introduction](https://docs.x.com/x-api/posts/bookmarks/introduction)
- [Bookmarks lookup quickstart](https://docs.x.com/x-api/posts/bookmarks/quickstart/bookmarks-lookup)
- [OAuth 2.0 PKCE guide](https://docs.x.com/fundamentals/authentication/oauth-2-0/authorization-code)
- [Rate limits](https://docs.x.com/x-api/fundamentals/rate-limits)

## Quick Start

```bash
git clone https://github.com/jdc4444/bookmark.git
cd bookmark
export X_CLIENT_ID="your-client-id"

# Only needed for confidential X apps:
export X_CLIENT_SECRET="your-client-secret"

python3 x_bookmark_reporter.py run
```

The first run opens X in your browser. After you approve access, X redirects to
the local callback server, the token is saved to `.x_tokens.json`, and the
utility starts fetching bookmarks.

## Web UI

Start the local UI:

```bash
python3 x_bookmark_reporter.py ui
```

Then open:

```text
http://127.0.0.1:8787
```

The UI can connect your X account, fetch bookmarks, rebuild the report, show the
live run log, render the Markdown report, and browse/search the bookmark archive.
The Client Secret field is not saved to browser local storage.

If you do not have an X API Client ID but you are already signed in to X in
Safari, use **Safari Import**. That mode opens `https://x.com/i/bookmarks` in
Safari, scrolls the bookmark timeline, extracts the rendered posts, and writes
the same local JSON/CSV/report files.

For semantic analysis, use **Analyze with OpenAI CLI** in the Backend tab. The UI
does not call OpenAI directly from browser JavaScript. It runs the local
`openai` command-line tool on the server process and writes:

```text
reports/openai-bookmark-analysis.json
```

The CLI needs `OPENAI_API_KEY` in the UI server environment:

```bash
export OPENAI_API_KEY="..."
python3 x_bookmark_reporter.py ui
```

## Commands

Authorize only:

```bash
python3 x_bookmark_reporter.py auth --client-id "$X_CLIENT_ID"
```

Fetch bookmarks only:

```bash
python3 x_bookmark_reporter.py fetch \
  --output data/x-bookmarks.json \
  --csv data/x-bookmarks.csv
```

Generate a report from an existing archive:

```bash
python3 x_bookmark_reporter.py report \
  --input data/x-bookmarks.json \
  --output reports/x-bookmark-report.md
```

Fetch a small sample for testing:

```bash
python3 x_bookmark_reporter.py run --limit 25
```

Import from signed-in Safari instead of the official API:

```bash
python3 x_bookmark_reporter.py safari-import
```

Use a different callback:

```bash
python3 x_bookmark_reporter.py run \
  --redirect-uri "http://127.0.0.1:9000/callback"
```

If the callback cannot listen locally, use manual mode:

```bash
python3 x_bookmark_reporter.py auth --manual-callback --no-browser
```

## Notes

- X bookmarks are private, so app-only bearer tokens are not enough. The utility
  needs a user access token with `bookmark.read`, `tweet.read`, and `users.read`.
- `offline.access` is included so X can issue a refresh token and future runs do
  not need another browser approval.
- The bookmark endpoint currently allows up to 100 results per page and has a
  per-user rate limit. The utility follows pagination until all bookmarks are
  fetched and will wait for rate-limit reset headers by default.
- `.x_tokens.json` is ignored by Git and saved with owner-only permissions.
