"""Run Vision-framework OCR on every captured bookmark image.

Pipeline:
  1. Collect every {bookmark_id, media_index, url} for type=image media.
  2. Download missing images in parallel into data/media-cache/<sha>.jpg.
  3. Run ./ocr (Swift Vision) on missing images, parallelized across workers.
  4. Store extracted text on each media item as `ocr_text` and write back.

Idempotent — anything already OCR'd is skipped.
"""

import argparse
import concurrent.futures
import datetime as dt
import hashlib
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any

from archive_io import archive_file_lock, write_json_atomic

RETRY_FAILED_AFTER_SECONDS = 24 * 60 * 60


def url_filename(url: str) -> str:
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()
    suffix = ".jpg"
    lower = url.lower()
    for ext in (".png", ".jpeg", ".jpg", ".webp", ".gif"):
        if ext in lower:
            suffix = ".jpg" if ext == ".jpeg" else ext
            break
    return f"{digest}{suffix}"


def download_one(url: str, dest: Path, *, timeout: float = 15.0) -> dict:
    if dest.exists() and dest.stat().st_size > 0:
        return {"url": url, "path": str(dest), "status": "cached"}
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd = None
    tmp_path: Path | None = None
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                              "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                              "Version/17.0 Safari/605.1.15",
                "Referer": "https://x.com/",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as response:
            data = response.read()
        if not data:
            return {"url": url, "path": str(dest), "status": "empty"}
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{dest.name}.",
            suffix=".tmp",
            dir=str(dest.parent),
        )
        tmp_path = Path(tmp_name)
        with os.fdopen(fd, "wb") as handle:
            fd = None
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, dest)
        return {"url": url, "path": str(dest), "status": "downloaded", "size": len(data)}
    except Exception as exc:  # noqa: BLE001
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if tmp_path is not None:
            try:
                tmp_path.unlink()
            except FileNotFoundError:
                pass
        return {"url": url, "path": str(dest), "status": "error", "error": str(exc)}


def run_ocr_batch(ocr_binary: Path, paths: list[str]) -> dict[str, dict]:
    if not paths:
        return {}
    cmd = [str(ocr_binary), *paths]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return {p: {"ok": False, "error": "ocr_timeout"} for p in paths}
    out: dict[str, dict] = {}
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "path" in obj:
            out[obj["path"]] = obj
    for p in paths:
        if p not in out:
            out[p] = {"ok": False, "error": "no_output"}
    return out


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--archive", default="data/x-bookmarks.json")
    p.add_argument("--cache-dir", default="data/media-cache")
    p.add_argument("--ocr-binary", default="./ocr")
    p.add_argument("--download-workers", type=int, default=16)
    p.add_argument("--ocr-workers", type=int, default=4,
                   help="Concurrent OCR subprocess invocations.")
    p.add_argument("--ocr-batch-size", type=int, default=12,
                   help="Images per OCR subprocess invocation.")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--save-every", type=int, default=200)
    p.add_argument("--force", action="store_true",
                   help="Re-OCR images that already have ocr_text.")
    p.add_argument("--needs-blocks", action="store_true",
                   help="Retarget images that have ocr_text but no ocr_blocks.")
    return p.parse_args()


def _walk_media_locations(bm: dict):
    """Yield (path, media_dict) for every media item reachable from a bookmark.

    Covers: bm.media, bm.quoted_tweet.media, bm.thread[i].media,
    bm.thread[i].quoted_tweet.media. `path` is the list of keys/indexes to
    navigate from the bookmark root to that media list element — used later
    by `_resolve_path` to write OCR results back to the same nested item.
    """
    for idx, m in enumerate(bm.get("media") or []):
        yield (["media", idx], m)
    q = bm.get("quoted_tweet") or {}
    for idx, m in enumerate(q.get("media") or []):
        yield (["quoted_tweet", "media", idx], m)
    for ti, t in enumerate(bm.get("thread") or []):
        for idx, m in enumerate(t.get("media") or []):
            yield (["thread", ti, "media", idx], m)
        tq = t.get("quoted_tweet") or {}
        for idx, m in enumerate(tq.get("media") or []):
            yield (["thread", ti, "quoted_tweet", "media", idx], m)


def _resolve_path(bm: dict, path: list):
    """Walk `path` into `bm` and return the media-list element, or None if
    any key/index is missing (e.g. archive shape changed since collection)."""
    cur = bm
    for key in path[:-1]:
        if isinstance(cur, dict):
            cur = cur.get(key)
        elif isinstance(cur, list) and isinstance(key, int) and 0 <= key < len(cur):
            cur = cur[key]
        else:
            return None
        if cur is None:
            return None
    last = path[-1]
    if isinstance(cur, list) and isinstance(last, int) and 0 <= last < len(cur):
        return cur[last]
    return None


def _find_media_by_url(bm: dict, url: str):
    for _, media in _walk_media_locations(bm):
        if isinstance(media, dict) and media.get("url") == url:
            return media
    return None


def _parse_ocr_failed_at(value: str | None) -> float | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.timestamp()
    except Exception:
        return None


def _should_retry_failed(media: dict) -> bool:
    failed = media.get("ocr_failed")
    if not failed:
        return False
    failed_at = _parse_ocr_failed_at(media.get("ocr_failed_at"))
    if failed_at is None:
        return True
    return time.time() - failed_at >= RETRY_FAILED_AFTER_SECONDS


def collect_targets(bookmarks: list[dict], *, force: bool = False,
                    needs_blocks: bool = False) -> list[tuple[str, list, str]]:
    """Return list of (bookmark_id, path, url) for images that still need OCR.

    `path` is the chain of keys/indexes to walk from the bookmark to the
    media dict — supports nested media inside quoted_tweet and thread items
    in addition to the bookmark's own media list.

    `force=True` reprocesses every image. `needs_blocks=True` retargets images
    that have ocr_text but no ocr_blocks (so we can backfill bounding boxes
    onto a previously-OCR'd archive without re-running the rest).
    """
    out = []
    for bm in bookmarks:
        for path, m in _walk_media_locations(bm):
            # Video posters are still .jpg/.png frames worth OCR'ing — they
            # often carry visible chart copy or talk-show-style chyrons.
            if m.get("type") not in ("image", "video", None):
                continue
            url = m.get("url") or ""
            if not url:
                continue
            if not force and not needs_blocks:
                if m.get("ocr_text") is not None:
                    continue
                if m.get("ocr_failed") and not _should_retry_failed(m):
                    continue
            elif needs_blocks and not force:
                # Only retarget if we don't already have blocks.
                if m.get("ocr_blocks") is not None:
                    continue
            out.append((str(bm["id"]), path, url))
    return out


def main() -> int:
    args = parse_args()
    archive_path = Path(args.archive)
    cache_dir = Path(args.cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    ocr_binary = Path(args.ocr_binary).resolve()
    if not ocr_binary.exists():
        print(f"OCR binary not found: {ocr_binary}", file=sys.stderr)
        return 2

    payload = json.loads(archive_path.read_text())
    bookmarks = payload.get("bookmarks") or []
    targets = collect_targets(bookmarks, force=args.force, needs_blocks=args.needs_blocks)
    if args.limit:
        targets = targets[: args.limit]
    print(f"OCR targets: {len(targets)} images across {len({t[0] for t in targets})} bookmarks")

    if not targets:
        print("Nothing to do.")
        return 0

    # Phase 1: parallel download
    started = time.time()
    url_to_path: dict[str, Path] = {}
    download_results: dict[str, dict] = {}
    unique_urls = list({u for _, _, u in targets})
    for url in unique_urls:
        url_to_path[url] = cache_dir / url_filename(url)

    print(f"Downloading {len(unique_urls)} unique images "
          f"with {args.download_workers} workers...")
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.download_workers) as ex:
        futures = {
            ex.submit(download_one, url, url_to_path[url]): url for url in unique_urls
        }
        ok = cached = err = 0
        for fut in concurrent.futures.as_completed(futures):
            res = fut.result()
            download_results[res["url"]] = res
            status = res.get("status")
            if status == "downloaded":
                ok += 1
            elif status == "cached":
                cached += 1
            else:
                err += 1
            if (ok + cached + err) % 100 == 0:
                print(f"  download progress: ok={ok} cached={cached} err={err}")
    print(f"Download done in {time.time()-started:.1f}s — "
          f"downloaded={ok}, cached={cached}, errors={err}")

    # Phase 2: parallel OCR over batches
    paths_needing_ocr: list[Path] = []
    for url in unique_urls:
        path = url_to_path[url]
        if path.exists() and path.stat().st_size > 0:
            paths_needing_ocr.append(path)

    print(f"Running OCR on {len(paths_needing_ocr)} images "
          f"with {args.ocr_workers} workers x {args.ocr_batch_size} batch...")

    batches: list[list[str]] = []
    for i in range(0, len(paths_needing_ocr), args.ocr_batch_size):
        batches.append([str(p) for p in paths_needing_ocr[i:i + args.ocr_batch_size]])

    started_ocr = time.time()
    ocr_results: dict[str, dict] = {}
    completed = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.ocr_workers) as ex:
        futures = {ex.submit(run_ocr_batch, ocr_binary, batch): batch for batch in batches}
        for fut in concurrent.futures.as_completed(futures):
            batch_result = fut.result()
            ocr_results.update(batch_result)
            completed += len(futures[fut])
            if completed % 50 < args.ocr_batch_size:
                elapsed = time.time() - started_ocr
                rate = completed / max(0.1, elapsed)
                eta = (len(paths_needing_ocr) - completed) / max(0.01, rate)
                print(f"  OCR progress: {completed}/{len(paths_needing_ocr)}  "
                      f"elapsed={elapsed:.0f}s eta={eta:.0f}s")

    print(f"OCR done in {time.time()-started_ocr:.1f}s")

    # Phase 3: re-read the latest archive under the archive lock, then merge
    # only OCR fields into that current payload before replacing it.
    with archive_file_lock(archive_path):
        latest_payload = json.loads(archive_path.read_text())
        latest_bookmarks = latest_payload.get("bookmarks") or []
        latest_by_id = {str(b["id"]): b for b in latest_bookmarks if b.get("id")}

        filled = 0
        failed = 0
        no_text = 0
        for bookmark_id, path, url in targets:
            bm = latest_by_id.get(bookmark_id)
            if not bm:
                continue
            media_dict = _resolve_path(bm, path)
            if not isinstance(media_dict, dict) or media_dict.get("url") != url:
                media_dict = _find_media_by_url(bm, url)
                if media_dict is None:
                    continue
            cache_path = url_to_path.get(url)
            if not cache_path:
                continue
            result = ocr_results.get(str(cache_path))
            if result is None:
                dl = download_results.get(url) or {}
                media_dict["ocr_failed"] = dl.get("error", "no_ocr_result")
                media_dict["ocr_failed_at"] = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
                failed += 1
                continue
            if result.get("ok"):
                text = (result.get("text") or "").strip()
                media_dict["ocr_text"] = text
                media_dict["ocr_lines"] = result.get("lines", 0)
                media_dict["ocr_blocks"] = result.get("blocks") or []
                media_dict.pop("ocr_failed", None)
                media_dict.pop("ocr_failed_at", None)
                if text:
                    filled += 1
                else:
                    no_text += 1
            else:
                media_dict["ocr_failed"] = result.get("error", "unknown")
                media_dict["ocr_failed_at"] = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
                failed += 1

        write_json_atomic(archive_path, latest_payload)
    print(f"\nWrote {archive_path}.")
    print(f"  filled (text found):    {filled}")
    print(f"  no_text (image, blank): {no_text}")
    print(f"  failed:                 {failed}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
