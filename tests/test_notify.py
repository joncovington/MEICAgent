"""Unit tests for notify.py's structured log-event CLI helper.

Writes to _LOG_PATH are redirected to a tmp_path file via monkeypatch so no
real logs/agent.log is touched.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
import notify


@pytest.fixture(autouse=True)
def _tmp_log(tmp_path, monkeypatch):
    log_dir = tmp_path / "logs"
    monkeypatch.setattr(notify, "_LOG_DIR", str(log_dir))
    monkeypatch.setattr(notify, "_LOG_PATH", str(log_dir / "agent.log"))
    return log_dir


def _read_log_lines(log_dir):
    path = Path(log_dir) / "agent.log"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_log_event_creates_log_dir_and_file(_tmp_log):
    notify._log_event_internal("INFO", "hello")
    assert (Path(_tmp_log) / "agent.log").exists()


def test_log_event_writes_level_and_message(_tmp_log):
    notify._log_event_internal("WARNING", "something happened")
    lines = _read_log_lines(_tmp_log)
    assert lines[0]["level"] == "WARNING"
    assert lines[0]["message"] == "something happened"
    assert "timestamp" in lines[0]


def test_log_event_omits_data_key_when_none(_tmp_log):
    notify._log_event_internal("INFO", "no data here")
    lines = _read_log_lines(_tmp_log)
    assert "data" not in lines[0]


def test_log_event_includes_data_when_given(_tmp_log):
    notify._log_event_internal("INFO", "with data", {"symbol": "XSP"})
    lines = _read_log_lines(_tmp_log)
    assert lines[0]["data"] == {"symbol": "XSP"}


def test_log_event_appends_multiple_entries(_tmp_log):
    notify._log_event_internal("INFO", "first")
    notify._log_event_internal("INFO", "second")
    lines = _read_log_lines(_tmp_log)
    assert len(lines) == 2
    assert [l["message"] for l in lines] == ["first", "second"]


def test_cmd_log_event_parses_json_data(_tmp_log, capsys):
    args = argparse.Namespace(level="info", message="parsed", data=json.dumps({"a": 1}))
    notify.cmd_log_event(args)
    lines = _read_log_lines(_tmp_log)
    assert lines[0]["level"] == "INFO"  # uppercased
    assert lines[0]["data"] == {"a": 1}
    out = json.loads(capsys.readouterr().out.strip())
    assert out == {"ok": True}


def test_cmd_log_event_falls_back_to_raw_string_on_invalid_json(_tmp_log):
    args = argparse.Namespace(level="info", message="raw", data="not-json{")
    notify.cmd_log_event(args)
    lines = _read_log_lines(_tmp_log)
    assert lines[0]["data"] == "not-json{"


def test_cmd_log_event_no_data_arg(_tmp_log):
    args = argparse.Namespace(level="debug", message="no data arg", data=None)
    notify.cmd_log_event(args)
    lines = _read_log_lines(_tmp_log)
    assert "data" not in lines[0]
