"""Read-only adapters for existing runtime records; no agent/control imports.

Only metadata is polled. A selected log is read from a bounded tail on demand.
Recorded state, file writes and mathematical progress are deliberately distinct.
"""
from __future__ import annotations

import heapq
import json
import math
import os
import re
import stat
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse

router = APIRouter()
TAIL_BYTES = 64 * 1024
RECORD_BYTES = 16 * 1024
RECENT_ROUNDS = 12
WORKER_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
ROUND_NAME = re.compile(r"round_([1-9][0-9]{0,11})\.log\Z")


def project_root() -> Path:
    value = os.environ.get("DANUS_DASHBOARD_PROJECT") or os.environ.get("DANUS_PROJECT_DIR")
    if not value:
        raise HTTPException(503, "Dashboard project is not configured")
    return Path(value).resolve()


def contained(root: Path, *parts: str) -> Path:
    """Reject traversal and symlink escapes, including symlinked directories."""
    root = root.resolve()
    try:
        path = root.joinpath(*parts).resolve()
        path.relative_to(root)
    except (ValueError, OSError, RuntimeError):
        raise HTTPException(404, "Source is outside the configured project")
    return path


def file_meta(path: Path) -> dict | None:
    try:
        info = path.stat()
        if not stat.S_ISREG(info.st_mode):
            return None
        return {"modified_at": info.st_mtime, "bytes": info.st_size,
                "version": f"{info.st_ino}:{info.st_mtime_ns}:{info.st_ctime_ns}:{info.st_size}"}
    except OSError:
        return None


def read_record(path: Path, truncate: bool = False) -> str:
    """Small control records only; never silently parse a truncated JSON record."""
    if file_meta(path) is None:
        return ""
    try:
        with path.open("rb") as stream:
            data = stream.read(RECORD_BYTES + 1)
        return data[:RECORD_BYTES].decode("utf-8", errors="replace") if truncate or len(data) <= RECORD_BYTES else ""
    except OSError:
        return ""


def number(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return value
    return None


def recent_rounds(directory: Path) -> list[dict]:
    """Scan filenames, not historical log contents; stat only the recent window."""
    try:
        with os.scandir(directory) as entries:
            numbers = heapq.nlargest(RECENT_ROUNDS, (
                int(match.group(1)) for entry in entries
                if (match := ROUND_NAME.fullmatch(entry.name))
                and entry.is_file(follow_symlinks=False)
            ))
    except OSError:
        return []
    result = []
    for n in numbers:
        meta = file_meta(directory / f"round_{n}.log")
        if meta is not None:
            result.append({"round": n, **meta})
    return result


def worker_snapshot(root: Path, name: str) -> dict:
    status_path = contained(root, "workers", name, ".status.json")
    raw = read_record(status_path)
    try:
        status = json.loads(raw)
    except (ValueError, TypeError):
        status = None
    valid = isinstance(status, dict)
    status = status if valid else {}
    role = {}
    for line in read_record(contained(root, "workers", name, ".role")).splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip() in {"MODEL", "REASONING_EFFORT"}:
            role[key.strip()] = value.strip()[:160]
    rounds = recent_rounds(contained(root, "workers", name, "logs"))
    task_path = contained(root, "workers", name, "TASK.md")
    task = read_record(task_path, truncate=True)
    task_meta = file_meta(task_path)
    return {
        "name": name, "model": role.get("MODEL"), "effort": role.get("REASONING_EFFORT"),
        "reported_state": str(status.get("state", "unknown"))[:80],
        "reported_round": number(status.get("round")), "recorded_pid": number(status.get("pid")),
        "last_rc": number(status.get("last_rc")),
        "round_started_at": number(status.get("round_started_at")),
        "status_record": file_meta(status_path), "status_readable": valid,
        "task": task[:8000], "task_truncated": len(task) > 8000 or (
            task_meta is not None and task_meta["bytes"] > RECORD_BYTES),
        "task_record": task_meta, "rounds": rounds,
        "latest_log": rounds[0] if rounds else None,
    }


def orchestrator_path() -> Path | None:
    # An explicit server-side opt-in, never an arbitrary browser-supplied path.
    value = os.environ.get("DANUS_DASHBOARD_ORCHESTRATOR_LOG")
    return Path(value).expanduser().resolve() if value else None


def build_activity(root: Path | None = None) -> dict:
    root = (root or project_root()).resolve()
    workers = []
    unavailable = []
    directory = contained(root, "workers")
    try:
        names = sorted(p.name for p in directory.iterdir()
                       if WORKER_NAME.fullmatch(p.name) and p.is_dir() and not p.is_symlink())
    except OSError:
        names = []
    for name in names:
        try:
            workers.append(worker_snapshot(root, name))
        except (HTTPException, OSError):
            unavailable.append(name)
    path = orchestrator_path()
    shared = {}
    # Exact existing records, not a second graph or a new progress database.
    for kind in ("master_guidance", "elaboration", "verification"):
        try:
            shared[kind] = file_meta(contained(root, "global_memory", f"{kind}.jsonl"))
        except HTTPException:
            shared[kind] = None
    return {
        "project": root.name, "fetched_at": time.time(), "workers": workers,
        "unavailable_workers": unavailable, "shared_records": shared,
        "orchestrator": {"configured": path is not None,
                         "record": file_meta(path) if path else None},
        "recent_round_limit": RECENT_ROUNDS, "tail_byte_limit": TAIL_BYTES,
    }


def read_tail(path: Path, label: str) -> dict:
    if file_meta(path) is None:
        raise HTTPException(404, "Log is missing, unreadable, or not a regular file")
    try:
        with path.open("rb") as stream:
            info = os.fstat(stream.fileno())
            start = max(0, info.st_size - TAIL_BYTES)
            stream.seek(start)
            data = stream.read(min(TAIL_BYTES, info.st_size - start))
    except OSError:
        raise HTTPException(404, "Log is unavailable")
    # Drop an initial partial line, but do not discard a single very long line.
    if start and b"\n" in data:
        data = data.split(b"\n", 1)[1]
    return {"source": label, "text": data.decode("utf-8", errors="replace"),
            "truncated": start > 0, "bytes": info.st_size, "modified_at": info.st_mtime,
            "fetched_at": time.time()}


def log_tail(worker: str | None = None, round_number: int | None = None,
             source: str = "worker") -> dict:
    if source == "orchestrator":
        path = orchestrator_path()
        if path is None:
            raise HTTPException(404, "Orchestrator transcript is not configured")
        return read_tail(path, "Operator-configured orchestrator transcript")
    if source != "worker" or not worker or not WORKER_NAME.fullmatch(worker):
        raise HTTPException(404, "Unknown log source")
    if round_number is None or not 1 <= round_number <= 999999999999:
        raise HTTPException(404, "Unknown round")
    root = project_root()
    path = contained(root, "workers", worker, "logs", f"round_{round_number}.log")
    return read_tail(path, f"workers/{worker}/logs/round_{round_number}.log")


@router.get("/api/activity")
def activity():
    return JSONResponse(build_activity(), headers={"Cache-Control": "no-store"})


@router.get("/api/activity/log")
def activity_log(worker: str | None = None,
                 round: int | None = Query(default=None, ge=1, le=999999999999),
                 source: str = "worker"):
    return JSONResponse(log_tail(worker, round, source), headers={"Cache-Control": "no-store"})
