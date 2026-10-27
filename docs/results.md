# Results log

Numbers only. Each day appends what was measured.

## Day 1 · Tue Oct 27, 2026

### Environment (step 0)

| Item | Value |
|---|---|
| Host | Windows 11 Home 10.0.26300, Intel Core Ultra 9 275HX (8 P + 16 E cores, no SMT) |
| WSL | Ubuntu **26.04 LTS**, kernel 6.6.87.2-microsoft-standard-WSL2 |
| `nproc` (WSL) | 24 |
| Memory visible to WSL | 15 GiB total, 14 GiB available |
| Docker | Engine 29.2.1 (Docker Desktop, WSL integration), Compose v5.1.0, `hello-world` OK |
| Python / uv (WSL) | 3.12.16 (installed by uv) / uv 0.13.6 |
| Repo location | `~/rebound` (Linux filesystem) |

Proposed CPU split (vCPU indices inside the WSL VM):

| Component | cpuset |
|---|---|
| postgres | 2-3 |
| fielddesk | 4-5 |
| toxiproxy | 6 |
| runner (`taskset`) | 8-15 |
| left free (kernel, dockerd, desktop) | 0-1, 7, 16-23 |

Note: the CPU is hybrid (P + E cores). Hyper-V schedules WSL vCPUs onto host cores, so pinning inside
the VM does not guarantee a container stays on P-cores. This is a known variance source; baseline p95
stability across healthy runs (Day 4 check) is what decides whether it matters.

Project renamed from "Aftershock" to **Rebound** on Day 1 (identity headers `X-RB-*`, cache key `rebound-agent-v1`).
