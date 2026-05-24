from __future__ import annotations

import fcntl
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any


def archive_lock_path(path: Path) -> Path:
    return path.with_name(f"{path.name}.lock")


@contextmanager
def archive_file_lock(path: str | Path):
    archive_path = Path(path)
    lock_path = archive_lock_path(archive_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


def write_json_atomic(path: Path, payload: Any, *, sort_keys: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
        text=True,
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=sort_keys)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        try:
            dir_fd = os.open(path.parent, os.O_DIRECTORY)
        except OSError:
            return
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except Exception:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass
        raise


def write_archive_locked(path: str | Path, payload: Any, *, sort_keys: bool = False) -> None:
    archive_path = Path(path)
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    with archive_file_lock(archive_path):
        write_json_atomic(archive_path, payload, sort_keys=sort_keys)
