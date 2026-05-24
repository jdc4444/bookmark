"""Generate Economist/New Yorker-style briefs for bookmarks.

Architecture
------------
- Per-bookmark article: cached at data/articles/article_<bookmark_id>.json
- Per-entity modules (ticker, poster, concept, book, repo): cached at
  data/modules/<type>_<key>.json so the same module is reused across articles.

Model selection
---------------
1. Try `claude -p` with sonnet + WebSearch + WebFetch (best quality, real research).
2. Fall back to local Ollama (qwen3:30b-a3b) when claude is rate-limited or
   unavailable. Qwen3 has no live web access; it leans on its training knowledge.
"""

from __future__ import annotations

import fcntl
from contextlib import contextmanager
import datetime as dt
import json
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

from archive_io import write_json_atomic

ROOT = Path(__file__).resolve().parent
ARTICLES_DIR = ROOT / "data" / "articles"
MODULES_DIR = ROOT / "data" / "modules"
ARTICLE_LOCKS_DIR = ARTICLES_DIR / ".locks"
MODULE_LOCKS_DIR = MODULES_DIR / ".locks"
ARTICLES_DIR.mkdir(parents=True, exist_ok=True)
MODULES_DIR.mkdir(parents=True, exist_ok=True)
ARTICLE_LOCKS_DIR.mkdir(parents=True, exist_ok=True)
MODULE_LOCKS_DIR.mkdir(parents=True, exist_ok=True)

CLAUDE_MODEL = "claude-sonnet-4-6"
OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
OLLAMA_MODEL = "qwen3:30b-a3b"

DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
DEEPSEEK_MODEL = "deepseek-chat"
DEEPSEEK_KEY_FILE = ROOT / ".deepseek_key"


USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.6 Safari/605.1.15"
)


def web_search(query: str, *, k: int = 5, timeout: float = 10.0) -> list[dict]:
    """DuckDuckGo HTML search — returns top result URLs with titles/snippets."""
    import html
    import urllib.parse
    url = "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(query)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return []
    results: list[dict] = []
    pattern = re.compile(
        r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>.*?'
        r'<a[^>]+class="result__snippet"[^>]*>(.*?)</a>',
        re.DOTALL,
    )
    for m in pattern.finditer(body):
        href, title, snippet = m.groups()
        if "duckduckgo.com" in href:
            continue
        href_clean = href
        if href.startswith("//duckduckgo.com/l/?uddg="):
            href_clean = urllib.parse.unquote(href.split("uddg=")[-1].split("&")[0])
        elif href.startswith("/l/?uddg="):
            href_clean = urllib.parse.unquote(href.split("uddg=")[-1].split("&")[0])
        title_clean = html.unescape(re.sub(r"<.*?>", "", title)).strip()
        snippet_clean = html.unescape(re.sub(r"<.*?>", "", snippet)).strip()
        if href_clean and title_clean:
            results.append({"url": href_clean, "title": title_clean,
                            "snippet": snippet_clean})
        if len(results) >= k:
            break
    return results


def fetch_url_text(url: str, *, timeout: float = 10.0,
                   max_chars: int = 4000) -> str:
    """Fetch a URL and return plain-text content (HTML stripped)."""
    import html
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            ctype = resp.headers.get("Content-Type", "").lower()
            if "html" not in ctype and "text" not in ctype:
                return ""
            raw = resp.read(800_000).decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return ""
    raw = re.sub(r"<script[^>]*>.*?</script>", " ", raw, flags=re.DOTALL | re.IGNORECASE)
    raw = re.sub(r"<style[^>]*>.*?</style>", " ", raw, flags=re.DOTALL | re.IGNORECASE)
    raw = re.sub(r"<.*?>", " ", raw)
    raw = html.unescape(raw)
    raw = re.sub(r"\s+", " ", raw).strip()
    return raw[:max_chars]


def gather_research(query: str, *, max_pages: int = 3) -> str:
    """Search and fetch a few top pages; return a concatenated research block."""
    results = web_search(query, k=max_pages + 2)
    if not results:
        return ""
    chunks: list[str] = []
    pulled = 0
    for r in results:
        if pulled >= max_pages:
            break
        text = fetch_url_text(r["url"])
        if not text:
            chunks.append(f"[Source: {r['title']} — {r['url']}]\n{r['snippet']}")
            continue
        chunks.append(
            f"[Source: {r['title']} — {r['url']}]\n{text[:2500]}"
        )
        pulled += 1
    return "\n\n---\n\n".join(chunks)


def _deepseek_key() -> str | None:
    if DEEPSEEK_KEY_FILE.exists():
        k = DEEPSEEK_KEY_FILE.read_text().strip()
        if k:
            return k
    import os
    return os.environ.get("DEEPSEEK_API_KEY") or None


# --------------------------- prompts ---------------------------

ARTICLE_PROMPT = """You write tight, smart briefs about saved X.com posts in the spirit of The Economist or The New Yorker — measured, informed, allergic to hype. The reader is intelligent but may not know niche jargon.

EDITORIAL STYLE
- Use markdown emphasis sparingly inside the body: **bold** for the key claim or pivotal noun, *italic* for terms-of-art and titles. Don't decorate every sentence.
- Write in flowing paragraphs separated by a blank line. No headings inside the body.
- Vary sentence length. Concrete nouns over adjectives. Cut hedges that don't pull weight.
- Whenever you name a publicly-traded company in the body, append its ticker in parentheses on the FIRST mention in that paragraph: "Sumitomo Electric (5802.T)", "Coherent (COHR)", "Taiwan Semiconductor Manufacturing (TSM)". Use the canonical exchange-suffixed ticker for non-US listings. Don't repeat the ticker on subsequent mentions in the same paragraph. If a company is private or you don't know the ticker, just use the name.

RESEARCH APPROACH
Before writing, dispatch the Agent tool (subagent_type="general-purpose") to do the research. Tell it: identify the substantive subject of this bookmark, then gather 5-8 authoritative primary sources — official filings, the company's own site, the original paper or repo, reputable news coverage, the poster's prior context. Have it return a structured digest with quotes and URLs. Then write the brief from that digest.

Below the bookmark you'll find a few RESEARCH SOURCES that were auto-fetched as a starting hint — they're not enough on their own. The sub-agent's research is the real evidence base.

Cite specific facts only from sources the sub-agent or you actually consulted. Do NOT fabricate company names, ticker meanings, or biographical details — when uncertain, say so rather than inventing. The `sources[]` array in your output must list every URL the sub-agent used (or you fetched yourself), with title and one-line note.

WHAT TO WRITE ABOUT
First identify what the bookmark is actually *about*. The tweet text often is the bookmark — a substantive claim, argument, or observation worth treating directly. But sometimes the tweet is a joke, aside, or thin caption ("look at this", "I'm reading this", "stole this from a friend"), and the substance lives in the attached media or quoted parent: a book on a shelf, a paper screenshot, a chart, a repo URL, a quoted argument. In those cases the SUBJECT of the brief is that substantive object — the book, the paper, the chart's claim — not the joke. Use the tweet's framing as helpful context (one sentence is usually enough), then write about the thing.

EXPAND TRUNCATED POSTS
X.com truncates the body of long posts to ~280 chars in several surfaces. If the quoted-tweet text in the bookmark below ends mid-sentence, mid-clause, or otherwise looks cut off (telltale signs: "...", "may be", "is expected to", trailing prepositions), use WebFetch on the quoted post's URL (the [BOOKMARK QUOTES @user] URL when present, or construct https://x.com/<username>/status/<id> from the chain context) to read the full post BEFORE writing. The substance is usually in the quoted post's full body, not the bookmark's own one-liner reaction.

Apply the same rule to the BOOKMARK's own tweet. If the BOOKMARK TWEET is suspiciously short for the attached media/link context, looks like an opening paragraph, or ends with a link after only a short setup, WebFetch the bookmark URL shown below and use the full status-page body before choosing the subject.

OUTPUT — return ONLY a JSON object, no prose outside it, no code fences.

CRITICAL JSON ESCAPING: every double-quote character inside a string value MUST be escaped as \" — never use a bare " character inside a string. When quoting someone's words in the body, prefer curly quotes (e.g. "they're buying X to make Y work") or escape with \" — but NEVER use raw straight double-quotes, they break the JSON.



{{
  "headline": "<8-12 word title in headline case>",
  "lede": "<2-3 sentence opening that orients a smart reader>",
  "body": "<3-5 short paragraphs explaining the substance, claims, and significance. Embed brief inline definitions for any non-obvious jargon. If the post makes a verifiable claim that's wrong or oversimplified, flag it within the body. Use markdown for emphasis sparingly.>",
  "tickers": ["<every public-company ticker that appears anywhere in your body, e.g. LITE, AAPL. Use the bare ticker (no $)>"],
  "companies": [
    {{"name": "<canonical company name, e.g. 'Sumitomo Electric'>", "ticker": "<ticker if publicly traded, else null>"}}
  ],
  "poster": "<the poster's username (no @)>",
  "concepts": [
    {{"key": "<kebab-case-slug>", "term": "<display name, e.g. 'golden pocket'>", "definition": "<2-4 sentence explanation written in plain language>"}}
  ],
  "books": [
    {{"key": "<slug>", "title": "<full title>", "author": "<author>", "summary": "<3-4 sentences on what the book is about and why it matters>"}}
  ],
  "repos": [
    {{"key": "<owner-repo>", "url": "<github url>", "summary": "<3 sentences on what the repo does, why people use it>"}}
  ],
  "fact_check": "<null if no issues; otherwise a short paragraph noting incorrect or misleading claims>",
  "thread_summary": "<null if not a thread starter; otherwise 2-3 sentences summarizing the full thread>",
  "sources": [
    {{"url": "<source url>", "title": "<source title>", "note": "<one-line why this source>"}}
  ]
}}

EXTRACTION RULES — these apply to YOUR body, not just the source post:
- companies[]: include EVERY company you reference by name in the body, with its ticker if publicly traded (use null otherwise). This includes companies you brought in as context that the source post didn't mention. Names should be canonical (e.g. "Taiwan Semiconductor Manufacturing", not "TSM" or "Taiwan Semi"). Don't list the same company twice.
- tickers[]: every public-company ticker that appears anywhere in your body. Must be a strict subset of companies[].ticker (non-null values). Bare ticker, no $.
- concepts[]: include EVERY non-obvious term-of-art, jargon word, or acronym you use in the body — even if you defined it inline, even if you introduced it yourself. If a smart-but-non-specialist reader could pause and ask "wait, what's that exactly?", it belongs in concepts[].
- Books and repos: same rule — if you mention them, list them.

Before returning the JSON, re-read your body and check: does every proper-noun company appear in companies[]? Does every term you italicized or briefly defined appear in concepts[]? If not, add them.

BOOKMARK
Author: @{username}
Tweet: {text}
{ocr_block}
URL: {url}

{context_block}

{focus_block}{known_block}
RESEARCH SOURCES (auto-fetched starting hint — use your tools to find more)
{research}
"""


MODULE_TICKER_PROMPT = """Write a tight encyclopedia-style entry about the public company with ticker {key}. Below are RESEARCH SOURCES from the web — use them as ground truth. If the sources don't make the answer clear, say "Not enough public information" instead of inventing details.

Return ONLY a JSON object:

{{
  "key": "{key}",
  "name": "<official company name>",
  "exchange": "<NASDAQ, NYSE, Stockholm, etc.>",
  "blurb": "<3-4 sentence description of what the company does and its market position>",
  "recent": "<2-3 sentences on the past 30 days: notable news, earnings, price action, catalysts>",
  "sources": [{{"url": "...", "title": "..."}}]
}}

RESEARCH SOURCES
{research}
"""


MODULE_POSTER_PROMPT = """Write a tight bio-style entry about the X.com user @{key}. Below are RESEARCH SOURCES from the web — use them as ground truth. If you can't find clear info, say so rather than inventing details.

Return ONLY a JSON object:

{{
  "key": "{key}",
  "display_name": "<their display name>",
  "blurb": "<3-4 sentences: who they are, domain expertise, what they post about, follower count if notable>",
  "credibility_note": "<null or 1-2 sentence note on track record / known biases / corrections>",
  "links": ["<personal site, fund, profile links>"],
  "sources": [{{"url": "...", "title": "..."}}]
}}

RESEARCH SOURCES
{research}
"""


# --------------------------- llm calls ---------------------------

def sanitize_prompt_arg(prompt: str) -> str:
    """Remove bytes that cannot be passed as a subprocess argv string."""
    return prompt.replace("\x00", "")


def claude_call(prompt: str, *,
                allowed_tools: str = "Agent,WebSearch,WebFetch",
                effort: str = "high",
                timeout: float = 1800.0) -> tuple[bool, str]:
    """Sonnet via the CLI with research sub-agents enabled.

    The `Agent` tool lets Claude spawn a general-purpose sub-agent for the
    research phase — exactly the pattern that produced the older
    "claude-sonnet-subagent" articles (5000-char bodies, 8 distinct sources).
    `--effort high` gives the model more thinking budget so it actually USES
    those tools instead of defaulting to whatever's already in context.
    """
    cmd = [
        "claude", "-p", sanitize_prompt_arg(prompt),
        "--model", CLAUDE_MODEL,
        "--permission-mode", "bypassPermissions",
        "--allowed-tools", allowed_tools,
        "--effort", effort,
        "--output-format", "text",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, "timeout"
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        return False, err[:300] or f"exit_{proc.returncode}"
    return True, proc.stdout


def deepseek_call(prompt: str, *, timeout: float = 180.0) -> tuple[bool, str]:
    key = _deepseek_key()
    if not key:
        return False, "no_deepseek_key"
    body = json.dumps({
        "model": DEEPSEEK_MODEL,
        "messages": [
            {"role": "system",
             "content": "You output only valid JSON when asked. No prose, no code fences."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.3,
        "max_tokens": 4000,
        "response_format": {"type": "json_object"},
    }).encode("utf-8")
    req = urllib.request.Request(
        DEEPSEEK_URL,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
    except Exception as exc:  # noqa: BLE001
        return False, f"deepseek_error: {exc}"
    try:
        content = data["choices"][0]["message"]["content"]
    except Exception:  # noqa: BLE001
        return False, f"deepseek_unexpected_response: {str(data)[:200]}"
    return True, content


def ollama_call(prompt: str, *, timeout: float = 240.0) -> tuple[bool, str]:
    body = json.dumps({
        "model": OLLAMA_MODEL,
        "prompt": "/no_think " + prompt,
        "stream": False,
        "options": {"temperature": 0.2, "num_predict": 5000},
    }).encode("utf-8")
    req = urllib.request.Request(
        OLLAMA_URL, data=body, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return True, json.loads(resp.read()).get("response", "")
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


_CLAUDE_DOWN_UNTIL: float = 0.0  # process-local cooldown after a rate-limit hit


def call_llm(prompt: str, *, prefer: str = "deepseek",
             allowed_tools: str = "Agent,WebSearch,WebFetch") -> tuple[str, str]:
    """Returns (backend_used, raw_response). Falls back transparently.

    Order: prefer → other available backends. On claude rate-limit, suppress
    claude attempts for 30 min.
    """
    global _CLAUDE_DOWN_UNTIL
    import time
    order: list[str]
    if prefer == "claude":
        # Sonnet-only: don't silently downgrade to deepseek/ollama on failure.
        # An inferior fallback hides quality regressions inside successful runs.
        # Better to surface the failure so the caller knows to retry.
        order = ["claude"]
    elif prefer == "ollama":
        order = ["ollama", "deepseek", "claude"]
    else:
        order = ["deepseek", "claude", "ollama"]

    last_err = ""
    for backend in order:
        if backend == "claude":
            if time.time() < _CLAUDE_DOWN_UNTIL:
                continue
            ok, out = claude_call(prompt, allowed_tools=allowed_tools)
            if ok:
                return "claude-sonnet-subagent", out
            last_err = out
            if "limit" in out.lower() or "rate" in out.lower():
                _CLAUDE_DOWN_UNTIL = time.time() + 1800
            continue
        if backend == "deepseek":
            ok, out = deepseek_call(prompt)
            if ok:
                return "deepseek", out
            last_err = out
            continue
        if backend == "ollama":
            ok, out = ollama_call(prompt)
            if ok:
                return "ollama", out
            last_err = out
            continue
    raise RuntimeError(f"All backends failed. Last error: {last_err[:300]}")


# --------------------------- response parsing ---------------------------

def parse_json_response(raw: str) -> dict | None:
    if not raw:
        return None
    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
    fence = re.search(r"```(?:json)?\s*(.*?)\s*```", raw, re.DOTALL)
    if fence:
        raw = fence.group(1).strip()
    a = raw.find("{")
    b = raw.rfind("}")
    if a < 0 or b <= a:
        return None
    try:
        return json.loads(raw[a:b + 1])
    except json.JSONDecodeError:
        return None


# --------------------------- entity helpers ---------------------------

def slugify(s: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", (s or "").lower()).strip("-")
    return s[:60]


# --------------------------- main: bookmark article ---------------------------

@contextmanager
def article_file_lock(bookmark_id: str):
    ARTICLE_LOCKS_DIR.mkdir(parents=True, exist_ok=True)
    safe_id = slugify(str(bookmark_id)) or "unknown"
    lock_path = ARTICLE_LOCKS_DIR / f"{safe_id}.lock"
    with lock_path.open("a") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def module_file_lock(mtype: str, key: str):
    MODULE_LOCKS_DIR.mkdir(parents=True, exist_ok=True)
    safe_type = slugify(str(mtype)) or "unknown"
    safe_key = slugify(str(key)) or "unknown"
    lock_path = MODULE_LOCKS_DIR / f"{safe_type}_{safe_key}.lock"
    with lock_path.open("a") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


def article_path(bookmark_id: str) -> Path:
    return ARTICLES_DIR / f"article_{bookmark_id}.json"


HISTORY_DIR = ARTICLES_DIR / "_history"

def _safe_ts(ts: str) -> str:
    return (ts or "").replace(":", "-").replace(".", "-").replace("/", "-").strip() or "v0"

def history_dir(bookmark_id: str) -> Path:
    p = HISTORY_DIR / str(bookmark_id)
    p.mkdir(parents=True, exist_ok=True)
    return p

def list_article_history(bookmark_id: str) -> list[dict]:
    """Return all versions newest-first: current + prior versions on disk."""
    out: list[dict] = []
    cur = article_path(str(bookmark_id))
    if cur.exists():
        try:
            data = json.loads(cur.read_text())
            out.append({
                "id": _safe_ts(data.get("generated_at") or "current"),
                "generated_at": data.get("generated_at"),
                "headline": data.get("headline"),
                "current": True,
            })
        except Exception:
            pass
    hd = HISTORY_DIR / str(bookmark_id)
    if hd.exists():
        items: list[tuple[str, dict]] = []
        for f in hd.iterdir():
            if not f.is_file() or not f.name.endswith(".json"):
                continue
            try:
                d = json.loads(f.read_text())
                items.append((d.get("generated_at") or f.stem, d))
            except Exception:
                continue
        items.sort(key=lambda x: x[0] or "", reverse=True)
        for ts, d in items:
            out.append({
                "id": _safe_ts(d.get("generated_at") or ""),
                "generated_at": d.get("generated_at"),
                "headline": d.get("headline"),
                "current": False,
            })
    return out

def load_article_version(bookmark_id: str, version_id: str) -> dict | None:
    cur = article_path(str(bookmark_id))
    if cur.exists():
        try:
            data = json.loads(cur.read_text())
            if _safe_ts(data.get("generated_at") or "current") == version_id:
                return data
        except Exception:
            pass
    hd = HISTORY_DIR / str(bookmark_id)
    if hd.exists():
        for f in hd.iterdir():
            if not f.is_file() or not f.name.endswith(".json"):
                continue
            try:
                d = json.loads(f.read_text())
                if _safe_ts(d.get("generated_at") or f.stem) == version_id:
                    return d
            except Exception:
                continue
    return None

def archive_current_version(bookmark_id: str) -> None:
    """Move the current cached article into _history/<id>/<ts>.json."""
    cur = article_path(str(bookmark_id))
    if not cur.exists():
        return
    try:
        raw = cur.read_text()
        data = json.loads(raw)
    except Exception:
        ts = _safe_ts(dt.datetime.now(dt.timezone.utc).isoformat())
        target = history_dir(bookmark_id) / f"{ts}.raw.json"
        suffix = 1
        while target.exists():
            target = history_dir(bookmark_id) / f"{ts}.{suffix}.raw.json"
            suffix += 1
        target.write_bytes(cur.read_bytes())
        return
    ts = _safe_ts(data.get("generated_at") or dt.datetime.now(dt.timezone.utc).isoformat())
    target = history_dir(bookmark_id) / f"{ts}.json"
    if target.exists():
        return  # already archived
    target.write_text(raw)


def write_article(bookmark_id: str, article: dict, *, archive: bool = True) -> None:
    with article_file_lock(bookmark_id):
        if archive:
            archive_current_version(bookmark_id)
        write_json_atomic(article_path(bookmark_id), article)


def module_path(mtype: str, key: str) -> Path:
    safe_key = slugify(key)
    return MODULES_DIR / f"{mtype}_{safe_key}.json"


def get_cached_article(bookmark_id: str, *, prefer: str | None = None) -> dict | None:
    p = article_path(str(bookmark_id))
    if p.exists():
        try:
            cached = json.loads(p.read_text())
        except Exception:
            return None
        if prefer == "claude" and cached.get("backend") != "claude-sonnet-subagent":
            return None
        return cached
    return None


def get_cached_module(mtype: str, key: str) -> dict | None:
    p = module_path(mtype, key)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            return None
    return None


def _compact_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def build_bookmark_context(bookmark: dict) -> str:
    context: dict[str, object] = {}
    urls = bookmark.get("urls") or []
    if urls:
        context["linked_urls"] = [
            {
                "expanded_url": u.get("expanded_url") or u.get("url") or "",
                "display_url": u.get("display_url") or "",
                "title": u.get("title") or "",
                "description": u.get("description") or "",
            }
            for u in urls
            if isinstance(u, dict)
        ]
    metrics = bookmark.get("public_metrics") or {}
    if metrics:
        context["public_metrics"] = metrics
    media_context = []
    for m in (bookmark.get("media") or []):
        if not isinstance(m, dict):
            continue
        item = {
            key: m.get(key)
            for key in ("type", "url", "alt", "source", "width", "height")
            if m.get(key) not in (None, "")
        }
        if item:
            media_context.append(item)
    if media_context:
        context["media"] = media_context
    reply_to = bookmark.get("reply_to") or []
    if reply_to:
        context["reply_to"] = reply_to
    status_context = bookmark.get("status_context") or {}
    post_context = status_context.get("post") or {}
    if isinstance(post_context, dict) and post_context.get("text"):
        context["status_page_post"] = {
            "text": post_context.get("text") or "",
            "url": post_context.get("url") or status_context.get("url") or "",
            "captured_at": status_context.get("loaded_at") or "",
        }
    replies = status_context.get("replies") or []
    if replies:
        context["status_context_replies"] = [
            {
                "author": ((reply.get("author") or {}).get("username") or ""),
                "text": (reply.get("text") or "")[:1000],
                "public_metrics": reply.get("public_metrics") or {},
                "url": reply.get("url") or "",
            }
            for reply in replies
            if isinstance(reply, dict)
        ]
    if not context:
        return ""
    return "BOOKMARK CONTEXT\n" + _compact_json(context)


def generate_article(bookmark: dict, *, prefer: str = "deepseek", focus: str | None = None,
                     use_cache: bool = True, write: bool = True) -> dict:
    bm_id = str(bookmark.get("id"))
    # When the caller passes a focus hint, always re-run — they're overriding
    # the cached interpretation.
    if use_cache and not focus:
        cached = get_cached_article(bm_id, prefer=prefer)
        if cached:
            return cached
    text = (bookmark.get("text") or "").strip()
    # Walk OCR from the BOOKMARK plus every nested quoted_tweet and thread
    # item. Without this, OCR text on a quoted-tweet image (like Citi's "15
    # Axioms" screenshot in the PythiaR semis post) never reaches the prompt
    # and the article writes around it.
    ocr_chunks: list[str] = []
    def _gather_ocr(media_list, label: str):
        for m in (media_list or []):
            t = (m.get("ocr_text") or "").strip()
            if t:
                ocr_chunks.append(f"[{label}] {t}" if label else t)
    _gather_ocr(bookmark.get("media"), "")
    q = bookmark.get("quoted_tweet") or {}
    if q:
        _gather_ocr(q.get("media"), f"image inside @{q.get('author_username','quoted')}'s post")
    for t in (bookmark.get("thread") or []):
        author = t.get("author_username") or "thread"
        _gather_ocr(t.get("media"), f"image in @{author}'s post earlier in chain")
        tq = t.get("quoted_tweet") or {}
        if tq:
            _gather_ocr(tq.get("media"), f"image inside @{tq.get('author_username','quoted')}'s post (quoted by @{author})")
    ocr_block = "Image OCR:\n" + "\n---\n".join(ocr_chunks) if ocr_chunks else ""

    # Fold the FULL conversation chain into the tweet text so the LLM can see
    # what the bookmark is responding to. We walk thread["before"] items oldest
    # first (chain root → immediate parent → bookmark), and for each item also
    # include its own quoted_tweet if present. Without this the article writes
    # about the bookmark in isolation and misses the discussion it's replying
    # to (e.g. r6ride_87's "energy revisions" reply only makes sense in the
    # context of taobanker's "revisions are all that matters" comment, which
    # itself was a reply to ConsensusGurus' SNDK chart).
    chain_pieces: list[str] = []
    for t in (bookmark.get("thread") or []):
        if t.get("position") != "before":
            continue
        author = t.get("author_username") or t.get("author_name") or "unknown"
        ttxt = (t.get("text") or "").strip()
        if ttxt:
            chain_pieces.append(f"[EARLIER IN CHAIN — @{author}]: {ttxt}")
        tq = t.get("quoted_tweet") or {}
        if tq and (tq.get("text") or tq.get("raw")):
            qa = tq.get("author_username") or tq.get("author_name") or "unknown"
            qt = tq.get("text") or tq.get("raw") or ""
            chain_pieces.append(f"[@{author} QUOTED @{qa}]: {qt}")
    # Also fold in the BOOKMARK's own quoted_tweet (independent of the thread).
    q = bookmark.get("quoted_tweet") or {}
    if q and (q.get("text") or q.get("raw")):
        qauthor = q.get("author_username") or q.get("author_name") or "unknown"
        qtext = q.get("text") or q.get("raw") or ""
        # Include the quoted tweet's URL so Sonnet can WebFetch it when the
        # text looks truncated (X.com cuts inline quoted posts at ~280 chars).
        qurl = q.get("url") or (f"https://x.com/{q.get('author_username')}/status/{q.get('id')}" if q.get('author_username') and q.get('id') else "")
        url_suffix = f" ({qurl})" if qurl else ""
        chain_pieces.append(f"[BOOKMARK QUOTES @{qauthor}{url_suffix}]: {qtext}")
    if chain_pieces:
        text = "\n\n".join(chain_pieces) + "\n\n[BOOKMARK TWEET]: " + text
    username = ((bookmark.get("author") or {}).get("username") or "unknown")
    url = bookmark.get("url") or ""

    # Research the post's primary subject. We use the first ~200 chars of the
    # tweet text plus the username as a query — it captures the topic without
    # being so specific that DDG returns no results.
    seed = (text[:200] + " " + username).strip() or username
    research = gather_research(seed, max_pages=3)

    # Pull in any cached profiles for entities the bookmark explicitly names
    # ($TICKER patterns in tweet/OCR text and the poster module). The LLM gets
    # them as ground truth so it doesn't waste WebSearch turns re-researching
    # things we already have a write-up for.
    known_entities = collect_known_entities(text, ocr_block, username)
    known_block = ""
    if known_entities:
        parts = ["KNOWN ENTITIES (already in our knowledge base — reference these directly, don't re-research)"]
        for kind, key, mod in known_entities:
            if kind == "ticker":
                name = mod.get("name") or key
                exch = mod.get("exchange") or ""
                parts.append(f"\n${key} — {name}{f' ({exch})' if exch else ''}\n  {(mod.get('blurb') or '').strip()}")
                if mod.get("recent"):
                    parts.append(f"  Recent: {mod['recent'].strip()}")
            elif kind == "poster":
                parts.append(f"\n@{key} — {(mod.get('display_name') or key)}\n  {(mod.get('blurb') or '').strip()}")
        known_block = "\n".join(parts) + "\n"
    focus_block = ""
    if focus and focus.strip():
        focus_block = (
            "FOCUS HINT FROM USER (overrides default subject reading)\n"
            f"{focus.strip()}\n"
            "If the tweet text frames the bookmark as a joke, aside, or thin commentary, "
            "and this focus hint points to the substantive subject (a book, paper, repo, "
            "product, ticker, or claim), write the brief about THAT subject. The tweet is "
            "the bookmark's pretext, not the topic.\n\n"
        )
    prompt = ARTICLE_PROMPT.format(
        text=text or "(no text)",
        ocr_block=ocr_block,
        context_block=build_bookmark_context(bookmark),
        username=username,
        url=url,
        focus_block=focus_block,
        known_block=known_block,
        research=research or "(no research results)",
    )
    backend, raw = call_llm(prompt, prefer=prefer)
    parsed = parse_json_response(raw)
    if parsed is None:
        # Sonnet sometimes emits raw `"` inside string values, breaking JSON.
        # Retry once with an explicit escape reminder appended — usually clean
        # on the second attempt.
        retry_prompt = prompt + (
            "\n\nIMPORTANT: your previous attempt produced JSON that failed to "
            "parse — you almost certainly used a bare \" inside a string value. "
            "Try again. EVERY double-quote inside a string MUST be escaped as \\\". "
            "When quoting speech, prefer curly-quote “…” pairs."
        )
        backend, raw = call_llm(retry_prompt, prefer=prefer)
        parsed = parse_json_response(raw)
    if parsed is None:
        raise ValueError(f"Could not parse {backend} output. First 400 chars: {raw[:400]}")

    parsed["bookmark_id"] = bm_id
    parsed["generated_at"] = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    parsed["backend"] = backend
    if write:
        write_article(bm_id, parsed, archive=True)

    # Backfill missing modules: every ticker the article references should
    # have a profile so the renderer doesn't show "(no profile yet)". Use
    # deepseek as the default backend for modules — they're encyclopedia-style
    # and don't need the heavier subagent path. Failures are non-fatal.
    try:
        ensure_referenced_modules(parsed, prefer_module="deepseek")
    except Exception:
        pass
    # Also kick off a poster module if missing.
    poster = (parsed.get("poster") or "").strip()
    if poster and not get_cached_module("poster", poster):
        try:
            generate_module("poster", poster, prefer="deepseek")
        except Exception:
            pass
    return parsed


_TICKER_PAT = re.compile(r"\$([A-Z]{1,5})\b")


def collect_known_entities(text: str, ocr_block: str, username: str) -> list[tuple[str, str, dict]]:
    """Scan tweet text + OCR for $TICKER patterns and the poster handle, then
    return (kind, key, module) for any of them we already have a profile for.
    Used to inject existing knowledge into the article prompt so the LLM
    doesn't re-research entities that are already in our pool."""
    out: list[tuple[str, str, dict]] = []
    seen: set[tuple[str, str]] = set()
    haystack = f"{text}\n{ocr_block or ''}"
    for m in _TICKER_PAT.finditer(haystack):
        key = m.group(1)
        sig = ("ticker", key)
        if sig in seen:
            continue
        seen.add(sig)
        mod = get_cached_module("ticker", key)
        if mod:
            out.append(("ticker", key, mod))
    if username and username != "unknown":
        mod = get_cached_module("poster", username)
        if mod:
            out.append(("poster", username, mod))
    return out


def ensure_referenced_modules(article: dict, *, prefer_module: str = "deepseek") -> list[str]:
    """Generate ticker modules for every ticker the article references that
    doesn't already have a cached profile. Looks at both `tickers[]` and any
    non-null `companies[].ticker`. Returns the list of newly-generated keys."""
    wanted: set[str] = set()
    for tk in (article.get("tickers") or []):
        if isinstance(tk, str) and tk.strip():
            wanted.add(tk.strip().upper())
    for c in (article.get("companies") or []):
        if isinstance(c, dict) and c.get("ticker"):
            wanted.add(str(c["ticker"]).strip().upper())
    created: list[str] = []
    for key in sorted(wanted):
        if get_cached_module("ticker", key):
            continue
        try:
            generate_module("ticker", key, prefer=prefer_module)
            created.append(key)
        except Exception:
            # One missing profile shouldn't fail the article write.
            continue
    return created


def generate_module(mtype: str, key: str, *, prefer: str = "deepseek") -> dict:
    with module_file_lock(mtype, key):
        cached = get_cached_module(mtype, key)
        if cached:
            return cached
        if mtype == "ticker":
            research = gather_research(f"{key} stock ticker company news", max_pages=3)
            prompt = MODULE_TICKER_PROMPT.format(key=key, research=research or "(no results)")
        elif mtype == "poster":
            research = gather_research(f'"@{key}" twitter x.com profile', max_pages=2)
            prompt = MODULE_POSTER_PROMPT.format(key=key, research=research or "(no results)")
        else:
            raise ValueError(f"Unknown module type: {mtype}")

        backend, raw = call_llm(prompt, prefer=prefer)
        parsed = parse_json_response(raw)
        if parsed is None:
            raise ValueError(f"Could not parse {backend} output for {mtype}/{key}: {raw[:300]}")
        parsed["type"] = mtype
        parsed["key"] = key
        parsed["generated_at"] = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
        parsed["backend"] = backend
        write_json_atomic(module_path(mtype, key), parsed)
        return parsed


def collect_modules_for_article(article: dict) -> dict:
    """Resolve all module references from an article into a dict keyed by `<type>_<slug>`."""
    out: dict[str, dict] = {}
    for tk in article.get("tickers") or []:
        m = get_cached_module("ticker", tk)
        if m:
            out[f"ticker_{slugify(tk)}"] = m
    poster = article.get("poster")
    if poster:
        m = get_cached_module("poster", poster)
        if m:
            out[f"poster_{slugify(poster)}"] = m
    return out


# --------------------------- CLI for manual use ---------------------------

def _cli() -> int:
    import argparse
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--archive", default=str(ROOT / "data/x-bookmarks.json"))
    p.add_argument("--bookmark-id")
    p.add_argument("--prefer", default="claude",
                   choices=("deepseek", "claude", "ollama"))
    p.add_argument("--module", help="Generate just a module: <type>:<key> (e.g. ticker:LITE)")
    args = p.parse_args()

    if args.module:
        mtype, _, key = args.module.partition(":")
        out = generate_module(mtype, key, prefer=args.prefer)
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return 0

    if not args.bookmark_id:
        print("Pass --bookmark-id <id> or --module <type>:<key>", file=sys.stderr)
        return 2
    archive = json.loads(Path(args.archive).read_text())
    bm = next((b for b in archive["bookmarks"] if str(b.get("id")) == args.bookmark_id), None)
    if not bm:
        print(f"Bookmark {args.bookmark_id} not found in {args.archive}", file=sys.stderr)
        return 3
    out = generate_article(bm, prefer=args.prefer)
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
