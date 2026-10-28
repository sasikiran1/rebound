"""Experiment manifests: YAML -> validated pydantic model -> resolved manifest + config hash.

`config_hash` = SHA-256 over the resolved manifest, the prompt file, the tool schemas, the model settings, the
image digests, the data checksum and the code that runs or judges a trial (runner, backend, oracle, schema, generator,
lockfile, container definitions). Per-trial fields (condition, gate, repeat, seed order) are not part of the
configuration; they are recorded next to it. `compat_fingerprint` drops only the declared treatment fields (fault
duration, fault on/off) so paired comparisons can be checked; `analysis_fingerprint` identifies the analyzer.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Literal

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
    # previous_response_id: provider-stored conversation. full_history: the runner resends visible messages and
    # function calls only (no reasoning items; kept for the historical explore-v1 trials). stateless_reasoning: the
    # runner resends every output item verbatim, including encrypted reasoning, with store=False (audit finding 1).
    continuation: Literal["previous_response_id", "full_history", "stateless_reasoning"] = "previous_response_id"
    reasoning_effort: str = "low"
    max_output_tokens: int = 2048
    parallel_tool_calls: bool = False
    prompt_cache_key: str = "rebound-agent-v1"
    prompt: str = "prompts/agent_v1.md"
    tools: str = "prompts/tools_v1.json"


class Scripted(Strict):
    # v1: the original policy (gives up after a first read fails its retries). robust_v2: deadline-aware, keeps
    # retrying with capped full-jitter backoff and Retry-After, re-reads after an uncertain write (audit finding 7).
    policy: Literal["v1", "robust_v2"] = "v1"
    think_time_s: float  # 0 = unpaced
    # robust_v2 only: draw each think time from the baseline-cohort step gaps of a recorded healthy agent trial
    # (runs/<exp>/<trial>/client.jsonl), matching the whole delay distribution rather than its median.
    think_samples_from: str | None = None
    max_retries: int = 2
    backoff_base_s: float = 0.5
    backoff_cap_s: float = 8.0
    retry_after_cap_s: float = 30.0
    request_budget: int = 20
    seed: int = 13


class AgentRuntime(Strict):
    """What the runtime around the model does, independent of the model's decisions. A separate treatment arm."""

    # Hold the goal's next HTTP attempt until the Retry-After of its last 503/429 has passed (capped). The model
    # still decides whether and what to call next; the runtime only enforces the server's back-pressure signal.
    retry_after_guard: bool = False
    retry_after_guard_cap_s: float = 10.0


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
    agent_runtime: AgentRuntime = AgentRuntime()
    scripted: Scripted
    limits: Limits = Limits()
    calibration: Calibration
    replay_trace: str | None = None  # runs/<exp>/<trial>/client.jsonl of the healthy reference trial
    # A faulted or otherwise unsuitable trace may be replayed only as a labeled mechanics test, never as a control.
    replay_mechanics_only: bool = False

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


def _files_hash(rels: list[str]) -> str:
    """Content hash of files and directories (all files below a directory), missing paths recorded as missing."""
    h = hashlib.sha256()
    for rel in rels:
        p = ROOT / rel
        files = sorted(x for x in p.rglob("*") if x.is_file() and "__pycache__" not in x.parts) if p.is_dir() else [p]
        for f in files:
            h.update(str(f.relative_to(ROOT)).encode())
            h.update(f.read_bytes() if f.exists() else b"<missing>")
    return "sha256:" + h.hexdigest()


# Code and inputs that decide what a trial does or how its goals are judged at run time. A change to any of them
# is a new configuration (audit 2026-10-27, finding 4: the oracle, schema, generator and lockfile were not hashed).
EXECUTION_INPUTS = ["oracle", "data/schema.sql", "data/indexes.sql", "data/generator", "uv.lock", "docker"]
# Rules applied after the trial (analysis, recovery definition, acceptance). Recorded per analysis run in
# verdict.json as `analysis_fingerprint`; never pool verdicts produced by different analyzers.
ANALYSIS_INPUTS = ["analysis", "docs/protocol.md"]
# Per-trial treatment values that a paired comparison may vary on purpose. Everything else must match.
TREATMENT_FIELDS = (("fault", "duration_s"), ("fault_enabled",))
CONFIG_HASH_VERSION = 2


def _identity_parts(resolved: dict) -> dict:
    m = json.loads(json.dumps(resolved))
    m.get("backend", {}).pop("repo_sha", None)
    return {
        "config_hash_version": CONFIG_HASH_VERSION,
        "manifest": m,
        "prompt": sha256_file(resolved["model"]["prompt"]),
        "tools": sha256_file(resolved["model"]["tools"]),
        "model": resolved["model"],
        "images": resolved["backend"]["images"],
        "data": resolved["data"]["checksum"],
        "tasks": resolved["tasks"]["sha256"],
        "harness_code": _tree_hash("harness"),
        "execution_inputs": _files_hash(EXECUTION_INPUTS),
    }


def config_hash(resolved: dict) -> str:
    """Hash over the resolved manifest + prompt + tool schemas + model settings + images + data checksum + runner,
    oracle, schema, generator, lockfile and container definitions. `backend.repo_sha` is excluded (a docs-only commit
    must not create a new configuration); backend code is covered by the resolved backend tree hash."""
    return "sha256:" + hashlib.sha256(json.dumps(_identity_parts(resolved), sort_keys=True).encode()).hexdigest()


def compat_fingerprint(resolved: dict) -> str:
    """Identity for paired comparisons: the configuration hash with only the declared treatment fields removed.
    Two trials may be compared or pooled only if their fingerprints are equal."""
    m = json.loads(json.dumps(resolved))
    for path in TREATMENT_FIELDS:
        node = m
        for key in path[:-1]:
            node = node.get(key, {})
        node.pop(path[-1], None)
    return "sha256:" + hashlib.sha256(json.dumps(_identity_parts(m), sort_keys=True).encode()).hexdigest()


def analysis_fingerprint() -> str:
    return _files_hash(ANALYSIS_INPUTS)
