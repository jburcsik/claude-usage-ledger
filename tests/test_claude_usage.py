"""Tests for claude_usage. Every fixture is invented; nothing touches the network or git."""

from __future__ import annotations

import json
import socket
import subprocess
from collections.abc import Callable
from dataclasses import asdict
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

import claude_usage as cu

TOKENS = {"input_tokens": 10, "output_tokens": 5, "cache_creation_input_tokens": 0}


def record(
    request: str, message: str, stamp: str, cwd: str | None = "/home/sam/projects/garden-planner"
) -> str:
    """One assistant transcript line, as Claude Code writes it."""
    rec = {
        "type": "assistant",
        "uuid": f"uuid-{request}-{message}",
        "requestId": request,
        "timestamp": stamp,
        "message": {"id": message, "model": "claude-sonnet-5-5", "usage": TOKENS},
    }
    if cwd:
        rec["cwd"] = cwd
    return json.dumps(rec)


def config(tmp_path: Path, **overrides: object) -> cu.Config:
    values = {"device": "laptop", "root": tmp_path, "transcripts": tmp_path / "transcripts"}
    values.update(overrides)
    return cu.Config(**values)


def day_file(cfg: cu.Config, device: str, day: str, tokens: int, project: str = "garden") -> Path:
    rec = cu.DayRecord(
        device=device,
        date=day,
        collected_at="2026-01-01T00:00:00+00:00",
        timezone=cfg.timezone,
        models=[cu.ModelUsage(agent="claude", model="m", input_tokens=tokens, cost_usd=1.5)],
        projects=[cu.ProjectUsage(project=project, model="m", input_tokens=tokens)],
    )
    path = cfg.root / "data" / device / f"{day}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(rec)))
    return path


def today(cfg: cu.Config) -> date:
    return datetime.now(cfg.tz).date()


def fake_runner(
    stdout: str = "", returncode: int = 0
) -> Callable[..., subprocess.CompletedProcess[str]]:
    def run(cmd: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, returncode, stdout, "")

    return run


def ccusage_payload(day: str, tokens: int) -> str:
    row = {"modelName": "claude-sonnet-5-5", "inputTokens": tokens, "cost": 0.25}
    agents = [{"agent": "claude", "modelBreakdowns": [row]}]
    return json.dumps({"daily": [{"period": day, "agents": agents}]})


# --- transcript scan ----------------------------------------------------------


def test_scan_counts_one_response_split_across_records_once(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    proj = cfg.transcripts / "garden-planner"
    proj.mkdir(parents=True)
    lines = [record("req-a", "msg-a", "2026-03-01T10:00:00Z") for _ in range(3)]
    lines.append(record("req-b", "msg-b", "2026-03-01T11:00:00Z"))
    (proj / "s1.jsonl").write_text("\n".join(lines) + "\n")
    # A resumed session copies earlier history into a second transcript.
    (proj / "s2.jsonl").write_text(lines[0] + "\n")
    [row] = cu.scan_transcripts(cfg)["2026-03-01"]
    assert row.input_tokens == 20
    assert row.project == "/home/sam/projects/garden-planner"


def test_scan_skips_records_without_usage_or_timestamp(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    proj = cfg.transcripts / "p"
    proj.mkdir(parents=True)
    good = json.loads(record("req-a", "msg-a", "2026-03-01T10:00:00Z"))
    no_stamp = {**good, "timestamp": None, "requestId": "req-b"}
    no_usage = {**good, "requestId": "req-c", "message": {"id": "msg-c", "usage": {}}}
    user = {**good, "type": "user", "requestId": "req-d"}
    lines = [json.dumps(r) for r in (good, no_stamp, no_usage, user)]
    (proj / "s.jsonl").write_text("\n".join(lines) + "\n")
    [row] = cu.scan_transcripts(cfg)["2026-03-01"]
    assert row.input_tokens == 10


def test_scan_falls_back_to_directory_name_when_cwd_missing(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    proj = cfg.transcripts / "-home-sam-projects-bird-log"
    proj.mkdir(parents=True)
    (proj / "s.jsonl").write_text(record("req-a", "msg-a", "2026-03-01T10:00:00Z", cwd=None))
    [row] = cu.scan_transcripts(cfg)["2026-03-01"]
    assert row.project == "-home-sam-projects-bird-log"


def test_scan_buckets_by_configured_timezone(tmp_path: Path) -> None:
    cfg = config(tmp_path, timezone="Asia/Tokyo")
    proj = cfg.transcripts / "p"
    proj.mkdir(parents=True)
    (proj / "s.jsonl").write_text(record("req-a", "msg-a", "2026-03-01T23:30:00Z"))
    assert list(cu.scan_transcripts(cfg)) == ["2026-03-02"]


# --- day files and collect ----------------------------------------------------


def test_validate_rejects_other_schema(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="schema"):
        cu.DayRecord.validate({"schema": 2, "device": "d", "date": "x"}, tmp_path / "f.json")


def test_validate_round_trips(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    path = day_file(cfg, "laptop", "2026-03-01", 100)
    rec = cu.DayRecord.validate(json.loads(path.read_text()), path)
    assert asdict(rec) == json.loads(path.read_text())


def test_collect_writes_day_file_and_last_sync(tmp_path: Path, monkeypatch) -> None:
    cfg = config(tmp_path)
    monkeypatch.setattr(cu, "run", fake_runner(ccusage_payload("2026-03-01", 100)))
    assert cu.collect(cfg) == 0
    raw = json.loads((cfg.root / "data" / "laptop" / "2026-03-01.json").read_text())
    assert raw["schema"] == 1 and raw["models"][0]["cost_usd"] == 0.25
    assert (cfg.root / "data" / "laptop" / "last_sync").read_text().strip() == str(today(cfg))


def test_collect_skips_rewrite_when_only_collected_at_differs(tmp_path: Path, monkeypatch) -> None:
    cfg = config(tmp_path)
    monkeypatch.setattr(cu, "run", fake_runner(ccusage_payload("2026-03-01", 100)))
    cu.collect(cfg)
    path = cfg.root / "data" / "laptop" / "2026-03-01.json"
    before = path.read_text()
    cu.collect(cfg, now="2099-01-01T00:00:00+00:00")
    assert path.read_text() == before


def test_collect_never_lowers_a_saved_day(tmp_path: Path, monkeypatch) -> None:
    cfg = config(tmp_path)
    monkeypatch.setattr(cu, "run", fake_runner(ccusage_payload("2026-03-01", 100)))
    cu.collect(cfg)
    monkeypatch.setattr(cu, "run", fake_runner(ccusage_payload("2026-03-01", 40)))
    cu.collect(cfg)
    path = cfg.root / "data" / "laptop" / "2026-03-01.json"
    assert json.loads(path.read_text())["models"][0]["input_tokens"] == 100


def test_collect_passes_configured_command_and_timezone(tmp_path: Path, monkeypatch) -> None:
    cfg = config(tmp_path, timezone="Europe/Lisbon", ccusage="ccusage")
    calls: list[list[str]] = []

    def run(cmd: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, '{"daily": []}', "")

    monkeypatch.setattr(cu, "run", run)
    cu.collect(cfg)
    assert calls == [["ccusage", "daily", "--json", "--by-agent", "-z", "Europe/Lisbon"]]


# --- report -------------------------------------------------------------------


@pytest.fixture
def three_devices(tmp_path: Path, monkeypatch) -> cu.Config:
    cfg = config(tmp_path)
    monkeypatch.setattr(cu, "run", fake_runner())
    today_ = today(cfg)
    for device, tokens, project in (("laptop", 300, "garden"), ("desktop", 200, "garden")):
        day_file(cfg, device, str(today_), tokens, project)
        (cfg.root / "data" / device / "last_sync").write_text(f"{today_}\n")
    day_file(cfg, "server", str(today_ - timedelta(days=1)), 100, "bird-log")
    (cfg.root / "data" / "server" / "last_sync").write_text(f"{today_ - timedelta(days=6)}\n")
    return cfg


def test_report_aggregates_by_device_model_project_and_day(
    three_devices: cu.Config, capsys
) -> None:
    assert cu.report(three_devices, days=7, as_json=True) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["total_tokens"] == 600
    assert out["by_device"]["laptop"]["tokens"] == 300
    assert out["by_model"]["m"]["cost_usd"] == 4.5
    assert out["by_project"]["server:bird-log"] == 100
    assert len(out["by_day"]) == 2
    assert out["stale_devices"] == {"server": str(today(three_devices) - timedelta(days=6))}


def test_report_warns_on_stale_device(three_devices: cu.Config, capsys) -> None:
    cu.report(three_devices, days=7, as_json=False)
    out = capsys.readouterr().out
    assert "Warning: server last synced" in out
    assert "By project" in out and "web, desktop and mobile" in out


def test_report_warns_on_drift(tmp_path: Path, monkeypatch, capsys) -> None:
    cfg = config(tmp_path)
    monkeypatch.setattr(cu, "run", fake_runner())
    path = day_file(cfg, "laptop", str(today(cfg)), 100)
    raw = json.loads(path.read_text())
    raw["projects"][0]["input_tokens"] = 90
    path.write_text(json.dumps(raw))
    cu.report(cfg, days=7, as_json=False)
    assert "10.0% off" in capsys.readouterr().out


def test_report_warns_when_pull_fails(three_devices: cu.Config, monkeypatch, capsys) -> None:
    monkeypatch.setattr(cu, "run", fake_runner(returncode=1))
    assert cu.report(three_devices, days=7, as_json=False) == 0
    assert "git pull failed" in capsys.readouterr().err


def test_report_exits_one_without_day_files(tmp_path: Path, monkeypatch, capsys) -> None:
    cfg = config(tmp_path)
    monkeypatch.setattr(cu, "run", fake_runner())
    assert cu.report(cfg, days=7, as_json=False) == 1
    assert "No day files" in capsys.readouterr().out


# --- configuration ------------------------------------------------------------


def test_config_defaults_without_file(tmp_path: Path) -> None:
    cfg = cu.Config.load(tmp_path, env={"HOME": str(tmp_path)})
    assert cfg.timezone == "UTC" and cfg.cron == "17 12" and cfg.stale_days == 5
    assert cfg.transcripts == tmp_path / ".claude" / "projects"
    assert cfg.device == socket.gethostname().split(".")[0]


def test_config_file_then_env_override(tmp_path: Path) -> None:
    (tmp_path / "claude-usage.toml").write_text('device = "desktop"\ntimezone = "Europe/Oslo"\n')
    cfg = cu.Config.load(tmp_path, env={})
    assert (cfg.device, cfg.timezone) == ("desktop", "Europe/Oslo")
    env = {"CLAUDE_USAGE_DEVICE": "server", "CLAUDE_USAGE_TZ": "UTC", "CLAUDE_CONFIG_DIR": "/c"}
    cfg = cu.Config.load(tmp_path, env=env)
    assert (cfg.device, cfg.timezone, cfg.transcripts) == ("server", "UTC", Path("/c/projects"))


def test_config_rejects_bad_cron(tmp_path: Path) -> None:
    (tmp_path / "claude-usage.toml").write_text('cron = "daily"\n')
    with pytest.raises(ValueError, match="cron"):
        cu.Config.load(tmp_path, env={})


def test_config_subcommand_prints_json_or_one_key(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(cu, "ROOT", tmp_path)
    monkeypatch.setenv("CLAUDE_USAGE_DEVICE", "laptop")
    assert cu.main(["config"]) == 0
    assert json.loads(capsys.readouterr().out)["device"] == "laptop"
    assert cu.main(["config", "device"]) == 0
    assert capsys.readouterr().out == "laptop\n"


def test_collect_exits_when_ccusage_fails(tmp_path: Path, monkeypatch) -> None:
    cfg = config(tmp_path)
    monkeypatch.setattr(cu, "run", fake_runner("boom", returncode=1))
    with pytest.raises(SystemExit, match="ccusage failed"):
        cu.collect(cfg)
    monkeypatch.setattr(cu, "run", fake_runner("not json"))
    with pytest.raises(SystemExit, match="no JSON"):
        cu.collect(cfg)


def test_config_rejects_cron_out_of_range(tmp_path: Path) -> None:
    (tmp_path / "claude-usage.toml").write_text('cron = "99 12"\n')
    with pytest.raises(ValueError, match="0-59"):
        cu.Config.load(tmp_path, env={})
