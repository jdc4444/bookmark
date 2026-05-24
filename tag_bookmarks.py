"""Tag every bookmark with topic labels via the Claude CLI (Haiku).

Multi-label: each bookmark gets a list of tags drawn from a curated taxonomy.
Idempotent: bookmarks already tagged are skipped unless --retag.
Parallel: runs N `claude -p` calls concurrently.
"""

import argparse
import concurrent.futures
import json
import re
import subprocess
import sys
import time
from pathlib import Path

from archive_io import write_archive_locked

# Curated taxonomy. Keep names short, lowercase, snake_case.
# Kept in sync with the consolidated tag set in the archive (see merges in
# the README / git history). When a bookmark genuinely doesn't fit any tag,
# emit "other".
TAGS = [
    "ai", "robotics", "3d_graphics",
    "programming", "hardware", "open_source",
    "startups", "business", "marketing", "productivity",
    "finance", "crypto",
    "science", "math",
    "philosophy", "spirituality", "psychology", "self_improvement",
    "health", "food", "travel",
    "art", "design", "music", "film", "writing", "gaming",
    "politics", "history", "education", "news",
    "humor", "personal_anecdote", "career",
    "other",
]

PROMPT_TEMPLATE = """You are a bookmark classifier. Output ONLY a JSON array of \
tag strings drawn from the allowed list. Multiple tags allowed when truly \
relevant. Return at least one tag, prefer 1-3 tags. Do not invent tags.

Allowed tags: {tags}

Tweet author: @{author}
Tweet text:
\"\"\"
{text}
\"\"\"
{ocr_section}
Output (JSON array only, no prose):"""


def build_prompt(bookmark: dict) -> str:
    text = (bookmark.get("text") or "").strip()
    author = ((bookmark.get("author") or {}).get("username") or "").strip() or "unknown"
    ocr_chunks = []
    for media in bookmark.get("media") or []:
        ocr = (media.get("ocr_text") or "").strip()
        if ocr:
            ocr_chunks.append(ocr)
    if ocr_chunks:
        merged = "\n---\n".join(ocr_chunks)[:3000]
        ocr_section = f"\nText extracted from attached image(s) via OCR:\n\"\"\"\n{merged}\n\"\"\"\n"
    else:
        ocr_section = ""
    return PROMPT_TEMPLATE.format(
        tags=", ".join(TAGS),
        text=text[:2000] or "(empty)",
        author=author,
        ocr_section=ocr_section,
    )


CODE_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def parse_tags(raw: str) -> list[str] | None:
    raw = raw.strip()
    if not raw:
        return None
    # Strip ```json ... ``` fences if present
    match = CODE_FENCE_RE.search(raw)
    if match:
        raw = match.group(1).strip()
    # If the response is multi-line, try to find the JSON array
    bracket = raw.find("[")
    if bracket >= 0:
        end = raw.rfind("]")
        if end > bracket:
            raw = raw[bracket : end + 1]
    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(result, list):
        return None
    cleaned = []
    seen = set()
    for tag in result:
        if not isinstance(tag, str):
            continue
        norm = tag.strip().lower().replace(" ", "_").replace("-", "_")
        if norm in TAGS and norm not in seen:
            seen.add(norm)
            cleaned.append(norm)
    return cleaned or None


def tag_one_claude(bookmark: dict, *, model: str, timeout: float) -> dict:
    prompt = build_prompt(bookmark)
    cmd = [
        "claude", "-p", prompt,
        "--model", model,
        "--permission-mode", "bypassPermissions",
        "--allowed-tools", "",
        "--output-format", "text",
    ]
    started = time.time()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"id": bookmark.get("id"), "ok": False, "error": "timeout",
                "elapsed": time.time() - started}
    elapsed = time.time() - started
    if proc.returncode != 0:
        return {"id": bookmark.get("id"), "ok": False,
                "error": f"exit_{proc.returncode}: {(proc.stderr or proc.stdout)[:300]}",
                "elapsed": elapsed}
    tags = parse_tags(proc.stdout)
    if tags is None:
        return {"id": bookmark.get("id"), "ok": False,
                "error": "parse_failed", "raw": proc.stdout[:300], "elapsed": elapsed}
    return {"id": bookmark.get("id"), "ok": True, "tags": tags, "elapsed": elapsed}


def tag_one_ollama(bookmark: dict, *, model: str, timeout: float,
                   url: str = "http://127.0.0.1:11434/api/generate") -> dict:
    import urllib.request
    prompt = build_prompt(bookmark)
    if "qwen3" in model.lower():
        # Qwen3 emits a long <think> block by default; suppress so the JSON array
        # comes out within budget.
        prompt = "/no_think " + prompt
    body = json.dumps({
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.0, "num_predict": 4000},
    }).encode("utf-8")
    started = time.time()
    try:
        req = urllib.request.Request(url, data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
    except Exception as exc:  # noqa: BLE001
        return {"id": bookmark.get("id"), "ok": False,
                "error": f"ollama_error: {exc}",
                "elapsed": time.time() - started}
    elapsed = time.time() - started
    raw = data.get("response", "")
    # Qwen3 may emit <think>...</think>; strip it.
    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
    tags = parse_tags(raw)
    if tags is None:
        return {"id": bookmark.get("id"), "ok": False,
                "error": "parse_failed", "raw": raw[:300], "elapsed": elapsed}
    return {"id": bookmark.get("id"), "ok": True, "tags": tags, "elapsed": elapsed}


def tag_one(bookmark: dict, *, backend: str, model: str, timeout: float) -> dict:
    if backend == "ollama":
        return tag_one_ollama(bookmark, model=model, timeout=timeout)
    return tag_one_claude(bookmark, model=model, timeout=timeout)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--archive", default="data/x-bookmarks.json")
    p.add_argument("--backend", choices=("claude", "ollama"), default="claude")
    p.add_argument("--model", default="claude-haiku-4-5-20251001",
                   help="claude model id or ollama tag (e.g. qwen3:30b-a3b).")
    p.add_argument("--workers", type=int, default=10)
    p.add_argument("--timeout", type=float, default=120.0)
    p.add_argument("--save-every", type=int, default=25)
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--ids", default="",
                   help="Comma-separated bookmark IDs to retag (overrides default skip-if-tagged).")
    p.add_argument("--retag", action="store_true",
                   help="Retag bookmarks that already have tags.")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    archive_path = Path(args.archive)
    payload = json.loads(archive_path.read_text())
    bookmarks = payload.get("bookmarks") or []

    explicit_ids = {s.strip() for s in (args.ids or "").split(",") if s.strip()}
    targets = []
    for bookmark in bookmarks:
        bid = str(bookmark.get("id") or "")
        if explicit_ids:
            if bid in explicit_ids:
                targets.append(bookmark)
            continue
        if not args.retag and isinstance(bookmark.get("tags"), list) and bookmark["tags"]:
            continue
        targets.append(bookmark)
    if args.limit:
        targets = targets[: args.limit]

    print(f"Tagging {len(targets)} bookmarks (out of {len(bookmarks)} total) "
          f"backend={args.backend} model={args.model} workers={args.workers}",
          flush=True)
    if not targets:
        return 0

    by_id = {str(b.get("id")): b for b in bookmarks if b.get("id")}
    started = time.time()
    completed = 0
    ok = 0
    failed = 0
    save_lock_n = 0

    def save():
        payload["bookmarks"] = list(by_id.values())
        write_archive_locked(archive_path, payload)

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as ex:
        future_to_bm = {
            ex.submit(tag_one, bm, backend=args.backend, model=args.model,
                      timeout=args.timeout): bm
            for bm in targets
        }
        for fut in concurrent.futures.as_completed(future_to_bm):
            bm = future_to_bm[fut]
            res = fut.result()
            completed += 1
            if res.get("ok"):
                bm["tags"] = res["tags"]
                ok += 1
            else:
                bm["tags_error"] = res.get("error", "unknown")
                failed += 1
            save_lock_n += 1
            if save_lock_n >= args.save_every:
                save()
                save_lock_n = 0
            if completed % 25 == 0 or completed == len(targets):
                elapsed = time.time() - started
                rate = completed / max(0.1, elapsed)
                eta = (len(targets) - completed) / max(0.01, rate)
                print(f"  [{completed}/{len(targets)}] ok={ok} failed={failed}  "
                      f"elapsed={elapsed:.0f}s  rate={rate:.1f}/s  eta={eta:.0f}s",
                      flush=True)

    save()
    print(f"\nDone. ok={ok} failed={failed} in {time.time()-started:.0f}s")

    # Summary: tag distribution
    counts: dict[str, int] = {}
    for bm in bookmarks:
        for t in (bm.get("tags") or []):
            counts[t] = counts.get(t, 0) + 1
    print("\nTag distribution (top 30):")
    for tag, count in sorted(counts.items(), key=lambda kv: -kv[1])[:30]:
        print(f"  {tag:20s}  {count}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
