"""System-under-test control: restart FieldDesk with a trial's settings, read container CPU counters."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from harness.manifest import ROOT, Manifest

COMPOSE = ["docker", "compose", "-f", str(ROOT / "docker/compose.yml")]
CONTAINERS = {"postgres": "rebound-postgres-1", "fielddesk": "rebound-fielddesk-1", "toxiproxy": "rebound-toxiproxy-1"}
LOG_DIR = ROOT / "var/fielddesk"


def _run(cmd: list[str], env: dict | None = None, timeout: float = 180) -> str:
    r = subprocess.run(cmd, cwd=ROOT, env={**os.environ, **(env or {})}, capture_output=True, text=True,
                       timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} failed: {r.stderr.strip()[-500:]}")
    return r.stdout


def fielddesk_env(m: Manifest, gate_on: bool) -> dict[str, str]:
    return {
        "POOL_SIZE": str(m.resources.pool_size),
        "GATE_ENABLED": "true" if gate_on else "false",
        "GATE_TOKENS": str(m.gate.tokens),
        "GATE_QUEUE_MAX": str(m.gate.queue_max),
        "GATE_QUEUE_WAIT_MS": str(m.gate.queue_wait_ms),
        "GATE_RETRY_AFTER_S": str(m.gate.retry_after_s),
    }


def restart_fielddesk(env: dict[str, str]) -> None:
    """Stop FieldDesk, clear its request and sample logs, start it fresh with `env`, wait until healthy."""
    _run(COMPOSE + ["stop", "fielddesk"], env)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    for f in LOG_DIR.glob("*.jsonl"):
        f.unlink()
    _run(COMPOSE + ["up", "-d", "--force-recreate", "--no-deps", "--wait", "fielddesk"], env, timeout=300)


def container_env(name: str) -> dict[str, str]:
    out = _run(["docker", "inspect", CONTAINERS[name], "--format", "{{range .Config.Env}}{{println .}}{{end}}"])
    return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)


def cpusets() -> dict[str, str]:
    return {k: _run(["docker", "inspect", c, "--format", "{{.HostConfig.CpusetCpus}}"]).strip()
            for k, c in CONTAINERS.items()}


def cpu_stat(name: str) -> dict[str, int] | None:
    """cgroup v2 cpu.stat of a container (usage_usec, nr_throttled, throttled_usec); None if unreadable."""
    try:
        out = _run(["docker", "exec", CONTAINERS[name], "cat", "/sys/fs/cgroup/cpu.stat"], timeout=20)
    except (RuntimeError, subprocess.TimeoutExpired):
        return None
    return {k: int(v) for k, v in (line.split() for line in out.splitlines() if len(line.split()) == 2)}


def parse_cpuset(spec: str) -> set[int]:
    cpus: set[int] = set()
    for part in spec.split(","):
        if "-" in part:
            a, b = part.split("-")
            cpus |= set(range(int(a), int(b) + 1))
        elif part:
            cpus.add(int(part))
    return cpus


def copy_logs(dest: Path) -> None:
    for name in ("backend.jsonl", "samples.jsonl"):
        src = LOG_DIR / name
        if src.exists():
            (dest / name).write_bytes(src.read_bytes())
