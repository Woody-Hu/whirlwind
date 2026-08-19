# Whirlwind Agent Runtime

**English** | [中文](README.zh-CN.md)

A harness-agnostic, sandboxed agent runtime. It treats any agent harness (DeepSeek Harness / dsh, echo, or a custom loop) as a black-box process placed inside a managed sandbox. The platform uniformly handles session routing, sandbox scheduling, snapshot restore, pool prewarming, event streaming, and capability injection.

- **Language**: Python 3.12+ / asyncio / FastAPI (deps: only `fastapi`, `uvicorn`, `httpx`, `pydantic`)
- **Form factor**: single-process all-in-one (M1 vertical slice) → Linux cluster with multiple substrates (M3, evolving)
- **Docs**: [Architecture design](agent-runtime-architecture.md) · [ADR-0001 M1/M2 vertical slice](docs/adr/0001-agent-runtime-m1.md) · [ADR-0002 M3 sandbox substrate](docs/adr/0002-m3-substrates.md)

## Core Capabilities

| Capability | Description |
| --- | --- |
| **Harness-agnostic mounting** | The platform only deals with the "sandbox + events + capability contract". Harness images that satisfy the contract can be hot-plugged (dsh via stdio JSON-RPC; echo as the test baseline). The platform never modifies their code |
| **Sandbox as the execution unit** | All agent execution happens inside managed sandboxes. `SandboxDriver` is a single interface with multiple implementations; capability bits (isolation / snapshot / density / net_policy) are reported truthfully, and scheduling relies only on capability bits |
| **Snapshots & lifecycle** | Session-level suspend / resume: DATA snapshot (workspace layer) + FULL snapshot (runsc/CRIU memory + rootfs); the dsh side uses an adopt-or-create shim for playback |
| **Secrets never enter the sandbox** | Inside the sandbox there is only a relay placeholder (`DEEPSEEK_API_KEY=whirlwind-relay`); real credentials live only in the Hostlet's SecretRelay and replace the Authorization header on egress |
| **Capability Seam** | Skill / Tool / Memory platform concepts compile into Seam contracts (Definition / Provider / Consumer), injected into the sandbox via the Renderer; non-native harnesses fall back to the MCP Gateway |
| **Replayable** | Durable WAL event log: appends return only after fsync lands on disk; crash recovery truncates torn records; group commit amortizes fsync cost |
| **Multi-substrate communication** | The Hostlet ↔ SandboxAgent link is uniformly abstracted: TCP / Unix Domain Socket / virtio-vsock (microVM form) share the same API |

## Quick Start

```bash
# Install (uv or pip)
uv sync

# 1. Build the image (echo is the test baseline; dsh needs a local checkout, see ADR D6)
whirlwind image build echo

# 2. Start the all-in-one runtime
whirlwind serve --port 8410 --data-dir .whirlwind

# 3. Create an agent and start a conversation
whirlwind agent create demo --harness echo --image echo --seam fs.v1=sandbox-fs
whirlwind session create demo
whirlwind session send <sid> "hello" --stream   # SSE streaming events
whirlwind session events <sid>                  # replay from any seq

# 4. Session lifecycle
whirlwind session suspend <sid>                 # data snapshot + release sandbox
whirlwind session resume <sid>                  # restore from snapshot and resume
whirlwind cron add <agent_id> --schedule '*/5 * * * *' --input 'check in'
```

## Architecture Overview

```
Gateway (REST + SSE + MCP)      entry layer: session/event-stream/cron/image management
  └─ Control (SessionManager / Scheduler / WarmPool / Lifecycle)
       └─ Hostlet (ensure / bind / turn / pause / destroy + SecretRelay)
            └─ SandboxDriver ──→ sandbox (gVisor / process group)
                 └─ SandboxAgent (EventTap / ControlAgent / ResourceInjector / LLM Relay)
                      └─ Harness (dsh / echo / ...)
```

| Module | Responsibility |
| --- | --- |
| `core/` | Domain model: AgentDefinition / Version, AgentSession, SessionEvent, Sandbox, Snapshot, state machine |
| `storage/` | Six provider interfaces (Metadata / KV / EventLog / ObjectStore / EventBus / Lock) + in-memory / WAL / directory implementations |
| `transport/` | Transport abstraction: endpoint parsing + connect / serve for TCP / UDS / vsock |
| `drivers/` | `SandboxDriver` interface + process / runsc (gVisor) implementations |
| `hostlet/` | Node agent: sandbox lifecycle orchestration + SecretRelay (credential egress) |
| `agent/` | SandboxAgent: first process inside the sandbox (stdlib asyncio HTTP, no heavy framework dependency) |
| `harness/` | HarnessAdapter interface + echo baseline + dsh adapter (stdio JSON-RPC) |
| `seam/` | Seam model and Renderer (AgentVersion → injection manifest) |
| `imaging/` | ImageRegistry + LocalRegistry (building means real installation) |
| `control/` | Session management, scheduling, warm pool (CAS claim), lifecycle |
| `gateway/` | FastAPI: REST + SSE + MCP Gateway + CronScheduler |
| `timer/` | Kafka-style hierarchical time wheel + croniter delegation |
| `bus/` | In-process event bus (topic fan-out, seq cursor) |

## Sandbox Driver Matrix

| | process (M1) | runsc / gVisor (M3) |
| --- | --- | --- |
| Isolation level | PROCESS (process group + env allowlist + cwd restriction) | LIGHT_VM (user-space kernel, independent trust domain) |
| FULL snapshot (memory) | ✗ | ✓ runsc checkpoint (embedded CRIU) |
| DATA snapshot (workspace) | ✓ merkle validation | ✓ same as left |
| Network policy | ✗ | ✓ netstack (per-sandbox independent protocol stack) |
| Density | HIGH | HIGH |
| Platform | macOS / Linux | Linux only |

The runsc driver has been end-to-end validated on a real gVisor (release-20260810.0, systrap platform) across the full lifecycle: create / exec / pause / resume / checkpoint / destroy. Inside the sandbox, `uname -r` reports the Sentry kernel (`4.19.0-gvisor`) rather than the host kernel — empirical proof of isolation. Constrained containers (without `CAP_SYS_ADMIN`) automatically degrade to rootless + `--network=none`; restore is not yet supported in rootless mode (runsc upstream limitation), full-capability hosts can use it.

## Transport Layer

The link between the Hostlet and the SandboxAgent is expressed with a unified `Endpoint` abstraction:

```
tcp://127.0.0.1:8000            # M1 default (same-machine loopback)
unix:///run/whirlwind/agent.sock  # same-machine UDS (no port usage)
http+vsock://2:8000            # microVM form (CID 2 = host)
```

`vsock_available()` probes `/dev/vsock`; real virtio-vsock only exists on VM platforms (Firecracker / QEMU), so tests are skipped when unavailable.

## Testing

```bash
uv run python scripts/run_tests.py                    # full suite (unit+integration+benchmark, non-e2e)
uv run python scripts/run_tests.py tests/unit -q      # unit only (fastest feedback)
uv run python scripts/run_tests.py tests/integration/test_runsc_driver.py -q   # real gVisor sandbox
WHIRLWIND_E2E=1 uv run python scripts/run_tests.py tests/e2e -q   # real DeepSeek API (requires key)
```

Test strategy (ADR §6): **no mocking / faking / cheating** — integration tests run real subprocesses, real filesystems, and real local HTTP; runsc tests are skipped without the binary rather than stubbed. Benchmarks include the ADR acceptance lines: cold-start p50 ≤ 250ms, event log group-commit burst ≥ 5k/s, time wheel 10k schedules, bus fan-out 20k/s. All tests run through `scripts/run_tests.py` (ADR-0008): full output lands in `.test-logs/`, the console gets only a verdict summary.

Current Linux baseline (2026-08-20, see the [session-log](docs/session-logs/2026-08-20-linux-baseline-bench-hardening.md)): full suite 214 passed / 12 skipped (every skip has a stated reason: runsc/VM-class environment gaps or e2e without keys).
