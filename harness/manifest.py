"""Experiment manifests: YAML -> validated pydantic model -> resolved manifest + config hash.

`config_hash` = SHA-256 over the resolved manifest, the prompt file, the tool schemas, the model settings, the
image digests and the data checksum. Per-trial fields (condition, gate, repeat, seed order) are not part of the
configuration; they are recorded next to it.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict

ROOT = Path(__file__).resolve().parents[1]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Resources(Strict):
    api_cpuset: str
    pg_cpuset: str
    toxiproxy_cpuset: str
    runner_cpuset: str
    api_mem: str
    pg_mem: str
    uvicorn_workers: int = 1
    pool_size: int = 8
    pg_max_connections: int = 40


class Data(Strict):
    generator_seed: int
    scale: float
    template_db: str
    checksum: str


class Tasks(Strict):
    file: str
    sha256: str


class Arrivals(Strict):
    process: str = "poisson"
    goals_per_s: float
    seed: int
    start_s: float = 0
    stop_s: float = 550


class Background(Strict):
    rate_rps: float
    mix: dict[str, float]
    timeout_s: float = 2.0
    retries: int = 0
    seed: int = 11


class Windows(Strict):
    warmup: tuple[float, float] = (0, 60)
    baseline: tuple[float, float] = (60, 180)
    observe_s: float = 360
    drain_deadline_s: float = 240


class Fault(Strict):
    kind: str = "latency"
    proxy: str = "pg"
    direction: str = "downstream"
    latency_ms: int = 400
    jitter_ms: int = 50
    start_s: float = 180
    duration_s: float = 10


class GateCfg(Strict):
    tokens: int = 8
    queue_max: int = 16
    queue_wait_ms: int = 1500
    retry_after_s: int = 2


class Model(Strict):
    id: str = "muse-spark-1.3"
    api: str = "responses"
    continuation: str = "previous_response_id"
    reasoning_effort: str = "low"
    max_output_tokens: int = 2048
    parallel_tool_calls: bool = False
    prompt_cache_key: str = "rebound-agent-v1"
    prompt: str = "prompts/agent_v1.md"
    tools: str = "prompts/tools_v1.json"


class Scripted(Strict):
    think_time_s: float
    max_retries: int = 2
    backoff_base_s: float = 0.5
    backoff_cap_s: float = 8.0
    retry_after_cap_s: float = 30.0
    request_budget: int = 20
    seed: int = 13


class Limits(Strict):
    model_calls_per_goal: int = 16
    tool_calls_per_goal: int = 14
    goal_deadline_s: float = 240
    input_tokens_per_goal: int = 80_000
    max_concurrent_goals: int = 24
    trial_budget_usd: float = 10.0


class Calibration(Strict):
    healthy_bg_p95_ms: float  # gate for the BASELINE exit guard
    capacity_rps: float | None = None
    capacity_db_ms_per_s: float | None = None
    notes: str | None = None


class Manifest(Strict):
    experiment: str
    backend: dict
    resources: Resources
    data: Data
    tasks: Tasks
    arrivals: Arrivals
    background: Background
    windows: Windows = Windows()
    fault: Fault = Fault()
    fault_enabled: bool = True
    gate: GateCfg = GateCfg()
    model: Model = Model()
    scripted: Scripted
    limits: Limits = Limits()
    calibration: Calibration
    replay_trace: str | None = None  # runs/<exp>/<trial>/client.jsonl of the healthy reference trial

    @property
    def observe_end_s(self) -> float:
        return self.fault.start_s + self.fault.duration_s + self.windows.observe_s

    @property
    def drain_end_s(self) -> float:
        return self.arrivals.stop_s + self.windows.drain_deadline_s


def sha256_file(path: str | Path) -> str:
    return "sha256:" + hashlib.sha256((ROOT / path).read_bytes()).hexdigest()


def git_sha() -> str:
    try:
        sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True)
        dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=ROOT,
                               capture_output=True, text=True, check=True).stdout.strip()
        return sha.stdout.strip() + ("-dirty" if dirty else "")
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def image_digests() -> dict[str, str]:
    """Pinned digests from docker/compose.yml plus the local fielddesk image ID."""
    compose = (ROOT / "docker/compose.yml").read_text()
    out = {
        "postgres": re.search(r"image: postgres@(sha256:[0-9a-f]+)", compose).group(1),
        "toxiproxy": re.search(r"image: ghcr.io/shopify/toxiproxy@(sha256:[0-9a-f]+)", compose).group(1),
    }
    try:
        out["api"] = subprocess.run(["docker", "image", "inspect", "rebound-fielddesk", "--format", "{{.Id}}"],
                                    capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        out["api"] = "unknown"
    return out


def load(path: str | Path) -> Manifest:
    raw = yaml.safe_load((ROOT / path).read_text())
    return Manifest.model_validate(raw)


def resolve(m: Manifest) -> dict:
    """The manifest with everything filled in that affects behavior, ready for hashing."""
    resolved = json.loads(m.model_dump_json())
    resolved["backend"] = {**resolved.get("backend", {}), "images": image_digests(),
                           "postgres_conf": sha256_file("docker/postgres.conf"),
                           "backend_code": _tree_hash("backend")}
    return resolved


def _tree_hash(rel: str) -> str:
    h = hashlib.sha256()
    for p in sorted((ROOT / rel).rglob("*.py")):
        h.update(str(p.relative_to(ROOT)).encode())
        h.update(p.read_bytes())
    return "sha256:" + h.hexdigest()


def config_hash(resolved: dict) -> str:
    """Hash over the resolved manifest + prompt + tool schemas + model settings + images + data checksum.
    `backend.repo_sha` is excluded (a docs-only commit must not create a new configuration); code that changes
    behavior is covered by the backend tree hash, the prompt, the tool schemas and the harness tree hash."""
    m = json.loads(json.dumps(resolved))
    m.get("backend", {}).pop("repo_sha", None)
    parts = {
        "manifest": m,
        "prompt": sha256_file(resolved["model"]["prompt"]),
        "tools": sha256_file(resolved["model"]["tools"]),
        "model": resolved["model"],
        "images": resolved["backend"]["images"],
        "data": resolved["data"]["checksum"],
        "tasks": resolved["tasks"]["sha256"],
        "harness_code": _tree_hash("harness"),
    }
    return "sha256:" + hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()
