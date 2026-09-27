"""Offline dashboard-only tests: synthetic records, no agents or API spending."""
import json
import os
from pathlib import Path

import pytest
from fastapi import HTTPException
from starlette.testclient import TestClient

from danus.observability import app
from danus.observability.activity import (
    RECORD_BYTES, TAIL_BYTES, build_activity, log_tail, read_tail,
)


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("DANUS_DASHBOARD_PROJECT", str(tmp_path))
    monkeypatch.delenv("DANUS_DASHBOARD_ORCHESTRATOR_LOG", raising=False)
    w = tmp_path / "workers" / "xhigh"
    (w / "logs").mkdir(parents=True)
    (w / ".status.json").write_text(json.dumps({
        "state": "running", "round": 1427, "pid": 9999, "round_started_at": 100,
        "last_rc": 0, "last_fact_id": "old-id-is-not-a-progress-signal"}))
    (w / ".role").write_text("MODEL=test-model\nREASONING_EFFORT=xhigh\nSECRET=not-exposed\n")
    (w / "TASK.md").write_text("Check the assigned repair.\n")
    for n in (9, 10, 1426, 1427):
        (w / "logs" / f"round_{n}.log").write_text(f"round {n}: inspect evidence\n")
    os.utime(w / ".status.json", (200, 200))
    os.utime(w / "logs" / "round_1427.log", (300, 300))
    return tmp_path


def test_source_times_not_refresh_or_progress(project):
    data = build_activity(project)
    w = data["workers"][0]
    assert data["fetched_at"] > 300
    assert w["status_record"]["modified_at"] == 200
    assert w["latest_log"]["modified_at"] == 300
    assert w["reported_round"] == 1427
    assert w["reported_state"] == "running"
    assert [r["round"] for r in w["rounds"]] == [1427, 1426, 10, 9]
    assert "last_fact_id" not in w and "healthy" not in w and "looping" not in w
    assert "SECRET" not in json.dumps(data)


@pytest.mark.parametrize("bad", ["{unfinished", "[]", "null", "x" * (RECORD_BYTES + 1)])
def test_malformed_status_does_not_hide_logs(project, bad):
    (project / "workers/xhigh/.status.json").write_text(bad)
    w = build_activity(project)["workers"][0]
    assert not w["status_readable"]
    assert w["reported_state"] == "unknown" and w["latest_log"]["round"] == 1427


def test_snapshot_reads_no_log_bodies(project, monkeypatch):
    original = Path.open
    def checked(path, *args, **kwargs):
        assert path.suffix != ".log", "Polling must not read round log contents"
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "open", checked)
    assert len(build_activity(project)["workers"]) == 1


def test_tail_bounded_and_partial_utf8_safe(project):
    path = project / "workers/xhigh/logs/round_1427.log"
    path.write_bytes(b"secret old prefix\n" + b"x" * (TAIL_BYTES * 2) + b"\nlatest\xe2\x82")
    data = log_tail("xhigh", 1427)
    assert data["truncated"]
    assert "latest" in data["text"] and "secret old prefix" not in data["text"]
    assert len(data["text"]) <= TAIL_BYTES


def test_empty_and_rotated_log(project):
    path = project / "workers/xhigh/logs/round_1427.log"
    first = build_activity(project)["workers"][0]["latest_log"]["version"]
    path.write_text("")
    assert log_tail("xhigh", 1427)["text"] == ""
    assert build_activity(project)["workers"][0]["latest_log"]["version"] != first
    path.unlink()
    with pytest.raises(HTTPException) as e:
        log_tail("xhigh", 1427)
    assert e.value.status_code == 404


@pytest.mark.parametrize("worker", ["..", "../xhigh", "/etc", "xhigh/../../outside", "xhigh%2f.."])
def test_path_traversal_rejected(project, worker):
    with pytest.raises(HTTPException):
        log_tail(worker, 1427)


def test_symlink_escape_rejected(project, tmp_path):
    outside = tmp_path.parent / (tmp_path.name + "-outside.log")
    outside.write_text("must not be exposed")
    path = project / "workers/xhigh/logs/round_1428.log"
    path.symlink_to(outside)
    with pytest.raises(HTTPException):
        log_tail("xhigh", 1428)
    assert 1428 not in [r["round"] for r in build_activity(project)["workers"][0]["rounds"]]


def test_symlinked_log_directory_rejected(project, tmp_path):
    outside = tmp_path.parent / (tmp_path.name + "-logs")
    outside.mkdir()
    (outside / "round_1.log").write_text("outside")
    w = project / "workers/other"
    w.mkdir()
    (w / "logs").symlink_to(outside, target_is_directory=True)
    assert "other" in build_activity(project)["unavailable_workers"]
    with pytest.raises(HTTPException):
        log_tail("other", 1)


def test_orchestrator_opt_in_only(project, monkeypatch, tmp_path):
    assert not build_activity(project)["orchestrator"]["configured"]
    with pytest.raises(HTTPException):
        log_tail(source="orchestrator")
    transcript = tmp_path / "selected-session.jsonl"
    transcript.write_text('{"type":"message","text":"reviewing"}\n')
    monkeypatch.setenv("DANUS_DASHBOARD_ORCHESTRATOR_LOG", str(transcript))
    assert build_activity(project)["orchestrator"]["record"]
    assert "reviewing" in log_tail(source="orchestrator")["text"]
    transcript.unlink()
    assert build_activity(project)["orchestrator"]["record"] is None


def test_recent_round_window_and_numeric_order(project):
    logs = project / "workers/xhigh/logs"
    for n in range(1500, 1600):
        (logs / f"round_{n}.log").write_text("")
    (logs / "round_9999999999999.log").write_text("ignored")
    (logs / "round_01600.log").write_text("ignored")
    rounds = build_activity(project)["workers"][0]["rounds"]
    assert len(rounds) == 12 and rounds[0]["round"] == 1599 and rounds[-1]["round"] == 1588


def test_http_read_only_routes_and_legacy_views(project):
    before = {p.relative_to(project): (p.read_bytes(), p.stat().st_mtime_ns)
              for p in project.rglob("*") if p.is_file()}
    with TestClient(app) as client:
        assert client.get("/").status_code == 200
        assert "Live activity" in client.get("/").text
        assert "Fact Graph" in client.get("/static/results.html").text
        r = client.get("/api/activity")
        assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
        r = client.get("/api/activity/log", params={"worker": "xhigh", "round": 1427})
        assert r.status_code == 200 and "1427" in r.json()["text"]
        assert r.headers["cache-control"] == "no-store"
        assert client.get("/api/activity/log?worker=xhigh&round=-1").status_code == 422
        for url in ("/api/activity", "/api/activity/log"):
            assert client.post(url).status_code == 405
        for url in ("/api/overview", "/api/factgraph", "/api/channels", "/api/channel/verification"):
            assert client.get(url).status_code == 200
    after = {p.relative_to(project): (p.read_bytes(), p.stat().st_mtime_ns)
             for p in project.rglob("*") if p.is_file()}
    assert before == after


def test_missing_sources_are_unknown(tmp_path):
    data = build_activity(tmp_path)
    assert data["workers"] == []
    assert all(value is None for value in data["shared_records"].values())


def test_nonfinite_status_values_do_not_break_json(project):
    (project / "workers/xhigh/.status.json").write_text('{"round":NaN,"pid":true}')
    with TestClient(app) as client:
        w = client.get("/api/activity").json()["workers"][0]
        assert w["reported_round"] is None and w["recorded_pid"] is None


def test_fifo_is_not_read(project):
    path = project / "pipe.log"
    os.mkfifo(path)
    with pytest.raises(HTTPException):
        read_tail(path, "fifo")
