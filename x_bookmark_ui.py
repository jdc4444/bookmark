#!/usr/bin/env python3
"""Local web UI for X Bookmark Reporter."""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.parse
import webbrowser
from collections import Counter
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from archive_io import archive_file_lock, write_json_atomic
import x_bookmark_reporter as reporter


ROOT = Path(__file__).resolve().parent
WEB_DIR = ROOT / "web"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787
ARTICLE_QUEUE_PATH = ROOT / "data" / "article_queue.json"
DEFAULT_OPENAI_ANALYSIS_PATH = "reports/openai-bookmark-analysis.json"
DEFAULT_BOOKMARK_ANALYSES_PATH = "reports/bookmark-analyses.json"
DEFAULT_OPENAI_MODEL = "gpt-4o-mini"
DEFAULT_PER_BOOKMARK_LIMIT = 5


def now_label() -> str:
    return reporter.iso_z()


def resolve_local_path(value: str | None, fallback: str) -> Path:
    raw = (value or fallback).strip() or fallback
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = ROOT / path
    return path


def as_int(value: Any, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def file_info(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"path": str(path), "exists": False}
    stat = path.stat()
    return {
        "path": str(path),
        "exists": True,
        "size": stat.st_size,
        "modified": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(stat.st_mtime)),
    }


def seed_archive_from_fresh_if_missing(archive_path: Path, fresh_path: Path) -> bool:
    """Initialize a missing archive from a fresh pull.

    Returns True when this call created the archive. If another refresh creates
    it first, leaves it untouched and returns False.
    """
    fresh_payload = reporter.read_json(fresh_path)
    fresh_payload["generated_at"] = now_label()
    with archive_file_lock(archive_path):
        if archive_path.exists():
            return False
        write_json_atomic(archive_path, fresh_payload, sort_keys=True)
    return True


def load_bookmark_ids(path: Path) -> set[str]:
    payload = reporter.read_json(path)
    return {
        str(bookmark.get("id"))
        for bookmark in (payload.get("bookmarks") or [])
        if bookmark.get("id") is not None
    }


def has_subagent_article(bookmark_id: str, archive_path: Path) -> bool:
    article_path = archive_path.parent / "articles" / f"article_{bookmark_id}.json"
    if not article_path.exists():
        return False
    try:
        article = json.loads(article_path.read_text(encoding="utf-8"))
    except Exception:
        return False
    return article.get("backend") == "claude-sonnet-subagent"


def has_article(bookmark_id: str, archive_path: Path) -> bool:
    article_path = archive_path.parent / "articles" / f"article_{bookmark_id}.json"
    return article_path.exists()


def bookmark_article_cutoff(archive: dict[str, Any], archive_path: Path) -> str:
    cutoff = ""
    for bookmark in archive.get("bookmarks") or []:
        bookmark_id = bookmark.get("id")
        if bookmark_id is None or not has_article(str(bookmark_id), archive_path):
            continue
        bookmark_time = bookmark.get("bookmarked_at") or bookmark.get("created_at") or ""
        if bookmark_time > cutoff:
            cutoff = bookmark_time
    return cutoff


def new_ids_missing_subagent_article(backup_or_archive_path: Path, archive_path: Path | None = None) -> list[str]:
    if archive_path is not None:
        before = load_bookmark_ids(backup_or_archive_path)
        archive = reporter.read_json(archive_path)
        selected: list[str] = []
        seen: set[str] = set()
        for bookmark in archive.get("bookmarks") or []:
            bookmark_id = bookmark.get("id")
            if bookmark_id is None:
                continue
            bid = str(bookmark_id)
            if bid in before or bid in seen or has_subagent_article(bid, archive_path):
                continue
            selected.append(bid)
            seen.add(bid)
        return selected

    archive_path = backup_or_archive_path
    archive = reporter.read_json(archive_path)
    cutoff = bookmark_article_cutoff(archive, archive_path)
    selected: list[str] = []
    seen: set[str] = set()
    for bookmark in archive.get("bookmarks") or []:
        bookmark_id = bookmark.get("id")
        if bookmark_id is None:
            continue
        bid = str(bookmark_id)
        bookmark_time = bookmark.get("bookmarked_at") or bookmark.get("created_at") or ""
        if cutoff and bookmark_time <= cutoff:
            continue
        if bid in seen or has_subagent_article(bid, archive_path):
            continue
        selected.append(bid)
        seen.add(bid)
    return selected


def article_queue_status() -> dict[str, Any]:
    if not ARTICLE_QUEUE_PATH.exists():
        return {
            "ids": [],
            "remaining_ids": [],
            "remaining_count": 0,
            "total_count": 0,
            "status": "idle",
        }
    try:
        payload = reporter.read_json(ARTICLE_QUEUE_PATH)
    except Exception:
        return {
            "ids": [],
            "remaining_ids": [],
            "remaining_count": 0,
            "total_count": 0,
            "status": "unknown",
        }
    ids = [str(item) for item in (payload.get("ids") or []) if str(item).strip()]
    remaining = [
        bookmark_id for bookmark_id in ids
        if not has_article(bookmark_id, ROOT / "data" / "x-bookmarks.json")
    ]
    running = subprocess.run(
        ["pgrep", "-f", rf"({re.escape(str(ROOT / 'upgrade_all.sh'))}|\./upgrade_all\.sh)"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    ).returncode == 0
    if remaining and running:
        status = "running"
    elif remaining:
        status = "stalled"
    elif ids:
        status = "done"
    else:
        status = "idle"
    return {
        **payload,
        "ids": ids,
        "remaining_ids": remaining,
        "remaining_count": len(remaining),
        "total_count": len(ids),
        "written_count": len(ids) - len(remaining),
        "status": status,
        "running": running,
    }


def write_article_queue(ids: list[str], run_dir: Path) -> None:
    ARTICLE_QUEUE_PATH.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(ARTICLE_QUEUE_PATH, {
        "started_at": now_label(),
        "updated_at": now_label(),
        "run_dir": str(run_dir),
        "ids": ids,
    }, sort_keys=True)


def dispatch_article_upgrade(ids: list[str]) -> bool:
    selected = [str(item) for item in ids if str(item).strip()]
    if not selected:
        return False
    guard = subprocess.run(
        ["pgrep", "-f", rf"({re.escape(str(ROOT / 'upgrade_all.sh'))}|\./upgrade_all\.sh)"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if guard.returncode == 0:
        return False

    run_dir = Path(tempfile.mkdtemp(prefix="bookmark-upgrade.", dir=os.environ.get("TMPDIR") or None))
    ids_file = run_dir / "ids.txt"
    ids_file.write_text("\n".join(selected) + "\n", encoding="utf-8")
    write_article_queue(selected, run_dir)
    log_path = ROOT / "data" / "upgrade.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["UPGRADE_RUN_DIR"] = str(run_dir)
    with log_path.open("a", encoding="utf-8") as log:
        subprocess.Popen(
            ["./upgrade_all.sh"],
            cwd=ROOT,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    return True


def token_info(path: Path) -> dict[str, Any]:
    info = file_info(path)
    if not info["exists"]:
        return info
    try:
        token = reporter.read_json(path)
    except Exception as exc:
        info["error"] = str(exc)
        return info
    info["saved_at"] = token.get("saved_at")
    info["expires_at"] = token.get("expires_at")
    if token.get("expires_at"):
        info["expires_at_iso"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(token["expires_at"])))
        info["expired"] = reporter.token_is_expired(token)
    info["has_refresh_token"] = bool(token.get("refresh_token"))
    return info


def archive_summary(path: Path) -> dict[str, Any]:
    info = file_info(path)
    if not info["exists"]:
        return info
    try:
        payload = reporter.read_json(path)
    except Exception as exc:
        info["error"] = str(exc)
        return info
    bookmarks = payload.get("bookmarks") or []
    categories: dict[str, int] = {}
    authors: set[str] = set()
    domains: set[str] = set()
    for bookmark in bookmarks:
        category = bookmark.get("category") or reporter.categorize_bookmark(bookmark)
        categories[category] = categories.get(category, 0) + 1
        author = bookmark.get("author") or {}
        if author.get("username"):
            authors.add(author["username"])
        for domain in reporter.bookmark_domains(bookmark):
            domains.add(domain)
    info.update(
        {
            "generated_at": payload.get("generated_at"),
            "bookmark_count": len(bookmarks),
            "author_count": len(authors),
            "domain_count": len(domains),
            "categories": sorted(
                [{"name": key, "count": value} for key, value in categories.items()],
                key=lambda item: item["count"],
                reverse=True,
            ),
        }
    )
    return info


def item_date(value: str | None) -> str:
    parsed = reporter.parse_datetime(value)
    return parsed.strftime("%Y-%m-%d") if parsed else ""


def month_label(value: str | None) -> str:
    parsed = reporter.parse_datetime(value)
    return parsed.strftime("%Y-%m") if parsed else "unknown"


def bookmark_key(bookmark: dict[str, Any]) -> str:
    return str(
        bookmark.get("id")
        or bookmark.get("url")
        or f"{bookmark.get('created_at', '')}:{(bookmark.get('text') or '')[:80]}"
    )


def bookmark_row(bookmark: dict[str, Any]) -> dict[str, Any]:
    author = bookmark.get("author") or {}
    category = bookmark.get("category") or reporter.categorize_bookmark(bookmark)
    status_context = bookmark.get("status_context") or {}
    media_full = bookmark.get("media") or status_context.get("media") or []
    # Pass through ocr_blocks (and other OCR fields) on the slimmed media list.
    media = []
    for m in media_full:
        item = {
            "type": m.get("type"),
            "url": m.get("url"),
            "alt": m.get("alt"),
            "width": m.get("width"),
            "height": m.get("height"),
        }
        if m.get("ocr_text"):
            item["ocr_text"] = m["ocr_text"]
        if m.get("ocr_blocks"):
            item["ocr_blocks"] = m["ocr_blocks"]
        media.append(item)
    replies = status_context.get("replies") or []
    tags = bookmark.get("tags") or []
    ocr_chunks = []
    for m in (bookmark.get("media") or []):
        text = (m.get("ocr_text") or "").strip()
        if text:
            ocr_chunks.append(text)
    ocr_text = "\n---\n".join(ocr_chunks)
    return {
        "id": bookmark_key(bookmark),
        "created_at": bookmark.get("created_at"),
        "bookmarked_at": bookmark.get("bookmarked_at"),
        "date": item_date(bookmark.get("created_at")),
        "month": month_label(bookmark.get("created_at")),
        "category": category,
        "tags": tags,
        "author": reporter.author_label(author),
        "author_name": author.get("name"),
        "username": author.get("username"),
        "url": bookmark.get("url"),
        "text": bookmark.get("text"),
        "score": reporter.engagement_score(bookmark),
        "metrics": bookmark.get("public_metrics") or {},
        "domains": reporter.bookmark_domains(bookmark),
        "url_count": len(bookmark.get("urls") or []),
        "media": media[:4],
        "media_count": len(media),
        "ocr_text": ocr_text,
        "ocr_lines": sum(1 for c in ocr_chunks for _ in c.splitlines()),
        "media_unavailable": bool(bookmark.get("media_unavailable")),
        "reply_context_count": len(replies),
        "quoted_tweet": bookmark.get("quoted_tweet"),
        "reply_to": bookmark.get("reply_to") or [],
        "thread": bookmark.get("thread") or [],
    }


def top_rows(counter: Counter, limit: int) -> list[dict[str, Any]]:
    return [{"name": name, "count": count} for name, count in counter.most_common(limit)]


def analysis_payload(path: Path) -> dict[str, Any]:
    payload = reporter.read_json(path)
    bookmarks = payload.get("bookmarks") or []
    rows = [bookmark_row(bookmark) for bookmark in bookmarks]
    rows.sort(key=lambda item: item.get("created_at") or "", reverse=True)

    category_counts = Counter(row["category"] for row in rows)
    author_counts = Counter(row["author"] for row in rows)
    domain_counts = Counter(domain for row in rows for domain in row["domains"])
    month_counts = Counter(row["month"] for row in rows if row["month"] != "unknown")
    linked_count = sum(1 for row in rows if row.get("url_count"))
    total_score = sum(row["score"] for row in rows)
    dates = [reporter.parse_datetime(row.get("created_at")) for row in rows]
    dates = [value for value in dates if value]

    high_signal = sorted(rows, key=lambda item: item["score"], reverse=True)[:18]
    recent = rows[:18]
    timeline = [{"month": month, "count": count} for month, count in sorted(month_counts.items())]

    return {
        "generated_at": payload.get("generated_at"),
        "source": payload.get("source"),
        "total": len(rows),
        "date_range": {
            "start": min(dates).strftime("%Y-%m-%d") if dates else "",
            "end": max(dates).strftime("%Y-%m-%d") if dates else "",
        },
        "stats": {
            "authors": len(author_counts),
            "domains": len(domain_counts),
            "linked": linked_count,
            "linked_share": linked_count / len(rows) if rows else 0,
            "total_score": total_score,
        },
        "categories": top_rows(category_counts, 14),
        "authors": top_rows(author_counts, 18),
        "domains": top_rows(domain_counts, 18),
        "timeline": timeline,
        "high_signal": high_signal,
        "recent": recent,
    }


def openai_cli_status() -> dict[str, Any]:
    cli_path = shutil.which("openai")
    return {
        "cli_path": cli_path,
        "available": bool(cli_path),
        "api_key_present": bool(os.getenv("OPENAI_API_KEY")),
        "model_default": os.getenv("OPENAI_MODEL", DEFAULT_OPENAI_MODEL),
    }


def codex_cli_path() -> str | None:
    bundled = Path("/Applications/Codex.app/Contents/Resources/codex")
    return shutil.which("codex") or (str(bundled) if bundled.exists() else None)


def codex_cli_status() -> dict[str, Any]:
    cli_path = codex_cli_path()
    return {
        "cli_path": cli_path,
        "available": bool(cli_path),
        "auth": "Codex app login",
        "model_default": "Codex CLI default",
    }


def compact_bookmark_for_openai(bookmark: dict[str, Any]) -> dict[str, Any]:
    row = bookmark_row(bookmark)
    metrics = row.get("metrics") or {}
    text = re.sub(r"\s+", " ", row.get("text") or "").strip()
    status_context = bookmark.get("status_context") or {}
    post_context = status_context.get("post") or {}
    replies = status_context.get("replies") or []
    media = bookmark.get("media") or status_context.get("media") or []
    linked_urls = [
        {
            "url": item.get("expanded_url") or item.get("url"),
            "title": item.get("title") or item.get("display_url") or "",
        }
        for item in bookmark.get("urls", []) or []
    ]
    return {
        "id": row.get("id"),
        "date": row.get("date"),
        "author": row.get("author"),
        "category_guess": row.get("category"),
        "score": round(float(row.get("score") or 0), 2),
        "metrics": {
            "likes": metrics.get("like_count", 0),
            "reposts": metrics.get("retweet_count", 0),
            "replies": metrics.get("reply_count", 0),
            "views": metrics.get("impression_count", 0),
        },
        "domains": row.get("domains") or [],
        "url": row.get("url"),
        "linked_urls": linked_urls[:8],
        "media": media[:8],
        "reply_context": {
            "captured_at": status_context.get("loaded_at"),
            "articles_seen": status_context.get("articles_seen", 0),
            "replies": [
                {
                    "author": reporter.author_label(reply.get("author") or {}),
                    "text": re.sub(r"\s+", " ", reply.get("text") or "").strip()[:500],
                    "metrics": reply.get("public_metrics") or {},
                    "url": reply.get("url"),
                }
                for reply in replies[:8]
            ],
        },
        "status_page_post_text": re.sub(r"\s+", " ", post_context.get("text") or "").strip()[:1200],
        "text": text[:700],
    }


def chunk_items_for_cli(items: list[dict[str, Any]], max_chars: int) -> list[list[dict[str, Any]]]:
    chunks: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_chars = 0
    for item in items:
        item_chars = len(json.dumps(item, ensure_ascii=False))
        if current and current_chars + item_chars > max_chars:
            chunks.append(current)
            current = []
            current_chars = 0
        current.append(item)
        current_chars += item_chars
    if current:
        chunks.append(current)
    return chunks


def extract_cli_content(output: str) -> str:
    output = output.strip()
    try:
        payload = json.loads(output)
        if isinstance(payload, dict):
            choices = payload.get("choices") or []
            if choices:
                message = choices[0].get("message") or {}
                if message.get("content"):
                    return str(message["content"]).strip()
                if choices[0].get("text"):
                    return str(choices[0]["text"]).strip()
    except json.JSONDecodeError:
        pass
    return output


def parse_jsonish(content: str) -> dict[str, Any]:
    content = content.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", content, re.S)
    if fence:
        content = fence.group(1).strip()
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        start = content.find("{")
        end = content.rfind("}")
        if start >= 0 and end > start:
            return json.loads(content[start : end + 1])
        raise


def openai_cli_chat(messages: list[tuple[str, str]], model: str, max_tokens: int) -> str:
    if not shutil.which("openai"):
        raise RuntimeError("The openai CLI is not installed or not on PATH.")
    if not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY is not set for the UI server. Set it, restart the UI, then run OpenAI analysis.")
    command = ["openai", "api", "chat.completions.create", "-m", model, "-M", str(max_tokens), "-t", "0"]
    for role, content in messages:
        command.extend(["-g", role, content])
    completed = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, check=False)
    if completed.returncode != 0:
        raise RuntimeError((completed.stderr or completed.stdout or "openai CLI failed").strip())
    return extract_cli_content(completed.stdout)


def codex_cli_prompt(prompt: str, *, search: bool = False, timeout: int = 300) -> str:
    cli_path = codex_cli_path()
    if not cli_path:
        raise RuntimeError("The Codex CLI is not installed or not on PATH.")
    with tempfile.NamedTemporaryFile("w+", encoding="utf-8", delete=True) as output_file:
        command = [cli_path]
        if search:
            command.append("--search")
        command.extend(
            [
                "exec",
                "--skip-git-repo-check",
                "--ephemeral",
                "--ignore-rules",
                "--ignore-user-config",
                "--sandbox",
                "read-only",
                "--output-last-message",
                output_file.name,
                "-",
            ]
        )
        try:
            completed = subprocess.run(
                command,
                cwd=ROOT,
                input=prompt,
                text=True,
                capture_output=True,
                check=False,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"Codex CLI timed out after {timeout} seconds while generating the article.") from exc
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "codex CLI failed").strip()
            detail = detail.replace(prompt, "[analysis prompt omitted]")
            if len(detail) > 2400:
                detail = "..." + detail[-2400:]
            raise RuntimeError(f"Codex CLI failed with exit code {completed.returncode}:\n{detail}")
        output_file.seek(0)
        content = output_file.read().strip()
        if not content:
            raise RuntimeError("Codex CLI did not produce an analysis response.")
        return content


def normalize_bookmark_analysis(data: dict[str, Any], bookmark: dict[str, Any], model: str) -> dict[str, Any]:
    row = bookmark_row(bookmark)
    normalized = dict(data) if isinstance(data, dict) else {}
    list_fields = (
        "topics",
        "key_points",
        "followups",
        "caveats",
        "implications",
        "counterpoints",
        "questions_to_watch",
        "notable_replies",
        "media_notes",
    )
    text_fields = (
        "headline",
        "summary",
        "why_saved",
        "utility",
        "quality_signal",
        "title",
        "dek",
        "article_markdown",
    )
    for field in text_fields:
        value = normalized.get(field)
        normalized[field] = str(value).strip() if value is not None else ""
    for field in list_fields:
        value = normalized.get(field)
        if isinstance(value, list):
            normalized[field] = [str(item).strip() for item in value if str(item).strip()]
        elif value:
            normalized[field] = [str(value).strip()]
        else:
            normalized[field] = []
    claim_check = normalized.get("claim_check")
    normalized["claim_check"] = claim_check if isinstance(claim_check, dict) else {}
    sources = normalized.get("sources")
    normalized["sources"] = sources if isinstance(sources, list) else []
    reply_context = normalized.get("reply_context")
    normalized["reply_context"] = reply_context if isinstance(reply_context, dict) else {}
    if normalized.get("title") and not normalized.get("headline"):
        normalized["headline"] = normalized["title"]
    if normalized.get("dek") and not normalized.get("summary"):
        normalized["summary"] = normalized["dek"]
    normalized.update(
        {
            "bookmark_id": row["id"],
            "date": row.get("date"),
            "author": row.get("author"),
            "username": row.get("username"),
            "url": row.get("url"),
            "category": row.get("category"),
            "generated_at": now_label(),
            "model": model,
            "source": "codex-cli",
        }
    )
    return normalized


def read_bookmark_analyses(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {
            "exists": False,
            "path": str(path),
            "analyses": {},
            "order": [],
            "status": codex_cli_status(),
        }
    payload = reporter.read_json(path)
    if not isinstance(payload, dict):
        payload = {"analyses": {}}
    payload.setdefault("analyses", {})
    payload.setdefault("order", list(payload["analyses"].keys()))
    payload["exists"] = True
    payload["_file"] = file_info(path)
    payload["_status"] = codex_cli_status()
    return payload


def write_bookmark_analyses(path: Path, payload: dict[str, Any]) -> None:
    payload["generated_at"] = now_label()
    reporter.write_json(path, payload)


def enrich_bookmark_for_article(bookmark: dict[str, Any], args: argparse.Namespace) -> None:
    if bookmark.get("status_context") and bookmark.get("status_context", {}).get("replies") is not None:
        return
    reply_limit = max(0, int(getattr(args, "reply_context_limit", 8) or 8))
    context_scrolls = max(1, int(getattr(args, "context_scrolls", 2) or 2))
    pause = max(0.2, float(getattr(args, "context_pause", 1.0) or 1.0))
    context = reporter.safari_extract_status_context(
        bookmark,
        reply_limit=reply_limit,
        scrolls=context_scrolls,
        pause=pause,
    )
    bookmark["status_context"] = context
    post_text = ((context.get("post") or {}).get("text") or "").strip()
    if post_text and len(post_text) > len((bookmark.get("text") or "").strip()) + 10:
        bookmark["text"] = reporter.clean_text(post_text)
        bookmark["text_full"] = True
        bookmark.pop("text_truncated", None)
    if context.get("media"):
        bookmark["media"] = context["media"]
    bookmark["context_enriched_at"] = now_label()


def _merge_enriched_media(
    latest_media: Any,
    enriched_media: Any,
) -> Any:
    if not isinstance(latest_media, list):
        return enriched_media
    if not isinstance(enriched_media, list):
        return latest_media

    merged = [dict(item) if isinstance(item, dict) else item for item in latest_media]
    index_by_url = {
        item.get("url"): index
        for index, item in enumerate(merged)
        if isinstance(item, dict) and item.get("url")
    }
    for item in enriched_media:
        if not isinstance(item, dict):
            merged.append(item)
            continue
        url = item.get("url")
        if not url or url not in index_by_url:
            merged.append(dict(item))
            if url:
                index_by_url[url] = len(merged) - 1
            continue
        index = index_by_url[url]
        latest = merged[index]
        if not isinstance(latest, dict):
            merged.append(dict(item))
            continue
        out = dict(latest)
        out.update(item)
        for key, value in latest.items():
            if key.startswith("ocr_") and item.get(key) in (None, "", [], {}):
                out[key] = value
        merged[index] = out
    return merged


def merge_bookmark_enrichment_into_archive(
    archive_path: Path,
    enriched_bookmark: dict[str, Any],
) -> None:
    """Merge Safari enrichment for one bookmark into the latest archive."""
    target_key = bookmark_key(enriched_bookmark)
    enrichment: dict[str, Any] = {}
    if "status_context" in enriched_bookmark:
        enrichment["status_context"] = enriched_bookmark.get("status_context")
    if "context_enriched_at" in enriched_bookmark:
        enrichment["context_enriched_at"] = enriched_bookmark.get("context_enriched_at")
    if enriched_bookmark.get("text_full") and enriched_bookmark.get("text"):
        enrichment["text"] = enriched_bookmark.get("text")
        enrichment["text_full"] = True
    if enriched_bookmark.get("media"):
        enrichment["media"] = enriched_bookmark.get("media")

    with archive_file_lock(archive_path):
        latest_payload = reporter.read_json(archive_path)
        latest_bookmarks = latest_payload.get("bookmarks") or []
        for latest_bookmark in latest_bookmarks:
            if bookmark_key(latest_bookmark) != target_key:
                continue
            if "media" in enrichment:
                enrichment["media"] = _merge_enriched_media(
                    latest_bookmark.get("media"),
                    enrichment["media"],
                )
            if enrichment.get("text_full"):
                latest_bookmark.pop("text_truncated", None)
            latest_bookmark.update(enrichment)
            write_json_atomic(archive_path, latest_payload, sort_keys=True)
            return


def run_codex_cli_bookmark_analyses(args: argparse.Namespace) -> dict[str, Any]:
    archive_path = Path(args.json)
    payload = reporter.read_json(archive_path)
    bookmarks = payload.get("bookmarks") or []
    if not bookmarks:
        raise RuntimeError("Bookmark archive is empty. Import bookmarks first.")

    model = "Codex CLI default"
    limit = max(1, int(getattr(args, "per_bookmark_limit", DEFAULT_PER_BOOKMARK_LIMIT) or DEFAULT_PER_BOOKMARK_LIMIT))
    output_path = Path(getattr(args, "bookmark_analyses", None) or ROOT / DEFAULT_BOOKMARK_ANALYSES_PATH)
    if not output_path.is_absolute():
        output_path = ROOT / output_path

    selected = sorted(bookmarks, key=lambda item: item.get("created_at") or "", reverse=True)[:limit]
    existing = read_bookmark_analyses(output_path) if output_path.exists() else {}
    analyses = existing.get("analyses") if isinstance(existing.get("analyses"), dict) else {}
    order = [bookmark_key(bookmark) for bookmark in selected]
    output: dict[str, Any] = {
        "generated_at": existing.get("generated_at") or now_label(),
        "source": "codex-cli",
        "model": model,
        "archive_generated_at": payload.get("generated_at"),
        "analyzed_limit": limit,
        "order": order,
        "analyses": dict(analyses),
    }

    for index, bookmark in enumerate(selected, start=1):
        print(f"Pulling media and replies for bookmark {index}/{len(selected)}...")
        try:
            needs_enrichment = not (
                bookmark.get("status_context")
                and bookmark.get("status_context", {}).get("replies") is not None
            )
            enrich_bookmark_for_article(bookmark, args)
            if needs_enrichment:
                merge_bookmark_enrichment_into_archive(archive_path, bookmark)
        except Exception as exc:
            print(f"Could not enrich bookmark {bookmark_key(bookmark)} from Safari: {exc}")
        row = compact_bookmark_for_openai(bookmark)
        print(f"Writing article {index}/{len(selected)}: {row.get('author') or row.get('id')}")
        prompt = {
            "instructions": [
                "Use this private X bookmark as the starting point for a short article.",
                "This is not a post analysis. Write a useful article that develops the idea in the post.",
                "Validate the central factual claim with web search when it is factual, numeric, financial, scientific, or current.",
                "If the claim cannot be verified from reputable sources, say that clearly instead of treating it as true.",
                "Use any captured media, links, and replies as context, but do not assume replies are authoritative.",
                "Return strict JSON only with no markdown fences.",
                "Cite sources with URLs in the sources array.",
                "Keep the article concise, concrete, and written for an intellectually curious reader.",
            ],
            "task": "Write a short article seeded by this bookmark, including claim validation, implications, and why it matters.",
            "required_json_shape": {
                "title": "article title, not just a restatement of the post",
                "dek": "one-sentence standfirst",
                "claim_check": {
                    "claim": "central factual claim being checked",
                    "verdict": "true | mostly true | mixed | unverified | false | not applicable",
                    "evidence": "brief explanation with dates/figures when possible",
                },
                "article_markdown": "500-900 word article with short sections and no front matter",
                "implications": ["important implications if the claim or frame matters"],
                "counterpoints": ["reasons to be cautious or alternative interpretations"],
                "questions_to_watch": ["what to monitor next"],
                "reply_context": {
                    "summary": "what replies add, if captured",
                    "useful_signals": ["notable reply signals or disagreements"],
                },
                "media_notes": ["what attached media contributes, if any"],
                "sources": [{"title": "source title", "url": "https://...", "note": "what it supports"}],
                "topics": ["3-6 short topic tags"],
            },
            "bookmark": row,
        }
        content = codex_cli_prompt(
            "Return only valid JSON for the following analysis request:\n"
            + json.dumps(prompt, ensure_ascii=False, indent=2),
            search=True,
            timeout=300,
        )
        analysis = normalize_bookmark_analysis(parse_jsonish(content), bookmark, model)
        output["analyses"][bookmark_key(bookmark)] = analysis
        write_bookmark_analyses(output_path, output)

    return {
        "bookmark_analyses": file_info(output_path),
        "meta": {
            "generated_at": output["generated_at"],
            "model": model,
            "analyzed_count": len(selected),
            "source": "codex-cli",
        },
    }


def run_openai_cli_analysis(args: argparse.Namespace) -> dict[str, Any]:
    archive_path = Path(args.json)
    payload = reporter.read_json(archive_path)
    bookmarks = payload.get("bookmarks") or []
    if not bookmarks:
        raise RuntimeError("Bookmark archive is empty. Import bookmarks first.")

    model = getattr(args, "openai_model", None) or os.getenv("OPENAI_MODEL", DEFAULT_OPENAI_MODEL)
    max_bookmarks = max(1, int(getattr(args, "openai_max_bookmarks", 1200) or 1200))
    chunk_chars = max(12000, int(getattr(args, "openai_chunk_chars", 42000) or 42000))
    output_path = Path(getattr(args, "openai_analysis", None) or ROOT / DEFAULT_OPENAI_ANALYSIS_PATH)
    if not output_path.is_absolute():
        output_path = ROOT / output_path

    compact = [compact_bookmark_for_openai(bookmark) for bookmark in bookmarks[:max_bookmarks]]
    chunks = chunk_items_for_cli(compact, chunk_chars)
    chunk_notes = []
    system = (
        "You analyze a private X bookmarks archive. Return strict JSON only. "
        "Do not mention implementation details. Be specific, concrete, and useful."
    )
    for index, chunk in enumerate(chunks, start=1):
        prompt = {
            "task": "Analyze this chunk of X bookmarks.",
            "chunk": index,
            "chunk_count": len(chunks),
            "required_json_shape": {
                "dominant_interests": ["short labels"],
                "notable_patterns": ["specific observations"],
                "signals": ["high-signal saved items or clusters"],
                "authors_or_sources": ["authors or domains worth revisiting"],
                "questions": ["questions this bookmark set suggests"],
            },
            "bookmarks": chunk,
        }
        content = openai_cli_chat(
            [("system", system), ("user", json.dumps(prompt, ensure_ascii=False))],
            model=model,
            max_tokens=1300,
        )
        chunk_notes.append(parse_jsonish(content))

    final_prompt = {
        "task": "Synthesize the chunk analyses into one product-ready dashboard analysis for the user.",
        "archive": {
            "bookmark_count": len(bookmarks),
            "analyzed_count": len(compact),
            "generated_at": payload.get("generated_at"),
            "source": payload.get("source"),
        },
        "required_json_shape": {
            "headline": "one concise title",
            "summary": "2-4 sentence executive read",
            "interest_clusters": [{"name": "cluster", "share_estimate": "qualitative", "takeaway": "specific takeaway"}],
            "behavioral_patterns": ["what the saves imply about attention/workflow/taste"],
            "high_signal_threads": [{"title": "thread", "why_it_matters": "reason"}],
            "blind_spots": ["missing or underrepresented areas"],
            "next_actions": ["concrete actions for the user"],
            "search_prompts": ["useful search prompts to explore this archive"],
            "caveats": ["data limitations"],
        },
        "chunk_analyses": chunk_notes,
    }
    content = openai_cli_chat(
        [("system", system), ("user", json.dumps(final_prompt, ensure_ascii=False))],
        model=model,
        max_tokens=2200,
    )
    analysis = parse_jsonish(content)
    analysis["_meta"] = {
        "generated_at": now_label(),
        "model": model,
        "bookmark_count": len(bookmarks),
        "analyzed_count": len(compact),
        "chunk_count": len(chunks),
        "source": "openai-cli",
    }
    reporter.write_json(output_path, analysis)
    return {"analysis": file_info(output_path), "meta": analysis["_meta"]}


def read_openai_analysis(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"exists": False, "path": str(path), "status": openai_cli_status()}
    payload = reporter.read_json(path)
    payload["_file"] = file_info(path)
    payload["_status"] = openai_cli_status()
    return payload


class Job:
    def __init__(self, kind: str) -> None:
        self.id = f"{int(time.time())}-{kind}"
        self.kind = kind
        self.status = "running"
        self.started_at = now_label()
        self.ended_at: str | None = None
        self.logs: list[str] = []
        self.error: str | None = None
        self.result: dict[str, Any] = {}
        self.lock = threading.Lock()

    def log(self, text: str) -> None:
        for line in text.splitlines():
            if line.strip():
                with self.lock:
                    self.logs.append(f"[{time.strftime('%H:%M:%S')}] {line.strip()}")
                    self.logs = self.logs[-400:]

    def complete(self, result: dict[str, Any] | None = None) -> None:
        with self.lock:
            self.status = "completed"
            self.ended_at = now_label()
            self.result = result or {}

    def fail(self, exc: BaseException) -> None:
        with self.lock:
            self.status = "failed"
            self.ended_at = now_label()
            self.error = str(exc)
            self.logs.append(traceback.format_exc())

    def as_dict(self) -> dict[str, Any]:
        with self.lock:
            return {
                "id": self.id,
                "kind": self.kind,
                "status": self.status,
                "started_at": self.started_at,
                "ended_at": self.ended_at,
                "logs": list(self.logs),
                "error": self.error,
                "result": self.result,
            }


class JobStream:
    def __init__(self, job: Job, original: Any) -> None:
        self.job = job
        self.original = original
        self.buffer = ""

    def write(self, text: str) -> int:
        self.original.write(text)
        self.original.flush()
        self.buffer += text
        while "\n" in self.buffer:
            line, self.buffer = self.buffer.split("\n", 1)
            self.job.log(line)
        return len(text)

    def flush(self) -> None:
        self.original.flush()
        if self.buffer.strip():
            self.job.log(self.buffer)
            self.buffer = ""


class AppState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.job: Job | None = None

    def active_job(self) -> Job | None:
        with self.lock:
            if self.job and self.job.status == "running":
                return self.job
            return None

    def set_job(self, job: Job) -> None:
        with self.lock:
            self.job = job

    def job_dict(self) -> dict[str, Any] | None:
        with self.lock:
            return self.job.as_dict() if self.job else None


STATE = AppState()


def settings_to_namespace(settings: dict[str, Any]) -> argparse.Namespace:
    json_path = resolve_local_path(settings.get("jsonPath"), "data/x-bookmarks.json")
    csv_path = resolve_local_path(settings.get("csvPath"), "data/x-bookmarks.csv")
    report_path = resolve_local_path(settings.get("reportPath"), "reports/x-bookmark-report.md")
    openai_analysis_path = resolve_local_path(settings.get("openaiAnalysisPath"), DEFAULT_OPENAI_ANALYSIS_PATH)
    bookmark_analyses_path = resolve_local_path(settings.get("bookmarkAnalysesPath"), DEFAULT_BOOKMARK_ANALYSES_PATH)
    token_path = resolve_local_path(settings.get("tokenFile"), ".x_tokens.json")
    csv_value = str(csv_path) if settings.get("csvPath", "data/x-bookmarks.csv") != "" else None
    return argparse.Namespace(
        client_id=settings.get("clientId") or None,
        client_secret=settings.get("clientSecret") or None,
        redirect_uri=settings.get("redirectUri") or reporter.DEFAULT_REDIRECT_URI,
        token_file=str(token_path),
        scopes=settings.get("scopes") or " ".join(reporter.DEFAULT_SCOPES),
        no_interactive_auth=bool(settings.get("noInteractiveAuth", False)),
        no_wait_rate_limit=bool(settings.get("noWaitRateLimit", False)),
        max_rate_limit_sleep=as_int(settings.get("maxRateLimitSleep"), 20 * 60),
        user_id=settings.get("userId") or None,
        output=str(json_path),
        json=str(json_path),
        csv=csv_value,
        report=str(report_path),
        index_limit=as_int(settings.get("indexLimit"), 200),
        limit=as_int(settings.get("limit"), 0),
        max_results=as_int(settings.get("maxResults"), 100),
        pagination_token=settings.get("paginationToken") or None,
        max_scrolls=as_int(settings.get("maxScrolls"), 400),
        idle_rounds=as_int(settings.get("idleRounds"), 8),
        scroll_pause=float(settings.get("scrollPause") or 0.85),
        scroll_step=float(settings.get("scrollStep") or 0.85),
        openai_analysis=str(openai_analysis_path),
        bookmark_analyses=str(bookmark_analyses_path),
        per_bookmark_limit=as_int(settings.get("perBookmarkLimit"), DEFAULT_PER_BOOKMARK_LIMIT),
        reply_context_limit=as_int(settings.get("replyContextLimit"), 8),
        context_scrolls=as_int(settings.get("contextScrolls"), 2),
        context_pause=float(settings.get("contextPause") or 1.0),
        openai_model=settings.get("openaiModel") or os.getenv("OPENAI_MODEL", DEFAULT_OPENAI_MODEL),
        openai_max_bookmarks=as_int(settings.get("openaiMaxBookmarks"), 1200),
        openai_chunk_chars=as_int(settings.get("openaiChunkChars"), 42000),
    )


def run_in_job(kind: str, settings: dict[str, Any], action: str) -> Job:
    if STATE.active_job():
        raise RuntimeError("A job is already running.")
    job = Job(kind)
    STATE.set_job(job)

    def worker() -> None:
        stream = JobStream(job, sys.stderr)
        out_stream = JobStream(job, sys.stdout)
        try:
            args = settings_to_namespace(settings)
            job.log(f"Starting {kind}.")
            with contextlib.redirect_stderr(stream), contextlib.redirect_stdout(out_stream):
                if action == "auth":
                    reporter.authorize(
                        client_id=reporter.require_client_id(args),
                        client_secret=args.client_secret,
                        redirect_uri=args.redirect_uri,
                        scopes=reporter.parse_scopes(args.scopes),
                        token_file=Path(args.token_file),
                    )
                    result = {"token": token_info(Path(args.token_file))}
                elif action == "run":
                    payload = reporter.fetch_bookmarks(args)
                    reporter.write_report_from_payload(payload, Path(args.report), args.index_limit)
                    result = {
                        "bookmark_count": len(payload.get("bookmarks") or []),
                        "json": file_info(Path(args.json)),
                        "csv": file_info(Path(args.csv)) if args.csv else None,
                        "report": file_info(Path(args.report)),
                    }
                elif action == "safari":
                    payload = reporter.import_bookmarks_from_safari(args)
                    reporter.write_report_from_payload(payload, Path(args.report), args.index_limit)
                    result = {
                        "bookmark_count": len(payload.get("bookmarks") or []),
                        "json": file_info(Path(args.json)),
                        "csv": file_info(Path(args.csv)) if args.csv else None,
                        "report": file_info(Path(args.report)),
                    }
                elif action == "report":
                    payload = reporter.read_json(Path(args.json))
                    reporter.write_report_from_payload(payload, Path(args.report), args.index_limit)
                    result = {"report": file_info(Path(args.report))}
                elif action == "openai":
                    result = run_openai_cli_analysis(args)
                elif action == "bookmark_codex":
                    result = run_codex_cli_bookmark_analyses(args)
                else:
                    raise RuntimeError(f"Unknown job action: {action}")
            stream.flush()
            out_stream.flush()
            job.log(f"Finished {kind}.")
            job.complete(result)
        except BaseException as exc:
            stream.flush()
            out_stream.flush()
            job.fail(exc)

    threading.Thread(target=worker, daemon=True).start()
    return job


def default_config() -> dict[str, Any]:
    return {
        "clientId": "",
        "clientSecret": "",
        "redirectUri": reporter.DEFAULT_REDIRECT_URI,
        "tokenFile": ".x_tokens.json",
        "scopes": " ".join(reporter.DEFAULT_SCOPES),
        "jsonPath": "data/x-bookmarks.json",
        "csvPath": "data/x-bookmarks.csv",
        "reportPath": "reports/x-bookmark-report.md",
        "limit": 0,
        "indexLimit": 200,
        "maxResults": 100,
        "maxRateLimitSleep": 1200,
        "maxScrolls": 400,
        "idleRounds": 8,
        "scrollPause": 0.85,
        "scrollStep": 0.85,
        "openaiModel": os.getenv("OPENAI_MODEL", DEFAULT_OPENAI_MODEL),
        "openaiMaxBookmarks": 1200,
        "openaiChunkChars": 42000,
        "openaiAnalysisPath": DEFAULT_OPENAI_ANALYSIS_PATH,
        "perBookmarkLimit": DEFAULT_PER_BOOKMARK_LIMIT,
        "replyContextLimit": 8,
        "contextScrolls": 2,
        "contextPause": 1.0,
        "bookmarkAnalysesPath": DEFAULT_BOOKMARK_ANALYSES_PATH,
        "envClientIdPresent": bool(reporter.os.getenv("X_CLIENT_ID")),
        "envClientSecretPresent": bool(reporter.os.getenv("X_CLIENT_SECRET")),
        "openaiCli": openai_cli_status(),
        "codexCli": codex_cli_status(),
    }


def status_payload(settings: dict[str, Any] | None = None) -> dict[str, Any]:
    settings = settings or default_config()
    args = settings_to_namespace(settings)
    return {
        "time": now_label(),
        "token": token_info(Path(args.token_file)),
        "archive": archive_summary(Path(args.json)),
        "files": {
            "json": file_info(Path(args.json)),
            "csv": file_info(Path(args.csv)) if args.csv else {"exists": False, "path": ""},
            "report": file_info(Path(args.report)),
            "openai_analysis": file_info(Path(args.openai_analysis)),
            "bookmark_analyses": file_info(Path(args.bookmark_analyses)),
        },
        "openai": openai_cli_status(),
        "codex": codex_cli_status(),
        "job": STATE.job_dict(),
    }


def read_bookmarks(path: Path, query: str = "", category: str = "",
                   tags: list[str] | None = None,
                   tag_mode: str = "any",
                   media_filter: str = "",
                   sort: str = "bookmarked-recent",
                   limit: int = 250) -> dict[str, Any]:
    payload = reporter.read_json(path)
    bookmarks = payload.get("bookmarks") or []
    query_lc = query.lower().strip()
    category_lc = category.lower().strip()
    tags = [t.lower().strip() for t in (tags or []) if t]
    tag_set = set(tags)
    media_filter = (media_filter or "").lower().strip()

    # When the user is searching, also fold any cached editorial brief into
    # the haystack so headlines/lede/body are searchable. Loaded lazily, once
    # per request — skipped entirely when there's no query.
    article_index: dict[str, str] = {}
    if query_lc:
        articles_dir = path.parent / "articles"
        if articles_dir.exists():
            for f in articles_dir.iterdir():
                if not (f.is_file() and f.name.startswith("article_") and f.name.endswith(".json")):
                    continue
                bid = f.name[len("article_"):-len(".json")]
                try:
                    a = json.loads(f.read_text())
                except Exception:
                    continue
                parts = [a.get("headline") or "", a.get("lede") or "", a.get("body") or ""]
                for c in (a.get("concepts") or []):
                    if isinstance(c, dict):
                        parts.append(c.get("term") or c.get("key") or "")
                        parts.append(c.get("definition") or "")
                article_index[bid] = " ".join(parts).lower()

    rows = []
    categories = set()
    tag_counts: dict[str, int] = {}
    media_with_count = 0
    media_without_count = 0
    ocr_with_count = 0
    for bookmark in bookmarks:
        bookmark_category = bookmark.get("category") or reporter.categorize_bookmark(bookmark)
        categories.add(bookmark_category)
        bm_tags = [t.lower() for t in (bookmark.get("tags") or [])]
        for t in bm_tags:
            tag_counts[t] = tag_counts.get(t, 0) + 1
        author = bookmark.get("author") or {}
        text = bookmark.get("text") or ""
        ocr_text = " ".join(
            (m.get("ocr_text") or "") for m in (bookmark.get("media") or [])
        )
        haystack = " ".join([
            text, ocr_text, author.get("username") or "",
            author.get("name") or "", bookmark_category,
            " ".join(bm_tags),
            article_index.get(str(bookmark.get("id")), ""),
        ]).lower()
        if query_lc and query_lc not in haystack:
            continue
        if category_lc and bookmark_category.lower() != category_lc:
            continue
        if tag_set:
            bm_tag_set = set(bm_tags)
            if tag_mode == "all":
                if not tag_set.issubset(bm_tag_set):
                    continue
            else:  # any
                if not (tag_set & bm_tag_set):
                    continue
        has_media = bool(bookmark.get("media"))
        has_ocr = any((m.get("ocr_text") or "").strip()
                      for m in (bookmark.get("media") or []))
        if media_filter == "media" and not has_media:
            continue
        if media_filter == "no_media" and has_media:
            continue
        if media_filter == "ocr" and not has_ocr:
            continue
        if has_media:
            media_with_count += 1
        else:
            media_without_count += 1
        if has_ocr:
            ocr_with_count += 1
        rows.append(bookmark_row(bookmark))

    def bookmarked_sort_key(row: dict[str, Any]) -> str:
        return row.get("bookmarked_at") or row.get("created_at") or ""

    if sort == "engagement":
        rows.sort(key=lambda r: r.get("score") or 0, reverse=True)
    elif sort in ("post-oldest", "oldest"):
        rows.sort(key=lambda r: r.get("created_at") or "")
    elif sort in ("post-recent", "recent"):
        rows.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    elif sort == "bookmarked-oldest":
        rows.sort(key=bookmarked_sort_key)
    else:  # bookmarked-recent (default)
        rows.sort(key=bookmarked_sort_key, reverse=True)

    row_limit = max(0, min(limit, 50000))
    visible_rows = rows if row_limit == 0 else rows[:row_limit]
    return {
        "generated_at": payload.get("generated_at"),
        "total": len(bookmarks),
        "matched": len(rows),
        "categories": sorted(categories),
        "tag_counts": tag_counts,
        "summary": {
            "with_media": media_with_count,
            "without_media": media_without_count,
            "with_ocr": ocr_with_count,
        },
        "rows": visible_rows,
    }


class UIHandler(BaseHTTPRequestHandler):
    server_version = "XBookmarkReporterUI/1.0"

    def do_GET(self) -> None:  # noqa: N802
        try:
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path.startswith("/api/"):
                self.handle_api_get(parsed)
                return
            self.serve_static(parsed.path)
        except Exception as exc:
            self.write_error(exc)

    def do_POST(self) -> None:  # noqa: N802
        try:
            parsed = urllib.parse.urlparse(self.path)
            if not parsed.path.startswith("/api/"):
                self.write_json({"error": "Not found"}, HTTPStatus.NOT_FOUND)
                return
            payload = self.read_json_body()
            if parsed.path == "/api/auth":
                job = run_in_job("authorization", payload, "auth")
                self.write_json({"job": job.as_dict()})
                return
            if parsed.path == "/api/run":
                job = run_in_job("fetch and report", payload, "run")
                self.write_json({"job": job.as_dict()})
                return
            if parsed.path == "/api/safari-import":
                job = run_in_job("Safari import", payload, "safari")
                self.write_json({"job": job.as_dict()})
                return
            if parsed.path == "/api/report":
                job = run_in_job("report rebuild", payload, "report")
                self.write_json({"job": job.as_dict()})
                return
            if parsed.path == "/api/openai-analyze":
                job = run_in_job("OpenAI CLI analysis", payload, "openai")
                self.write_json({"job": job.as_dict()})
                return
            if parsed.path == "/api/analyze-bookmarks":
                job = run_in_job("per-bookmark Codex CLI analysis", payload, "bookmark_codex")
                self.write_json({"job": job.as_dict()})
                return
            if parsed.path == "/api/article":
                self.handle_article_generate(payload)
                return
            if parsed.path == "/api/refresh":
                self.handle_refresh()
                return
            self.write_json({"error": "Not found"}, HTTPStatus.NOT_FOUND)
        except RuntimeError as exc:
            self.write_json({"error": str(exc)}, HTTPStatus.CONFLICT)
        except Exception as exc:
            self.write_error(exc)

    def handle_api_get(self, parsed: urllib.parse.ParseResult) -> None:
        params = urllib.parse.parse_qs(parsed.query)
        if parsed.path == "/api/config":
            self.write_json(default_config())
            return
        if parsed.path == "/api/status":
            settings = {}
            if params.get("settings"):
                settings = json.loads(params["settings"][0])
            self.write_json(status_payload(settings))
            return
        if parsed.path == "/api/report":
            report_path = resolve_local_path(params.get("path", ["reports/x-bookmark-report.md"])[0], "reports/x-bookmark-report.md")
            if not report_path.exists():
                self.write_json({"error": "Report does not exist yet.", "path": str(report_path)}, HTTPStatus.NOT_FOUND)
                return
            self.write_json({"path": str(report_path), "markdown": report_path.read_text(encoding="utf-8")})
            return
        if parsed.path == "/api/analysis":
            json_path = resolve_local_path(params.get("path", ["data/x-bookmarks.json"])[0], "data/x-bookmarks.json")
            if not json_path.exists():
                self.write_json({"error": "Bookmark archive does not exist yet.", "path": str(json_path)}, HTTPStatus.NOT_FOUND)
                return
            self.write_json(analysis_payload(json_path))
            return
        if parsed.path == "/api/openai-analysis":
            analysis_path = resolve_local_path(
                params.get("path", [DEFAULT_OPENAI_ANALYSIS_PATH])[0],
                DEFAULT_OPENAI_ANALYSIS_PATH,
            )
            self.write_json(read_openai_analysis(analysis_path))
            return
        if parsed.path == "/api/bookmark-analyses":
            analysis_path = resolve_local_path(
                params.get("path", [DEFAULT_BOOKMARK_ANALYSES_PATH])[0],
                DEFAULT_BOOKMARK_ANALYSES_PATH,
            )
            self.write_json(read_bookmark_analyses(analysis_path))
            return
        if parsed.path == "/api/bookmarks":
            json_path = resolve_local_path(params.get("path", ["data/x-bookmarks.json"])[0], "data/x-bookmarks.json")
            if not json_path.exists():
                self.write_json({"error": "Bookmark archive does not exist yet.", "path": str(json_path)}, HTTPStatus.NOT_FOUND)
                return
            tags_param = params.get("tags", [""])[0]
            tag_list = [t for t in tags_param.split(",") if t.strip()]
            self.write_json(
                read_bookmarks(
                    json_path,
                    query=params.get("query", [""])[0],
                    category=params.get("category", [""])[0],
                    tags=tag_list,
                    tag_mode=params.get("tag_mode", ["any"])[0],
                    media_filter=params.get("media", [""])[0],
                    sort=params.get("sort", ["bookmarked-recent"])[0],
                    limit=as_int(params.get("limit", [250])[0], 250),
                )
            )
            return
        if parsed.path == "/api/articles":
            json_path = resolve_local_path(params.get("path", ["data/x-bookmarks.json"])[0], "data/x-bookmarks.json")
            archive = reporter.read_json(json_path) if json_path.exists() else {"bookmarks": []}
            bm_by_id: dict[str, dict] = {str(b.get("id")): b for b in (archive.get("bookmarks") or [])}
            articles_dir = json_path.parent / "articles"
            out: list[dict[str, Any]] = []
            if articles_dir.exists():
                for f in articles_dir.iterdir():
                    if not (f.is_file() and f.name.startswith("article_") and f.name.endswith(".json")):
                        continue
                    bid = f.name[len("article_"):-len(".json")]
                    try:
                        a = json.loads(f.read_text())
                    except Exception:
                        continue
                    bm = bm_by_id.get(bid) or {}
                    # First media of any kind that has a URL (image OR video
                    # thumbnail). When nothing is available, the front-end
                    # uses the tweet text as typographic key art.
                    image_url = None
                    for m in (bm.get("media") or []):
                        u = m.get("url")
                        if u:
                            image_url = u
                            break
                    out.append({
                        "bookmark_id": bid,
                        "headline": a.get("headline") or "",
                        "lede": a.get("lede") or "",
                        "poster": a.get("poster") or ((bm.get("author") or {}).get("username") or ""),
                        "poster_name": (bm.get("author") or {}).get("name") or "",
                        "generated_at": a.get("generated_at") or "",
                        "backend": a.get("backend") or "",
                        "tags": bm.get("tags") or [],
                        "tickers": a.get("tickers") or [],
                        "created_at": bm.get("created_at"),
                        "bookmarked_at": bm.get("bookmarked_at"),
                        "image_url": image_url,
                        "tweet_text": bm.get("text") or "",
                        "quoted_tweet": bm.get("quoted_tweet"),
                        "reply_to": bm.get("reply_to") or [],
                        "thread": bm.get("thread") or [],
                    })
            self.write_json({"articles": out})
            return
        if parsed.path == "/api/index":
            # Aggregate every entity referenced across all written articles:
            # tickers, companies, concepts, books, repos. Each entry includes
            # the article ids that mention it so the UI can filter / link to
            # them, and (where available) the cached module/profile so the
            # index page can render a definition without an extra fetch.
            json_path = resolve_local_path(params.get("path", ["data/x-bookmarks.json"])[0], "data/x-bookmarks.json")
            articles_dir = json_path.parent / "articles"
            modules_dir = json_path.parent / "modules"
            aliases_path = json_path.parent / "aliases.json"

            def slugify(s: str) -> str:
                import re as _re
                return _re.sub(r"[^a-zA-Z0-9]+", "-", (s or "").lower()).strip("-")[:60]

            def load_module(mtype: str, key: str) -> dict | None:
                p = modules_dir / f"{mtype}_{slugify(key)}.json"
                if not p.exists():
                    return None
                try:
                    return json.loads(p.read_text())
                except Exception:
                    return None

            # Alias map: non-canonical name → canonical. Manually curated;
            # underscore keys (`_comment`, etc.) are ignored.
            aliases: dict[str, str] = {}
            if aliases_path.exists():
                try:
                    raw = json.loads(aliases_path.read_text())
                    aliases = {k: v for k, v in raw.items() if not k.startswith("_")}
                except Exception:
                    pass

            # Strip the kind of corporate suffixes that show up in SEC-style
            # filings ("Alphabet Inc. (Class A)") to merge with the colloquial
            # form ("Alphabet"). Run BEFORE alias lookup so the alias map
            # works on the bare name.
            import re as _re
            _SUFFIX_RE = _re.compile(
                r"\s*(?:[,]?\s*(?:Inc|Corp|Corporation|Co|Company|Ltd|Limited|"
                r"plc|PLC|S\.A\.|AB|AG|N\.V\.|GmbH|LLC|LP|Holdings?|Group)\b\.?)+"
                r"(?:\s*\([^)]*\))?\s*$",
                _re.IGNORECASE,
            )

            def normalize_corp_name(name: str) -> str:
                if not name:
                    return name
                n = name.strip()
                # Strip a single trailing parenthetical (e.g. "(publ)", "(Class A)")
                n = _re.sub(r"\s*\([^)]*\)\s*$", "", n).strip()
                # Then strip corporate suffixes iteratively (e.g. "Inc.", "Holdings Inc.")
                prev = None
                while prev != n:
                    prev = n
                    n = _SUFFIX_RE.sub("", n).strip().rstrip(",")
                return n or name

            _alias_lower = {k.lower(): v for k, v in aliases.items()}

            def canon(name: str) -> str:
                """Resolve a name to its canonical form. Tries direct alias
                match first (so "X Corp." → "X (xAI)" works without losing the
                "Corp." suffix during normalization), then falls back to
                normalizing corporate suffixes and re-checking the map."""
                if not name:
                    return name
                raw = name.strip()
                # 1. Direct alias hit on the raw name.
                if raw in aliases:
                    return aliases[raw]
                if raw.lower() in _alias_lower:
                    return _alias_lower[raw.lower()]
                # 2. Normalize suffixes ("Alphabet Inc. (Class A)" → "Alphabet")
                #    and try the alias map again. The normalized form is what
                #    we return when no alias applies.
                n = normalize_corp_name(raw)
                if n in aliases:
                    return aliases[n]
                if n.lower() in _alias_lower:
                    return _alias_lower[n.lower()]
                return n

            tickers: dict[str, dict] = {}
            companies: dict[str, dict] = {}
            concepts: dict[str, dict] = {}
            books: dict[str, dict] = {}
            repos: dict[str, dict] = {}
            posters: dict[str, dict] = {}

            if articles_dir.exists():
                for f in articles_dir.iterdir():
                    if not (f.is_file() and f.name.startswith("article_") and f.name.endswith(".json")):
                        continue
                    bid = f.name[len("article_"):-len(".json")]
                    try:
                        a = json.loads(f.read_text())
                    except Exception:
                        continue

                    # Track per-article companies we've already counted, so a
                    # ticker mention + a companies[] entry that resolve to the
                    # same canonical company don't double-count this article.
                    counted_company_keys: set[str] = set()

                    for tk in (a.get("tickers") or []):
                        if not tk:
                            continue
                        k = str(tk).upper()
                        e = tickers.setdefault(k, {"ticker": k, "count": 0, "articles": []})
                        e["count"] += 1
                        e["articles"].append(bid)
                        # Synthesize a companies entry from the ticker module if
                        # one exists. Older articles only populate tickers[];
                        # this lifts them into the Companies index without re-
                        # generating anything.
                        m = load_module("ticker", k)
                        if m and m.get("name"):
                            cname = canon(m["name"])
                            ckey = cname.lower()
                            if ckey not in counted_company_keys:
                                e2 = companies.setdefault(ckey, {"name": cname, "ticker": k, "count": 0, "articles": []})
                                e2["count"] += 1
                                e2["articles"].append(bid)
                                if not e2.get("ticker"):
                                    e2["ticker"] = k
                                counted_company_keys.add(ckey)

                    for c in (a.get("companies") or []):
                        if not isinstance(c, dict):
                            continue
                        name = (c.get("name") or "").strip()
                        if not name:
                            continue
                        cname = canon(name)
                        ckey = cname.lower()
                        if ckey in counted_company_keys:
                            # Already counted via the ticker path; just merge
                            # ticker info if the companies[] entry has one we
                            # didn't already attach.
                            e = companies.get(ckey) or {}
                            if c.get("ticker") and not e.get("ticker"):
                                e["ticker"] = c["ticker"]
                            continue
                        e = companies.setdefault(ckey, {"name": cname, "ticker": c.get("ticker"), "count": 0, "articles": []})
                        e["count"] += 1
                        e["articles"].append(bid)
                        if c.get("ticker") and not e.get("ticker"):
                            e["ticker"] = c["ticker"]
                        counted_company_keys.add(ckey)

                    for c in (a.get("concepts") or []):
                        if not isinstance(c, dict):
                            continue
                        term = (c.get("term") or "").strip()
                        if not term:
                            continue
                        key = (c.get("key") or slugify(term)).lower()
                        e = concepts.setdefault(key, {"key": key, "term": term, "definition": c.get("definition") or "", "count": 0, "articles": []})
                        e["count"] += 1
                        e["articles"].append(bid)
                        # Prefer the longest definition we've seen.
                        if c.get("definition") and len(c["definition"]) > len(e.get("definition") or ""):
                            e["definition"] = c["definition"]

                    for b in (a.get("books") or []):
                        if not isinstance(b, dict):
                            continue
                        title = (b.get("title") or "").strip()
                        if not title:
                            continue
                        key = (b.get("key") or slugify(title)).lower()
                        e = books.setdefault(key, {"key": key, "title": title, "author": b.get("author") or "", "summary": b.get("summary") or "", "count": 0, "articles": []})
                        e["count"] += 1
                        e["articles"].append(bid)

                    for r in (a.get("repos") or []):
                        if not isinstance(r, dict):
                            continue
                        key = (r.get("key") or "").strip().lower()
                        if not key:
                            continue
                        e = repos.setdefault(key, {"key": key, "url": r.get("url") or "", "summary": r.get("summary") or "", "count": 0, "articles": []})
                        e["count"] += 1
                        e["articles"].append(bid)

                    poster = (a.get("poster") or "").strip()
                    if poster:
                        e = posters.setdefault(poster.lower(), {"username": poster, "count": 0, "articles": []})
                        e["count"] += 1
                        e["articles"].append(bid)

            # Attach cached profile/module data where available.
            for k, e in tickers.items():
                m = load_module("ticker", k)
                if m:
                    e["module"] = {kk: m.get(kk) for kk in ("name", "exchange", "blurb", "recent")}
            for e in companies.values():
                if e.get("ticker"):
                    m = load_module("ticker", e["ticker"])
                    if m:
                        e["module"] = {kk: m.get(kk) for kk in ("name", "exchange", "blurb", "recent")}
            for e in posters.values():
                m = load_module("poster", e["username"])
                if m:
                    e["module"] = {kk: m.get(kk) for kk in ("display_name", "blurb", "credibility_note")}

            self.write_json({
                "tickers": sorted(tickers.values(), key=lambda x: (-x["count"], x["ticker"])),
                "companies": sorted(companies.values(), key=lambda x: (-x["count"], x["name"].lower())),
                "concepts": sorted(concepts.values(), key=lambda x: (-x["count"], x["term"].lower())),
                "books": sorted(books.values(), key=lambda x: (-x["count"], x["title"].lower())),
                "repos": sorted(repos.values(), key=lambda x: (-x["count"], x["key"])),
                "posters": sorted(posters.values(), key=lambda x: (-x["count"], x["username"].lower())),
            })
            return
        if parsed.path.startswith("/api/article/"):
            rest = parsed.path[len("/api/article/"):]
            # /api/article/<id>/history  → list of versions
            if rest.endswith("/history"):
                bm_id = rest[: -len("/history")]
                self.handle_article_history(bm_id)
                return
            # /api/article/<id>/v/<version_id>  → load a specific version
            if "/v/" in rest:
                bm_id, _, vid = rest.partition("/v/")
                self.handle_article_version(bm_id, vid)
                return
            self.handle_article_get(rest)
            return
        if parsed.path.startswith("/api/module/"):
            rest = parsed.path[len("/api/module/"):]
            mtype, _, key = rest.partition("/")
            self.handle_module_get(mtype, key)
            return
        if parsed.path == "/api/longform":
            self.handle_longform_list()
            return
        if parsed.path.startswith("/api/longform/"):
            rest = parsed.path[len("/api/longform/"):].strip("/")
            # URL-decode so that "companies%2Funitika" → "companies/unitika"
            rest_dec = urllib.parse.unquote(rest)
            # Company-shaped reports live under /api/longform/companies/<id>
            if rest_dec.startswith("companies/"):
                cid = rest_dec[len("companies/"):].strip("/")
                if cid:
                    self.handle_longform_company_get(cid)
                    return
            if rest_dec == "companies":
                self.handle_longform_company_list()
                return
            # below here, treat rest as the legacy (single-segment) report id
            rest = rest_dec
            if rest.endswith("/live"):
                self.handle_longform_live(rest[:-len("/live")])
                return
            if "/options/" in rest:
                report_id, _, prediction_id = rest.partition("/options/")
                self.handle_longform_option(report_id, prediction_id)
                return
            if rest:
                self.handle_longform_get(rest)
                return
        if parsed.path == "/api/stats":
            json_path = resolve_local_path(params.get("path", ["data/x-bookmarks.json"])[0], "data/x-bookmarks.json")
            if not json_path.exists():
                self.write_json({"error": "Bookmark archive does not exist yet."}, HTTPStatus.NOT_FOUND)
                return
            payload = reporter.read_json(json_path)
            bms = payload.get("bookmarks") or []
            tag_counts: dict[str, int] = {}
            with_media = 0
            with_ocr = 0
            for b in bms:
                if b.get("media"):
                    with_media += 1
                if any((m.get("ocr_text") or "").strip() for m in (b.get("media") or [])):
                    with_ocr += 1
                for t in (b.get("tags") or []):
                    tag_counts[t] = tag_counts.get(t, 0) + 1
            authors: Counter = Counter()
            for b in bms:
                username = (b.get("author") or {}).get("username") or "?"
                authors[username] += 1
            articles_dir = resolve_local_path("data/articles", "data/articles")
            article_ids: list[str] = []
            article_meta: dict[str, str] = {}
            if articles_dir.exists():
                for f in articles_dir.iterdir():
                    if f.is_file() and f.name.startswith("article_") and f.name.endswith(".json"):
                        bid = f.name[len("article_"):-len(".json")]
                        article_ids.append(bid)
                        try:
                            d = json.loads(f.read_text())
                            article_meta[bid] = d.get("generated_at") or ""
                        except Exception:
                            pass
            # Stale = the bookmark has a quoted-tweet (older articles missed it)
            # OR the bookmark's text was rechecked AFTER the article was written.
            stale_ids: list[str] = []
            for b in bms:
                bid = str(b.get("id"))
                if bid not in article_meta:
                    continue
                art_ts = article_meta[bid]
                if b.get("quoted_tweet"):
                    stale_ids.append(bid); continue
                bm_ts = b.get("text_checked_at") or ""
                if bm_ts and art_ts and bm_ts > art_ts:
                    stale_ids.append(bid)
            self.write_json({
                "total": len(bms),
                "with_media": with_media,
                "without_media": len(bms) - with_media,
                "with_ocr": with_ocr,
                "tag_counts": tag_counts,
                "top_authors": [
                    {"username": u, "count": n} for u, n in authors.most_common(20)
                ],
                "article_ids": article_ids,
                "stale_article_ids": stale_ids,
                "article_queue": article_queue_status(),
                "generated_at": payload.get("generated_at"),
            })
            return
        self.write_json({"error": "Not found"}, HTTPStatus.NOT_FOUND)

    # ---- article generation endpoints ----

    def handle_article_history(self, bookmark_id: str) -> None:
        try:
            import article_generator as agen
        except Exception as exc:  # noqa: BLE001
            self.write_json({"error": f"generator import failed: {exc}"},
                            HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        versions = agen.list_article_history(bookmark_id)
        self.write_json({"bookmark_id": bookmark_id, "versions": versions})

    def handle_article_version(self, bookmark_id: str, version_id: str) -> None:
        try:
            import article_generator as agen
        except Exception as exc:  # noqa: BLE001
            self.write_json({"error": f"generator import failed: {exc}"},
                            HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        article = agen.load_article_version(bookmark_id, version_id)
        if article is None:
            self.write_json({"error": "version not found"}, HTTPStatus.NOT_FOUND)
            return
        modules = agen.collect_modules_for_article(article)
        self.write_json({"article": article, "modules": modules})

    def handle_article_get(self, bookmark_id: str) -> None:
        try:
            import article_generator as agen
        except Exception as exc:  # noqa: BLE001
            self.write_json({"error": f"generator import failed: {exc}"},
                            HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        cached = agen.get_cached_article(bookmark_id)
        if cached is None:
            self.write_json({"error": "not generated yet", "bookmark_id": bookmark_id},
                            HTTPStatus.NOT_FOUND)
            return
        modules = agen.collect_modules_for_article(cached)
        self.write_json({"article": cached, "modules": modules})

    def handle_module_get(self, mtype: str, key: str) -> None:
        try:
            import article_generator as agen
        except Exception as exc:  # noqa: BLE001
            self.write_json({"error": f"generator import failed: {exc}"},
                            HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        if not mtype or not key:
            self.write_json({"error": "missing type or key"}, HTTPStatus.BAD_REQUEST)
            return
        cached = agen.get_cached_module(mtype, key)
        if cached is None:
            self.write_json({"error": "not generated yet"}, HTTPStatus.NOT_FOUND)
            return
        self.write_json(cached)

    # ---- longform endpoints ----

    def handle_longform_list(self) -> None:
        """Enumerate longform reports under data/longform/<id>/. Each entry
        carries the meta block from the assembled report.json so the topbar
        chooser can render titles/word counts without a second fetch.

        Also enumerates company-shaped reports under data/longform/companies/
        and emits them with id `companies/<slug>` so the existing UI dropdown
        can pick them up without code changes."""
        base = ROOT / "data" / "longform"
        if not base.exists():
            self.write_json({"reports": []})
            return
        out = []
        for d in sorted(base.iterdir()):
            if not d.is_dir() or d.name.startswith("."):
                continue
            # Skip the companies/ collection here — we descend into it next
            if d.name == "companies":
                continue
            report_path = d / "reports" / "report.json"
            if not report_path.exists():
                out.append({"id": d.name, "available": False})
                continue
            try:
                payload = json.loads(report_path.read_text())
            except Exception:
                out.append({"id": d.name, "available": False, "error": "parse_failed"})
                continue
            meta = payload.get("meta") or {}
            out.append({
                "id": d.name,
                "type": "profile",
                "available": True,
                "title": meta.get("title"),
                "subtitle": meta.get("subtitle"),
                "byline": meta.get("byline"),
                "snapshot_date": meta.get("snapshot_date"),
                "word_count": meta.get("word_count"),
                "n_chapters": meta.get("n_chapters"),
                "n_sources": meta.get("n_sources"),
                "missing_chapters": meta.get("missing_chapters") or [],
                "generated_at": meta.get("generated_at"),
            })

        # Now enumerate company-shaped reports
        companies_base = base / "companies"
        if companies_base.exists():
            for d in sorted(companies_base.iterdir()):
                if not d.is_dir() or d.name.startswith("."):
                    continue
                report_path = d / "reports" / "report.json"
                if not report_path.exists():
                    out.append({"id": f"companies/{d.name}", "type": "company", "available": False})
                    continue
                try:
                    payload = json.loads(report_path.read_text())
                except Exception:
                    out.append({"id": f"companies/{d.name}", "type": "company",
                                "available": False, "error": "parse_failed"})
                    continue
                meta = payload.get("meta") or {}
                out.append({
                    "id": f"companies/{d.name}",
                    "type": "company",
                    "available": True,
                    "title": meta.get("title"),
                    "subtitle": meta.get("subtitle"),
                    "byline": meta.get("byline"),
                    "snapshot_date": meta.get("snapshot_date"),
                    "word_count": meta.get("word_count"),
                    "n_chapters": meta.get("n_chapters"),
                    "n_sources": meta.get("n_sources"),
                    "missing_chapters": meta.get("missing_chapters") or [],
                    "generated_at": meta.get("generated_at"),
                    "ticker": meta.get("ticker"),
                })
        self.write_json({"reports": out})

    def handle_longform_company_list(self) -> None:
        """Just the company reports (filtered subset)."""
        base = ROOT / "data" / "longform" / "companies"
        out = []
        if base.exists():
            for d in sorted(base.iterdir()):
                if not d.is_dir() or d.name.startswith("."):
                    continue
                report_path = d / "reports" / "report.json"
                if not report_path.exists():
                    out.append({"id": d.name, "available": False})
                    continue
                try:
                    payload = json.loads(report_path.read_text())
                except Exception:
                    out.append({"id": d.name, "available": False, "error": "parse_failed"})
                    continue
                meta = payload.get("meta") or {}
                out.append({
                    "id": d.name,
                    "available": True,
                    "ticker": meta.get("ticker"),
                    "title": meta.get("title"),
                    "subtitle": meta.get("subtitle"),
                    "word_count": meta.get("word_count"),
                    "n_chapters": meta.get("n_chapters"),
                    "n_sources": meta.get("n_sources"),
                    "snapshot_date": meta.get("snapshot_date"),
                    "generated_at": meta.get("generated_at"),
                })
        self.write_json({"reports": out})

    def handle_longform_company_get(self, company_id: str) -> None:
        """Return assembled report.json for a company-shaped longform."""
        if not re.match(r"^[a-z0-9_-]+$", company_id or ""):
            self.write_json({"error": "bad company id"}, HTTPStatus.BAD_REQUEST)
            return
        report_path = (ROOT / "data" / "longform" / "companies"
                       / company_id / "reports" / "report.json")
        if not report_path.exists():
            self.write_json({"error": "report not assembled yet",
                             "id": company_id},
                            HTTPStatus.NOT_FOUND)
            return
        try:
            payload = json.loads(report_path.read_text())
        except Exception as exc:  # noqa: BLE001
            self.write_json({"error": f"parse_failed: {exc}"},
                            HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        self.write_json(payload)

    def handle_longform_get(self, report_id: str) -> None:
        """Return the full assembled report.json for a given longform id."""
        if not re.match(r"^[a-z0-9_-]+$", report_id or ""):
            self.write_json({"error": "bad report id"}, HTTPStatus.BAD_REQUEST)
            return
        report_path = ROOT / "data" / "longform" / report_id / "reports" / "report.json"
        if not report_path.exists():
            self.write_json({"error": "report not assembled yet",
                             "id": report_id},
                            HTTPStatus.NOT_FOUND)
            return
        try:
            payload = json.loads(report_path.read_text())
        except Exception as exc:  # noqa: BLE001
            self.write_json({"error": f"parse_failed: {exc}"},
                            HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        self.write_json(payload)

    def handle_longform_live(self, report_id: str) -> None:
        """Re-quote each timeline prediction against the live Stax headless
        cache (127.0.0.1:1421) and recompute progress/status.

        Returns a slim diff keyed by prediction_id. Frontend calls this on
        visibilitychange and patches the rendered Timeline cards in place
        without a full re-render. Companies appendix and chapter chips are
        intentionally not refreshed (per design choice 2026-05-10)."""
        if not re.match(r"^[a-z0-9_-]+$", report_id or ""):
            self.write_json({"error": "bad report id"}, HTTPStatus.BAD_REQUEST)
            return
        timeline_path = (ROOT / "data" / "longform" / report_id
                         / "prep" / "timeline.json")
        if not timeline_path.exists():
            self.write_json({"error": "timeline missing"},
                            HTTPStatus.NOT_FOUND)
            return
        try:
            timeline = json.loads(timeline_path.read_text())
        except Exception as exc:  # noqa: BLE001
            self.write_json({"error": f"parse_failed: {exc}"},
                            HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        try:
            sys.path.insert(0, str(ROOT / "longform" / "discover"))
            from longform_live import refresh_timeline_live  # type: ignore
        except Exception as exc:  # noqa: BLE001
            self.write_json({"error": f"live module missing: {exc}"},
                            HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        try:
            diff = refresh_timeline_live(timeline)
        except Exception as exc:  # noqa: BLE001
            self.write_json({"error": f"live refresh failed: {exc}"},
                            HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        self.write_json(diff)

    def handle_longform_option(self, report_id: str, prediction_id: str) -> None:
        """Return one curated option-track JSON for a timeline prediction."""
        if not re.match(r"^[a-z0-9_-]+$", report_id or ""):
            self.write_json({"error": "bad report id"}, HTTPStatus.BAD_REQUEST)
            return
        if not re.match(r"^[A-Za-z0-9_-]+$", prediction_id or ""):
            self.write_json({"error": "bad prediction id"}, HTTPStatus.BAD_REQUEST)
            return
        path = (ROOT / "data" / "longform" / report_id
                / "prep" / "options" / f"{prediction_id}.json")
        if not path.exists():
            self.write_json({"error": "option track missing"},
                            HTTPStatus.NOT_FOUND)
            return
        try:
            payload = json.loads(path.read_text())
        except Exception as exc:  # noqa: BLE001
            self.write_json({"error": f"parse_failed: {exc}"},
                            HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        self.write_json(payload)

    def handle_refresh(self) -> None:
        """Pull latest bookmarks via Safari (osascript), merge into the archive.

        Uses the Safari import path because the Playwright profile's saved
        login has been unreliable on this machine. Requires Safari to be
        running with x.com open in a NON-front window (the safari-import
        helpers skip the front window to avoid commandeering whatever the
        user is actively browsing). Stops scrolling after 2 consecutive
        already-known bookmarks so a normal refresh takes seconds.
        """
        archive_path = ROOT / "data" / "x-bookmarks.json"
        fresh_path = ROOT / "data" / "x-bookmarks.fresh.json"
        ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_path = ROOT / "data" / f"x-bookmarks.backup-pre-refresh-{ts}.json"
        try:
            before = len(reporter.read_json(archive_path).get("bookmarks") or [])
        except Exception:
            before = 0
        backup_ready = False
        try:
            with archive_file_lock(archive_path):
                if archive_path.exists():
                    shutil.copy(archive_path, backup_path)
                    backup_ready = True
        except Exception as exc:
            self.write_json({"error": "backup failed", "detail": str(exc)},
                            HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        # 1. Safari-driven pull. Stops at the 2-consecutive-known boundary so
        #    a refresh after a quiet stretch finishes in seconds, not minutes.
        try:
            pull = subprocess.run(
                [
                    "python3", "x_bookmark_reporter.py", "safari-import",
                    "--json", str(fresh_path),
                    "--csv", "",
                    "--report", "",
                    "--stop-after-known", "2",
                    "--max-scrolls", "200",
                    "--idle-rounds", "10",
                    "--scroll-pause", "1.2",
                ],
                cwd=ROOT, capture_output=True, text=True, timeout=600,
            )
        except subprocess.TimeoutExpired:
            self.write_json({"error": "safari pull timed out (10 min) — make sure Safari is open with a non-front x.com tab"},
                            HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        if pull.returncode != 0:
            self.write_json({"error": "safari pull failed",
                             "detail": (pull.stderr or pull.stdout or "")[:400]},
                            HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        # 2. Smart-merge into the archive (keeps existing enrichment, adds new IDs).
        merge_log = ""
        if not backup_ready:
            try:
                seeded_archive = seed_archive_from_fresh_if_missing(archive_path, fresh_path)
            except Exception as exc:
                self.write_json({"error": "archive seed failed", "detail": str(exc)},
                                HTTPStatus.INTERNAL_SERVER_ERROR)
                return
            if seeded_archive:
                merge_log = "initialized archive from fresh pull"
            else:
                try:
                    with archive_file_lock(archive_path):
                        shutil.copy(archive_path, backup_path)
                        backup_ready = True
                except Exception as exc:
                    self.write_json({"error": "backup failed", "detail": str(exc)},
                                    HTTPStatus.INTERNAL_SERVER_ERROR)
                    return
        if backup_ready:
            try:
                merge = subprocess.run(
                    ["python3", "smart_merge.py", str(backup_path), str(fresh_path), str(archive_path)],
                    cwd=ROOT, capture_output=True, text=True, timeout=120,
                )
            except subprocess.TimeoutExpired:
                self.write_json({"error": "merge timed out"}, HTTPStatus.INTERNAL_SERVER_ERROR)
                return
            if merge.returncode != 0:
                self.write_json({"error": "merge failed",
                                 "detail": (merge.stderr or merge.stdout or "")[:400]},
                                HTTPStatus.INTERNAL_SERVER_ERROR)
                return
            merge_log = merge.stdout[-200:]
        # 3. OCR any newly-pulled images so the article-generation pipeline
        #    has selectable + searchable text from the start. Pipeline is a
        #    no-op when no images need OCR (~1s), so safe to always run.
        ocr_log = ""
        try:
            ocr = subprocess.run(
                ["python3", "ocr_pipeline.py"],
                cwd=ROOT, capture_output=True, text=True, timeout=900,
            )
            ocr_log = (ocr.stdout or ocr.stderr or "")[-300:]
        except subprocess.TimeoutExpired:
            self.write_json({"error": "ocr timed out",
                             "detail": "ocr timed out (15 min)"},
                            HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        except Exception as exc:
            self.write_json({"error": "ocr failed", "detail": str(exc)},
                            HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        if ocr.returncode != 0:
            self.write_json({"error": "ocr failed",
                             "detail": (ocr.stderr or ocr.stdout or "")[-1000:]},
                            HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        if backup_ready:
            try:
                upgrade_ids = new_ids_missing_subagent_article(archive_path)
                dispatched = dispatch_article_upgrade(upgrade_ids)
            except Exception as exc:  # noqa: BLE001
                log_path = ROOT / "data" / "upgrade.log"
                log_path.parent.mkdir(parents=True, exist_ok=True)
                with log_path.open("a", encoding="utf-8") as log:
                    log.write(f"[{now_label()}] refresh upgrade dispatch failed: {exc}\n")
                upgrade_ids = []
                dispatched = False
        else:
            upgrade_ids = []
            dispatched = False
        try:
            after = len(reporter.read_json(archive_path).get("bookmarks") or [])
        except Exception:
            after = before
        self.write_json({"added": max(0, after - before), "total": after,
                         "pull_log": (pull.stdout or pull.stderr or "")[-400:],
                         "merge_log": merge_log,
                         "ocr_log": ocr_log,
                         "article_queue": article_queue_status(),
                         "article_queue_dispatched": dispatched,
                         "article_queue_candidate_ids": upgrade_ids})

    def handle_article_generate(self, payload: dict[str, Any]) -> None:
        try:
            import article_generator as agen
        except Exception as exc:  # noqa: BLE001
            self.write_json({"error": f"generator import failed: {exc}"},
                            HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        bm_id = str(payload.get("bookmark_id") or "")
        if not bm_id:
            self.write_json({"error": "missing bookmark_id"}, HTTPStatus.BAD_REQUEST)
            return
        force = bool(payload.get("force"))
        focus = (payload.get("focus") or "").strip() or None
        # Default to the Claude CLI (matches the wave-dispatch quality bar);
        # caller can pass {"backend": "deepseek"} to force the cheaper path.
        prefer = payload.get("backend") or "claude"
        archive_path = resolve_local_path(
            payload.get("path") or "data/x-bookmarks.json", "data/x-bookmarks.json")
        if not archive_path.exists():
            self.write_json({"error": "archive missing"}, HTTPStatus.NOT_FOUND)
            return
        archive = reporter.read_json(archive_path)
        bookmark = next(
            (b for b in archive.get("bookmarks") or [] if str(b.get("id")) == bm_id),
            None,
        )
        if bookmark is None:
            self.write_json({"error": "bookmark not found"}, HTTPStatus.NOT_FOUND)
            return
        if force or focus:
            # Belt-and-suspenders: ensure every image (own, quoted, threaded)
            # is OCR'd before regenerating. Mirrors the worker-script logic so
            # both the script-driven and UI-driven force regenerations have
            # the same OCR context.
            def _has_unocrd_image(bm: dict) -> bool:
                import ocr_pipeline as ocrp
                return bool(ocrp.collect_targets([bm]))

            if _has_unocrd_image(bookmark):
                try:
                    ocr = subprocess.run(
                        ["python3", "ocr_pipeline.py"],
                        cwd=ROOT, capture_output=True, text=True, timeout=900,
                    )
                except subprocess.TimeoutExpired:
                    self.write_json({"error": "ocr timed out",
                                     "detail": "ocr timed out (15 min)"},
                                    HTTPStatus.INTERNAL_SERVER_ERROR)
                    return
                except Exception:
                    self.write_json({"error": "ocr failed"}, HTTPStatus.INTERNAL_SERVER_ERROR)
                    return
                if ocr.returncode != 0:
                    self.write_json({"error": "ocr failed",
                                     "detail": (ocr.stderr or ocr.stdout or "")[-1000:]},
                                    HTTPStatus.INTERNAL_SERVER_ERROR)
                    return
                # Re-read bookmark with fresh OCR
                archive = reporter.read_json(archive_path)
                bookmark = next(
                    (b for b in archive.get("bookmarks") or [] if str(b.get("id")) == bm_id),
                    bookmark,
                )
        try:
            if force or focus:
                article = agen.generate_article(
                    bookmark, prefer=prefer, focus=focus,
                    use_cache=False, write=False,
                )
                agen.write_article(bm_id, article, archive=True)
            else:
                article = agen.generate_article(bookmark, prefer=prefer, focus=focus)
        except Exception as exc:  # noqa: BLE001
            self.write_json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        # Lazily generate ticker + poster modules in the same call so the
        # client gets everything it needs to render. Failures here are non-
        # fatal — the article still works without modules.
        for tk in article.get("tickers") or []:
            try:
                agen.generate_module("ticker", tk, prefer=prefer)
            except Exception:
                pass
        if article.get("poster"):
            try:
                agen.generate_module("poster", article["poster"], prefer=prefer)
            except Exception:
                pass
        modules = agen.collect_modules_for_article(article)
        self.write_json({"article": article, "modules": modules})

    def serve_static(self, request_path: str) -> None:
        path = "/index.html" if request_path in {"", "/"} else request_path
        candidate = (WEB_DIR / path.lstrip("/")).resolve()
        if WEB_DIR.resolve() not in candidate.parents and candidate != WEB_DIR.resolve():
            self.write_json({"error": "Invalid path"}, HTTPStatus.BAD_REQUEST)
            return
        if not candidate.exists() or not candidate.is_file():
            self.write_json({"error": "Not found"}, HTTPStatus.NOT_FOUND)
            return
        mime = mimetypes.guess_type(str(candidate))[0] or "application/octet-stream"
        data = candidate.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length == 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        return json.loads(raw)

    def write_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def write_error(self, exc: BaseException) -> None:
        self.write_json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        sys.stderr.write("%s - %s\n" % (self.address_string(), format % args))


def serve(host: str, port: int, open_browser: bool) -> None:
    server = ThreadingHTTPServer((host, port), UIHandler)
    url = f"http://{host}:{port}"
    print(f"X Bookmark Reporter UI: {url}")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping UI server.")
    finally:
        server.server_close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Start the local X Bookmark Reporter UI.")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--no-open", action="store_true", help="Do not open the UI in a browser.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    serve(args.host, args.port, open_browser=not args.no_open)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
