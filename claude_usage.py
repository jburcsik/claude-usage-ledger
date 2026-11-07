#!/usr/bin/env python3
"""Record Claude Code usage per machine and report where the tokens went.

collect writes data/<device>/<date>.json from this machine's local Claude logs,
report merges every device's day files, config prints the effective settings.
Run every command from the repository directory.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import socket
import subprocess
import sys
import tomllib
from collections import defaultdict
from collections.abc import Iterator, Mapping
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timedelta
from operator import itemgetter
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

SCHEMA = 1
ROOT = Path.cwd()
TOKEN_FIELDS = ("input_tokens", "output_tokens", "cache_creation_tokens", "cache_read_tokens")


@dataclass(frozen=True)
class Config:
    """Effective settings: defaults, then the config file, then the environment."""

    device: str
    root: Path = ROOT
    timezone: str = "UTC"
    cron: str = "17 12"
    stale_days: int = 5
    drift_warn: float = 0.02
    transcripts: Path = Path.home() / ".claude" / "projects"
    ccusage: str = "npx -y ccusage@20"

    @classmethod
    def load(cls, root: Path = ROOT, env: Mapping[str, str] = os.environ) -> Config:
        """Read root/claude-usage.toml if present and apply environment overrides."""
        path = root / "claude-usage.toml"
        raw: dict[str, Any] = tomllib.loads(path.read_text()) if path.exists() else {}
        home = Path(env.get("HOME") or Path.home())
        config_dir = env.get("CLAUDE_CONFIG_DIR")
        default_transcripts = (Path(config_dir) if config_dir else home / ".claude") / "projects"
        cfg = cls(
            device=env.get("CLAUDE_USAGE_DEVICE")
            or str(raw.get("device") or socket.gethostname().split(".")[0]),
            root=root,
            timezone=env.get("CLAUDE_USAGE_TZ") or str(raw.get("timezone", cls.timezone)),
            cron=str(raw.get("cron", cls.cron)),
            stale_days=int(raw.get("stale_days", cls.stale_days)),
            drift_warn=float(raw.get("drift_warn", cls.drift_warn)),
            transcripts=Path(str(raw.get("transcripts", default_transcripts))).expanduser(),
            ccusage=str(raw.get("ccusage", cls.ccusage)),
        )
        parts = cfg.cron.split()
        ok = len(parts) == 2 and all(p.isdigit() for p in parts)
        if not ok or not (0 <= int(parts[0]) <= 59 and 0 <= int(parts[1]) <= 23):
            raise ValueError(f"cron must be 'MINUTE HOUR' within 0-59 and 0-23, got {cfg.cron!r}")
        ZoneInfo(cfg.timezone)
        return cfg

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def data(self) -> Path:
        return self.root / "data"

    def as_dict(self) -> dict[str, Any]:
        return {k: str(v) if isinstance(v, Path) else v for k, v in asdict(self).items()}


def _tokens(raw: dict[str, Any]) -> dict[str, int]:
    return {f: int(raw.get(f, 0)) for f in TOKEN_FIELDS}


@dataclass(kw_only=True)
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0

    @property
    def tokens(self) -> int:
        return sum(getattr(self, f) for f in TOKEN_FIELDS)


@dataclass(kw_only=True)
class ModelUsage(TokenUsage):
    """Per-agent, per-model totals for one day. Cost comes from ccusage."""

    agent: str
    model: str
    cost_usd: float = 0.0

    @classmethod
    def validate(cls, raw: dict[str, Any]) -> ModelUsage:
        cost = float(raw.get("cost_usd", 0.0))
        return cls(agent=str(raw["agent"]), model=str(raw["model"]), cost_usd=cost, **_tokens(raw))


@dataclass(kw_only=True)
class ProjectUsage(TokenUsage):
    """Per-project, per-model token totals for one day. No cost: see docs/design.md."""

    project: str
    model: str

    @classmethod
    def validate(cls, raw: dict[str, Any]) -> ProjectUsage:
        return cls(project=str(raw["project"]), model=str(raw["model"]), **_tokens(raw))


@dataclass
class DayRecord:
    """One device's usage for one day. This is the on-disk format."""

    device: str
    date: str
    collected_at: str
    timezone: str
    schema: int = SCHEMA
    models: list[ModelUsage] = field(default_factory=list)
    projects: list[ProjectUsage] = field(default_factory=list)

    @classmethod
    def validate(cls, raw: dict[str, Any], source: Path) -> DayRecord:
        got = int(raw.get("schema", 0))
        if got != SCHEMA:
            raise ValueError(f"{source}: schema {got}, this tool reads schema {SCHEMA}")
        return cls(
            device=str(raw["device"]),
            date=str(raw["date"]),
            collected_at=str(raw["collected_at"]),
            timezone=str(raw.get("timezone", "UTC")),
            schema=got,
            models=[ModelUsage.validate(m) for m in raw.get("models", [])],
            projects=[ProjectUsage.validate(p) for p in raw.get("projects", [])],
        )


def run(cmd: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    """The one place subprocesses are started; tests replace it."""
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=False)


def ccusage_by_day(cfg: Config) -> dict[str, list[ModelUsage]]:
    """Day -> per-agent, per-model totals and cost, from ccusage."""
    proc = run(shlex.split(cfg.ccusage) + ["daily", "--json", "--by-agent", "-z", cfg.timezone])
    if proc.returncode:
        raise SystemExit(f"ccusage failed ({proc.returncode}): {proc.stderr.strip()[:500]}")
    try:
        daily = json.loads(proc.stdout).get("daily", [])
    except json.JSONDecodeError as exc:
        raise SystemExit(f"ccusage printed no JSON: {proc.stdout.strip()[:200]}") from exc
    days: dict[str, list[ModelUsage]] = {}
    for row in daily:
        days[row["period"]] = [
            ModelUsage(
                agent=agent.get("agent", "claude"),
                model=m["modelName"],
                input_tokens=m.get("inputTokens", 0),
                output_tokens=m.get("outputTokens", 0),
                cache_creation_tokens=m.get("cacheCreationTokens", 0),
                cache_read_tokens=m.get("cacheReadTokens", 0),
                cost_usd=m.get("cost", 0.0),
            )
            for agent in row.get("agents") or [row]
            for m in agent.get("modelBreakdowns", [])
        ]
    return days


def read_records(path: Path) -> Iterator[dict[str, Any]]:
    """Assistant records that carry usage and a timestamp, streamed from one transcript."""
    try:
        handle = path.open(encoding="utf-8", errors="replace")
    except OSError:
        return
    with handle:
        for line in handle:
            if '"usage"' not in line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            message = rec.get("message") or {}
            if rec.get("type") == "assistant" and message.get("usage") and rec.get("timestamp"):
                yield rec


def scan_transcripts(cfg: Config) -> dict[str, list[ProjectUsage]]:
    """Day -> per-project token totals from the transcripts; ccusage has no project view."""
    totals: dict[tuple[str, str, str], ProjectUsage] = {}
    seen: set[tuple[str | None, ...]] = set()
    for path in sorted(cfg.transcripts.rglob("*.jsonl")):
        for rec in read_records(path):
            message = rec["message"]
            # One response is written as several records, one per content block, each
            # repeating the full usage object; they share requestId and message.id. The
            # seen-set spans all files because a resumed session copies them into a new one.
            key = (rec.get("requestId"), message["id"]) if message.get("id") else (rec.get("uuid"),)
            if key in seen or key == (None,):
                continue
            seen.add(key)
            day = datetime.fromisoformat(rec["timestamp"]).astimezone(cfg.tz).date().isoformat()
            project = rec.get("cwd") or path.parent.name
            model = message.get("model") or "unknown"
            row = totals.setdefault(
                (day, project, model), ProjectUsage(project=project, model=model)
            )
            usage = message["usage"]
            row.input_tokens += usage.get("input_tokens", 0)
            row.output_tokens += usage.get("output_tokens", 0)
            row.cache_creation_tokens += usage.get("cache_creation_input_tokens", 0)
            row.cache_read_tokens += usage.get("cache_read_input_tokens", 0)
    by_day: dict[str, list[ProjectUsage]] = defaultdict(list)
    for (day, _, _), row in totals.items():
        by_day[day].append(row)
    return by_day


def collect(cfg: Config, now: str | None = None) -> int:
    """Write this device's day files, touching only days whose numbers moved."""
    models = ccusage_by_day(cfg)
    projects = scan_transcripts(cfg)
    now = now or datetime.now(cfg.tz).isoformat(timespec="seconds")
    out_dir = cfg.data / cfg.device
    out_dir.mkdir(parents=True, exist_ok=True)
    written = 0
    for day in sorted(set(models) | set(projects)):
        record = DayRecord(
            device=cfg.device,
            date=day,
            collected_at=now,
            timezone=cfg.timezone,
            models=sorted(models.get(day, []), key=lambda m: (m.agent, m.model)),
            projects=sorted(projects.get(day, []), key=lambda p: (p.project, p.model)),
        )
        payload = asdict(record)
        target = out_dir / f"{day}.json"
        if target.exists():
            old = DayRecord.validate(json.loads(target.read_text()), target)
            if {**asdict(old), "collected_at": now} == payload:
                continue
            # Transcript pruning can remove part of a day's history, so a recount
            # can come up short. A saved day never goes down.
            if sum(m.tokens for m in record.models) < sum(m.tokens for m in old.models):
                continue
        target.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        written += 1
    (out_dir / "last_sync").write_text(datetime.now(cfg.tz).date().isoformat() + "\n")
    print(f"{cfg.device}: {written} day file(s) written, {len(models)} day(s) known")
    return 0


Row = tuple[str, int, float | None]


@dataclass
class Total:
    tokens: int = 0
    cost_usd: float = 0.0


def pull(cfg: Config) -> None:
    """Fetch the other devices' day files; a failure only means they may be stale."""
    proc = run(["git", "pull", "-q", "--rebase", "--autostash"], cwd=cfg.root)
    if proc.returncode:
        reason = proc.stderr.strip()[:200]
        print(f"Warning: git pull failed, other devices may be stale: {reason}", file=sys.stderr)


def stale_devices(cfg: Config) -> dict[str, str]:
    """Device -> last sync date, for devices silent longer than stale_days."""
    cutoff = (datetime.now(cfg.tz).date() - timedelta(days=cfg.stale_days)).isoformat()
    stale: dict[str, str] = {}
    for device_dir in sorted(p for p in cfg.data.iterdir() if p.is_dir()):
        stamp = device_dir / "last_sync"
        last = stamp.read_text().strip() if stamp.exists() else "never"
        if last == "never" or last < cutoff:
            stale[device_dir.name] = last
    return stale


def load(cfg: Config, since: str) -> list[DayRecord]:
    paths = sorted(cfg.data.glob("*/*.json")) if cfg.data.is_dir() else []
    records = [DayRecord.validate(json.loads(p.read_text()), p) for p in paths]
    return [r for r in records if r.date >= since]


def table(title: str, rows: list[Row], total: int, by_size: bool = True) -> None:
    """Print rows of (label, tokens, cost or None) with each row's share of total."""
    print(f"\n{title}")
    rows = sorted(rows, key=itemgetter(1), reverse=True) if by_size else sorted(rows)
    if not rows:
        print("  (nothing)")
        return
    width = max(len(r[0]) for r in rows)
    for label, tokens, cost in rows:
        share = 100 * tokens / total if total else 0
        money = f"  ${cost:>8,.2f}" if cost is not None else ""
        print(f"  {label:<{width}}  {tokens:>14,}  {share:>5.1f}%{money}")


def report(cfg: Config, days: int, as_json: bool) -> int:
    """Merge every device's day files since the cutoff and print the four tables."""
    pull(cfg)
    since = (datetime.now(cfg.tz).date() - timedelta(days=days - 1)).isoformat()
    records = load(cfg, since)
    if not records:
        print(f"No day files since {since} under {cfg.data}. Run collect on a device first.")
        return 1
    groups = {k: defaultdict(Total) for k in ("device", "model", "day")}
    by_project: dict[str, int] = defaultdict(int)
    claude_tokens = 0
    for rec in records:
        for m in rec.models:
            if m.agent == "claude":
                claude_tokens += m.tokens
            label = m.model if m.agent == "claude" else f"{m.model} ({m.agent})"
            for group, key in (("device", rec.device), ("model", label), ("day", rec.date)):
                groups[group][key].tokens += m.tokens
                groups[group][key].cost_usd += m.cost_usd
        for p in rec.projects:
            by_project[f"{rec.device}:{p.project}"] += p.tokens
    devices = groups["device"].values()
    total = Total(sum(t.tokens for t in devices), sum(t.cost_usd for t in devices))
    project_total = sum(by_project.values())
    stale = stale_devices(cfg)
    if as_json:
        out: dict[str, Any] = {"since": since, "timezone": cfg.timezone, "stale_devices": stale}
        out.update(total_tokens=total.tokens, total_cost_usd=round(total.cost_usd, 2))
        for name, group in groups.items():
            out[f"by_{name}"] = {
                k: asdict(Total(t.tokens, round(t.cost_usd, 2))) for k, t in group.items()
            }
        out["by_project"] = dict(by_project)
        print(json.dumps(out, indent=2, sort_keys=True))
        return 0
    for device, last in stale.items():
        print(f"Warning: {device} last synced {last}. Check its sync.log.")
    print(f"Claude usage, {since} to today ({cfg.timezone})")
    print(f"{total.tokens:,} tokens, ${total.cost_usd:,.2f} at API prices, {len(devices)} devices")
    rows = {g: [(k, t.tokens, t.cost_usd) for k, t in groups[g].items()] for g in groups}
    rows["project"] = [(k, v, None) for k, v in by_project.items()]
    table("By device", rows["device"], total.tokens)
    table("By model", rows["model"], total.tokens)
    table("By project (Claude Code only)", rows["project"], project_total)
    table("By day", rows["day"], total.tokens, by_size=False)
    # Only Claude Code records cwd, so the project total should match the claude
    # agent total closely. A wider gap means the transcript scan has regressed.
    gap = abs(project_total - claude_tokens)
    drift = gap / claude_tokens if claude_tokens else float(project_total > 0)
    if drift > cfg.drift_warn:
        print(
            f"\nWarning: project totals are {drift:.1%} off the Claude Code total"
            f" ({project_total:,} vs {claude_tokens:,}). The transcript scan may be stale."
        )
    print(
        "\nclaude.ai web, desktop and mobile usage shares the same account but writes no"
        "\nlocal logs, so it is absent here. Check /usage in Claude Code for the account total."
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("collect", help="write this device's usage to data/")
    rep = sub.add_parser("report", help="print merged usage across devices")
    rep.add_argument("-d", "--days", type=int, default=7, help="days back, including today")
    rep.add_argument("-j", "--json", action="store_true", help="machine-readable output")
    conf = sub.add_parser("config", help="print the effective settings as JSON")
    conf.add_argument("key", nargs="?", choices=[f.name for f in fields(Config)])
    args = parser.parse_args(argv)
    try:
        cfg = Config.load(ROOT)
    except (ValueError, ZoneInfoNotFoundError) as exc:
        parser.error(str(exc))
    if args.command == "collect":
        return collect(cfg)
    if args.command == "report":
        return report(cfg, args.days, args.json)
    settings = cfg.as_dict()
    print(settings[args.key] if args.key else json.dumps(settings, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
