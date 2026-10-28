"""One trial: PREPARING -> BASELINE -> FAULT_ACTIVE -> OBSERVING -> DRAINING -> ORACLE ->
ANALYZING -> COMPLETE, with exits to INVALID, INCONCLUSIVE or CANCELLED. Every path ends in CLEANUP (Toxiproxy
reset, cancel client tasks, flush and hash files).

Time 0 is the moment background traffic and goal arrivals start; all phase times are offsets from it.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import os
import shutil
import statistics
import time
from pathlib import Path

import asyncpg
import httpx

from harness import dbadmin, settings, sut
from harness.adapter import ToolAdapter, load_tools, new_http_client
from harness.agent import AgentConfig
from harness.agent import run_goal as run_agent_goal
from harness.events import EventLog, goal_ids
from harness.fault import Canary, Toxiproxy
from harness.load import Background, RunnerHealth, poisson_arrivals
from harness.manifest import ROOT, Manifest, config_hash, git_sha, load, resolve
from harness.replay import load_trace
from harness.replay import run_goal as run_replay_goal
from harness.scripted import ScriptedConfig
from harness.scripted import run_goal as run_scripted_goal
from oracle.verdicts import verdict_for

CONDITIONS = ("agent", "scripted", "replay")
MIN_FREE_GB = 10.0


class TrialAbort(Exception):
    def __init__(self, state: str, reason: str):
        super().__init__(reason)
        self.state, self.reason = state, reason


def load_tasks(m: Manifest) -> list[dict]:
    path = ROOT / m.tasks.file
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != m.tasks.sha256:
        raise TrialAbort("INVALID", f"task file {m.tasks.file} sha256 {digest} != manifest {m.tasks.sha256}")
    return [json.loads(line) for line in path.read_text().splitlines()]


def next_trial_id(exp_dir: Path) -> str:
    exp_dir.mkdir(parents=True, exist_ok=True)
    n = sum(1 for p in exp_dir.iterdir() if p.is_dir() and p.name.startswith("tr_"))
    return f"tr_{n + 1:04d}"


def sha256sums(trial_dir: Path) -> None:
    lines = []
    for p in sorted(trial_dir.rglob("*")):
        if p.is_file() and p.name != "SHA256SUMS":
            lines.append(f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.relative_to(trial_dir)}")
    (trial_dir / "SHA256SUMS").write_text("\n".join(lines) + "\n")


def verify_sums(trial_dir: Path) -> list[str]:
    bad = []
    for line in (trial_dir / "SHA256SUMS").read_text().splitlines():
        digest, name = line.split("  ", 1)
        p = trial_dir / name
        if not p.exists() or hashlib.sha256(p.read_bytes()).hexdigest() != digest:
            bad.append(name)
    return bad


class Trial:
    def __init__(self, manifest_path: str, condition: str, gate_on: bool, experiment: str | None = None,
                 repeat: int | None = None, block: int | None = None, label: str | None = None,
                 fault_enabled: bool | None = None, fault_duration_s: float | None = None):
        if condition not in CONDITIONS:
            raise ValueError(f"condition must be one of {CONDITIONS}")
        self.m = load(manifest_path)
        if fault_enabled is not None:
            self.m.fault_enabled = fault_enabled
        if fault_duration_s is not None:
            self.m.fault.duration_s = fault_duration_s
        self.manifest_path = manifest_path
        self.condition, self.gate_on = condition, gate_on
        self.experiment = experiment or self.m.experiment
        self.exp_dir = ROOT / "runs" / self.experiment
        self.trial_id = next_trial_id(self.exp_dir)
        self.dir = self.exp_dir / self.trial_id
        self.dir.mkdir(parents=True)
        self.resolved = resolve(self.m)
        self.resolved["backend"]["repo_sha"] = git_sha()
        self.config_hash = config_hash(self.resolved)
        self.meta = {
            "trial_id": self.trial_id, "experiment_id": self.experiment, "condition": condition,
            "gate": "on" if gate_on else "off", "repeat": repeat, "block": block, "label": label,
            "manifest_path": manifest_path, "config_hash": self.config_hash, "states": [], "invalid_reasons": [],
            "inconclusive_reasons": [], "runner_pid": os.getpid(), "runner_affinity": sorted(os.sched_getaffinity(0)),
        }
        self.events = EventLog(self.dir, experiment_id=self.experiment, trial_id=self.trial_id, condition=condition,
                               gate=self.meta["gate"], config_hash=self.config_hash)
        self.outcomes: dict[int, str] = {}
        self.scheduled: dict[int, dict] = {}
        self.spent_usd = 0.0
        self.cancel = asyncio.Event()
        self.t0 = 0.0

    # --- bookkeeping -------------------------------------------------------------------------------------------
    def state(self, name: str, **info) -> None:
        self.meta["states"].append({"state": name, "ts_wall": dt.datetime.now(dt.UTC).isoformat(),
                                    "t": round(time.monotonic() - self.t0, 3) if self.t0 else None, **info})
        self.events.emit("trial.state", state=name, **info)
        print(f"[{self.trial_id}] {name} {json.dumps(info) if info else ''}", flush=True)
        self.save_meta()

    def save_meta(self) -> None:
        (self.dir / "trial.json").write_text(json.dumps(self.meta, indent=2, default=str) + "\n")

    def elapsed(self) -> float:
        return time.monotonic() - self.t0

    async def until(self, t: float) -> None:
        while True:
            remaining = self.t0 + t - time.monotonic()
            if remaining <= 0:
                return
            if self.cancel.is_set():
                raise TrialAbort("CANCELLED", self.meta.get("cancel_reason", "cancelled"))
            await asyncio.sleep(min(remaining, 0.5))

    # --- phases ------------------------------------------------------------------------------------------------
    async def prepare(self) -> None:
        self.state("PREPARING")
        (self.dir / "manifest.resolved.json").write_text(json.dumps(self.resolved, indent=2) + "\n")
        (self.dir / "config_hash").write_text(self.config_hash + "\n")
        self.tasks = load_tasks(self.m)
        async with httpx.AsyncClient(timeout=10) as c:
            await Toxiproxy(c).reset()
        fp = await dbadmin.clone_trial_db()
        if fp["checksum"] != self.m.data.checksum:
            raise TrialAbort("INVALID", f"data checksum {fp['checksum']} != manifest {self.m.data.checksum}")
        self.n_appointments = fp["tables"]["appointments"]["rows"]
        await asyncio.to_thread(sut.restart_fielddesk, sut.fielddesk_env(self.m, self.gate_on))
        env = await asyncio.to_thread(sut.container_env, "fielddesk")
        if env.get("GATE_ENABLED") != ("true" if self.gate_on else "false"):
            raise TrialAbort("INVALID", f"FieldDesk GATE_ENABLED={env.get('GATE_ENABLED')} does not match gate")
        await self.preflight()
        self.cpu_before = {n: await asyncio.to_thread(sut.cpu_stat, n) for n in ("postgres", "fielddesk")}

    async def preflight(self) -> None:
        checks = {}
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(settings.FIELDDESK_URL + "/healthz")
            checks["fielddesk_health"] = r.status_code == 200
            tox = await Toxiproxy(c).toxics(self.m.fault.proxy)
            checks["no_toxics"] = tox == []
        canary = Canary()
        await canary.open()
        checks["canary_ms"] = round(await canary.probe(), 2)
        await canary.close()
        cpus = await asyncio.to_thread(sut.cpusets)
        runner = set(os.sched_getaffinity(0))
        checks["cpusets"] = cpus
        checks["cpu_separation"] = not (runner & (sut.parse_cpuset(cpus["postgres"]) |
                                                  sut.parse_cpuset(cpus["fielddesk"])))
        # The WSL and Docker virtual disks live on the Windows drive; when it filled up mid-trial (explore-v1
        # tr_0010) every write failed. Require headroom on both the Linux and the host filesystem.
        for name, path in (("disk_free_gb_root", "/"), ("disk_free_gb_host", "/mnt/c")):
            if os.path.exists(path):
                checks[name] = round(shutil.disk_usage(path).free / 1e9, 1)
                if checks[name] < MIN_FREE_GB:
                    checks[f"{name}_ok"] = False
        if self.condition == "agent":
            checks["model_key"] = bool(os.environ.get("MODEL_API_KEY"))
        if self.condition == "replay":
            checks["replay_trace"] = bool(self.m.replay_trace and (ROOT / self.m.replay_trace).exists())
        self.meta["preflight"] = checks
        failed = [k for k, v in checks.items() if v is False]
        if failed:
            raise TrialAbort("INVALID", f"preflight failed: {failed}")

    def phase_of(self, t: float) -> tuple[str, str]:
        f = self.m.fault
        w = self.m.windows
        if t < w.warmup[1]:
            phase = "warmup"
        elif t < f.start_s:
            phase = "baseline"
        elif t < f.start_s + f.duration_s:
            phase = "fault"
        elif t < self.m.observe_end_s:
            phase = "observe"
        else:
            phase = "drain"
        fault_state = "pre" if t < f.start_s else ("during" if t < f.start_s + f.duration_s else "post")
        return phase, fault_state

    async def context_updater(self) -> None:
        while True:
            phase, fs = self.phase_of(self.elapsed())
            self.events.context.update(phase=phase, fault_state=fs)
            await asyncio.sleep(0.2)

    async def canary_loop(self) -> None:
        canary = Canary()
        await canary.open()
        try:
            i = 0
            while True:
                await asyncio.sleep(max(0.0, self.t0 + i - time.monotonic()))
                try:
                    async with asyncio.timeout(5):
                        ms = await canary.probe()
                    self.events.emit("fault.canary", latency_ms=round(ms, 2), t=round(self.elapsed(), 3))
                    self.canary.append((self.elapsed(), ms))
                except TimeoutError:
                    self.events.emit("fault.canary", latency_ms=None, status="timeout", t=round(self.elapsed(), 3))
                i += 1
        finally:
            await canary.close()

    def baseline_guard(self) -> dict:
        """At fault time: background success >= 99% and p95 <= 1.5x calibrated healthy p95 (backend side)."""
        a, b = self.m.windows.baseline
        lat, ok = [], 0
        path = sut.LOG_DIR / "backend.jsonl"
        for line in path.read_text().splitlines() if path.exists() else []:
            r = json.loads(line)
            if r.get("trial_id") != self.trial_id or r.get("actor_id") != "background":
                continue
            t = (r["ts_mono_ns"] / 1e9) - self.t0
            if a <= t < b:
                lat.append(r["total_ms"])
                ok += 200 <= r["http_status"] < 300 and r["total_ms"] <= self.m.background.timeout_s * 1000
        n = len(lat)
        p95 = sorted(lat)[min(n - 1, round(0.95 * (n - 1)))] if n else float("nan")
        res = {"n": n, "success": ok / n if n else 0.0, "p95_ms": p95,
               "limit_p95_ms": 1.5 * self.m.calibration.healthy_bg_p95_ms}
        res["ok"] = n > 0 and res["success"] >= 0.99 and p95 <= res["limit_p95_ms"]
        return res

    async def fault_sequence(self) -> None:
        f = self.m.fault
        await self.until(f.start_s)
        guard = await asyncio.to_thread(self.baseline_guard)
        self.meta["baseline_guard"] = guard
        if not guard["ok"]:
            raise TrialAbort("INVALID", f"baseline unhealthy: {guard}")
        self.state("FAULT_ACTIVE", enabled=self.m.fault_enabled)
        async with httpx.AsyncClient(timeout=10) as c:
            tox = Toxiproxy(c)
            if self.m.fault_enabled:
                await tox.add_latency(f.proxy, f.latency_ms, f.jitter_ms)
                self.events.emit("fault.applied", proxy=f.proxy, latency_ms=f.latency_ms, jitter_ms=f.jitter_ms,
                                 t=round(self.elapsed(), 3))
            await self.until(f.start_s + f.duration_s)
            if self.m.fault_enabled:
                await tox.remove(f.proxy)
                self.events.emit("fault.removed", proxy=f.proxy, t=round(self.elapsed(), 3))
        a, b = self.m.windows.baseline
        base = [ms for t, ms in self.canary if a <= t < b]
        during = [ms for t, ms in self.canary if f.start_s + 1 <= t < f.start_s + f.duration_s]
        rise = statistics.median(during) - statistics.median(base) if base and during else float("nan")
        self.meta["canary_rise_ms"] = round(rise, 1)
        if self.m.fault_enabled:
            self.meta["fault_verified"] = bool(rise >= 0.7 * f.latency_ms)
            if not self.meta["fault_verified"]:
                raise TrialAbort("INVALID", f"fault not verified: canary rise {rise:.1f} ms")
        self.state("OBSERVING")

    async def run_one_goal(self, n: int, task: dict, sem: asyncio.Semaphore, clients: dict) -> None:
        ids = goal_ids(n)
        kind = {"agent": "agent", "scripted": "scripted", "replay": "replay"}[self.condition]
        actor = {**ids, "actor_id": f"{kind}_{n:04d}", "actor_kind": kind}
        self.events.emit("goal.scheduled", **actor, task_id=task["task_id"], kind=task["kind"],
                         expected=task["expected"], t_sched=round(self.elapsed(), 3))
        self.scheduled[n] = task
        try:
            async with sem:
                if self.condition == "agent":
                    out = await run_agent_goal(task, n, self.trial_id, clients["provider"], clients["adapter"],
                                               self.events, clients["tools"], clients["agent_cfg"],
                                               on_step=self.on_agent_step)
                elif self.condition == "scripted":
                    out = await run_scripted_goal(task, n, self.trial_id, clients["adapter"], self.events,
                                                  clients["scripted_cfg"])
                else:
                    out = await run_replay_goal(task, n, self.trial_id, clients["trace"].get(n, []),
                                                clients["adapter"], self.events)
            self.outcomes[n] = out.end_reason
        except asyncio.CancelledError:
            self.outcomes[n] = "unfinished"
            self.events.emit("goal.ended", **actor, task_id=task["task_id"], outcome="unfinished")

    def on_agent_step(self, step: dict) -> None:
        self.spent_usd += step["cost_usd"]
        if self.spent_usd > self.m.limits.trial_budget_usd and not self.cancel.is_set():
            self.meta["cancel_reason"] = f"spend ${self.spent_usd:.2f} over trial cap ${self.m.limits.trial_budget_usd}"
            self.cancel.set()

    async def arrivals(self, clients: dict, goal_tasks: list[asyncio.Task]) -> None:
        a = self.m.arrivals
        times = poisson_arrivals(a.goals_per_s, a.seed, a.start_s, a.stop_s)
        if len(times) > len(self.tasks):
            raise TrialAbort("INVALID", f"{len(times)} arrivals but only {len(self.tasks)} tasks")
        self.meta["arrivals_planned"] = len(times)
        sem = asyncio.Semaphore(self.m.limits.max_concurrent_goals)
        for n, t in enumerate(times, start=1):
            await self.until(t)
            goal_tasks.append(asyncio.create_task(self.run_one_goal(n, self.tasks[n - 1], sem, clients)))

    async def run(self) -> Path:
        final = "COMPLETE"
        background = health_task = None
        side: list[asyncio.Task] = []
        goal_tasks: list[asyncio.Task] = []
        http = None
        try:
            await self.prepare()
            http = new_http_client()
            clients = {"adapter": ToolAdapter(settings.FIELDDESK_URL, http, load_tools(self.m.model.tools),
                                              self.events)}
            clients["tools"] = load_tools(self.m.model.tools)
            if self.condition == "agent":
                from harness.provider import Provider, make_client
                clients["provider"] = Provider(make_client())
                lim = self.m.limits
                clients["agent_cfg"] = AgentConfig(
                    model=self.m.model.id, reasoning_effort=self.m.model.reasoning_effort,
                    max_output_tokens=self.m.model.max_output_tokens, prompt_cache_key=self.m.model.prompt_cache_key,
                    prompt_path=self.m.model.prompt, max_model_calls=lim.model_calls_per_goal,
                    max_tool_calls=lim.tool_calls_per_goal, goal_deadline_s=lim.goal_deadline_s,
                    max_input_tokens=lim.input_tokens_per_goal, continuation=self.m.model.continuation)
            elif self.condition == "scripted":
                s = self.m.scripted
                clients["scripted_cfg"] = ScriptedConfig(
                    think_time_s=s.think_time_s, max_retries=s.max_retries, backoff_base_s=s.backoff_base_s,
                    backoff_cap_s=s.backoff_cap_s, retry_after_cap_s=s.retry_after_cap_s,
                    request_budget=s.request_budget, goal_deadline_s=self.m.limits.goal_deadline_s, seed=s.seed)
            else:
                clients["trace"] = load_trace(ROOT / self.m.replay_trace)
            self.canary: list[tuple[float, float]] = []

            # BASELINE (covers warm-up): start background, arrivals, canary, runner health.
            self.t0 = time.monotonic()
            self.meta["t0_mono_ns"] = int(self.t0 * 1e9)
            self.meta["t0_wall"] = dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z")
            self.state("BASELINE")
            bgc = self.m.background
            background = Background(settings.FIELDDESK_URL, bgc.rate_rps, bgc.mix, bgc.timeout_s, bgc.seed,
                                    self.events, self.n_appointments, self.trial_id)
            bg_task = asyncio.create_task(background.run(self.t0, 0, self.m.drain_end_s))
            health = RunnerHealth(self.events)
            health_task = asyncio.create_task(health.run())
            side = [bg_task, health_task, asyncio.create_task(self.context_updater()),
                    asyncio.create_task(self.canary_loop())]
            arrivals = asyncio.create_task(self.arrivals(clients, goal_tasks))
            fault = asyncio.create_task(self.fault_sequence())
            side += [arrivals, fault]
            waiter = asyncio.create_task(self.cancel.wait())
            side.append(waiter)
            watch = {fault, arrivals, waiter}
            while not (fault.done() and arrivals.done()):
                done, watch = await asyncio.wait(watch, return_when=asyncio.FIRST_COMPLETED)
                for t in done:
                    if t is waiter:
                        raise TrialAbort("CANCELLED", self.meta.get("cancel_reason", "cancelled"))
                    if t.exception():
                        raise t.exception()
            await self.until(self.m.observe_end_s)

            self.state("DRAINING")
            pending = [t for t in goal_tasks if not t.done()]
            if pending:
                remaining = self.t0 + self.m.drain_end_s - time.monotonic()
                done, pending = await asyncio.wait(pending, timeout=max(0.0, remaining))
                for t in pending:
                    t.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
            self.meta["drain_end_t"] = round(self.elapsed(), 1)
            if self.cancel.is_set():
                raise TrialAbort("CANCELLED", self.meta.get("cancel_reason", "cancelled"))
        except TrialAbort as exc:
            final = exc.state
            key = "invalid_reasons" if exc.state == "INVALID" else "inconclusive_reasons"
            self.meta[key].append(exc.reason)
            if exc.state == "CANCELLED":
                self.meta["cancel_reason"] = exc.reason
            self.state(exc.state, reason=exc.reason)
        finally:
            for t in goal_tasks:
                t.cancel()
            await asyncio.gather(*goal_tasks, return_exceptions=True)
            for t in side:
                t.cancel()
            await asyncio.gather(*side, return_exceptions=True)
            if background is not None:
                await background.close()
            if http is not None:
                await http.aclose()
            async with httpx.AsyncClient(timeout=10) as c:
                try:
                    await Toxiproxy(c).reset()
                except httpx.HTTPError as exc:
                    self.meta["invalid_reasons"].append(f"toxiproxy reset failed: {exc}")

        # ORACLE (also for invalid or cancelled trials, so every scheduled goal has a recorded verdict).
        self.state("ORACLE")
        try:
            await self.run_oracle()
        except Exception as exc:  # noqa: BLE001 - record and continue to analysis
            self.meta["inconclusive_reasons"].append(f"oracle failed: {exc!r}")
            if final == "COMPLETE":
                final = "INCONCLUSIVE"
        await asyncio.sleep(1.0)  # let FieldDesk's line-buffered logs settle
        sut.copy_logs(self.dir)
        cpu_after = {n: await asyncio.to_thread(sut.cpu_stat, n) for n in ("postgres", "fielddesk")}
        before = getattr(self, "cpu_before", {})
        self.meta["throttling_delta"] = {
            n: ({k: cpu_after[n][k] - before[n][k] for k in ("usage_usec", "nr_throttled", "throttled_usec")}
                if before.get(n) and cpu_after.get(n) else None) for n in ("postgres", "fielddesk")}
        self.meta["spent_usd"] = round(self.spent_usd, 4)
        self.meta["goals_scheduled"] = len(self.scheduled)
        self.events.flush()

        self.state("ANALYZING")
        self.meta["final_state"] = final
        self.save_meta()
        try:
            from analysis.metrics import analyze
            from analysis.report import trial_report
            result = analyze(self.dir)
            trial_report(self.dir, result)
            if final == "COMPLETE" and result["status"] != "valid":
                final = "INVALID" if result["status"] == "invalid" else "INCONCLUSIVE"
        except Exception as exc:  # noqa: BLE001
            self.meta["inconclusive_reasons"].append(f"analysis failed: {exc!r}")
            final = "INCONCLUSIVE" if final == "COMPLETE" else final
        self.meta["final_state"] = final
        self.state(final)
        self.state("CLEANUP")
        self.events.close()
        self.save_meta()
        sha256sums(self.dir)
        return self.dir

    async def run_oracle(self) -> None:
        conn = await asyncpg.connect(settings.oracle_dsn())
        try:
            for n, task in sorted(self.scheduled.items()):
                end_reason = self.outcomes.get(n, "unfinished")
                v = await verdict_for(conn, task, end_reason)
                self.events.emit("oracle.verdict", goal_id=goal_ids(n)["goal_id"], task_id=task["task_id"],
                                 verdict=v["verdict"],
                                 end_reason=v["end_reason"], completed_at=v["completed_at"], facts=v["facts"])
        finally:
            await conn.close()


def run(manifest: str, condition: str, gate: str, **kw) -> Path:
    import uvloop

    async def main() -> Path:
        return await Trial(manifest, condition, gate == "on", **kw).run()

    # uvloop: the runner must not saturate its event loop while hundreds of background requests are in flight.
    return uvloop.run(main())
