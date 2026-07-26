# vn.py Agent Console: Side Master Guidance

The Agent Console is the operator-facing vn.py application for authenticated Side Master
conversation, exact guidance confirmation, main Master acknowledgement, effective guidance,
resources, recovery, retention, safe-boundary receipts, and health. It is a non-blocking Qt client
of the Rust `agentd` supervisor. vn.py remains the only trading, risk, and strategy-lifecycle
authority.

## Setup

Use Python 3.10+ and install the pinned test/runtime dependencies from the vn.py repository:

```powershell
python -m pip install -e ".[agent-guidance-test]"
```

Build `agentd`, `agentctl`, and the PyO3 bridge from the exact locked Rust revision:

```powershell
Set-Location ..\auto-tride-rust
cargo build --locked -p agentd -p agentctl -p vnpy-bridge-py
$env:AGENT_WORKSPACE_ROOT = (Resolve-Path ..).Path
cargo run --locked -p agentd -- serve
```

Register `AgentConsoleApp` with the normal vn.py `MainEngine` and open **Agent Console** from the
vn.py application menu. `AGENT_WORKSPACE_ROOT` must identify the shared workspace containing
`.agent-state`, the Rust binaries, and the Agent assets. The UI launches `agentctl` through
`QProcess`; it does not block the EventEngine or execute model calls in an event callback.

## OS-session authentication

`OsSessionIdentityProvider` verifies the current interactive login and binds each command to:

- an opaque operator digest derived from the Windows SID or Linux UID;
- the current desktop/login-session epoch and lock state;
- the local IPC peer digest;
- the exact request digest and a maximum 60-second assertion lifetime; and
- a revocation revision advanced by lock, logout, user switch, or session replacement.

The provider writes only a public trust anchor under `.agent-state`. Raw SID/UID, login-session
value, peer value, and the Ed25519 private key are never included in a request, projection, metric,
log, or CLI argument. An unverifiable or headless process identity is not sufficient. After a lock,
logout, or user switch, existing accepted history remains visible, but new turns and confirmations
stay blocked until a new verified interactive session is established.

## Operator workflow

For a new mission, initial guidance is mandatory before autonomous Master work can begin. Start
from any template or from a blank custom body, freely rename/delete/nest fields, discuss it in the
isolated side thread, prepare the exact content, inspect the digest preview, and confirm it. Startup
opens only after the main Master records a semantic acknowledgement; the independent observer gate
and every normal safety gate remain unchanged.

For a running mission:

1. Open a Side Master session against the displayed immutable mission snapshot.
2. Enter arbitrary UTF-8 text or JSON. The inline body limit is 32 KiB; a session is limited to 128
   turns and 1 MiB of canonical transcript.
3. Use **Prepare** to create an immutable draft. The user-approved body and optional Side Master
   interpretation have separate identities.
4. Verify the visible body and digest, then use **Send**. Model text alone cannot publish.
5. Follow transport, acknowledgement, conflict, effective-guidance, boundary, and terminal state in
   the workspace. A pending notification may be cancelled; a delivered one requires a new
   withdrawal or replacement notification.

Unsent dialogue, abandoned drafts, and model inferences remain isolated from the main Master's
context, short/long-term memory, workflows, Skills, Tools, CLI, MCP, workers, and actions. The side
session uses the same frozen `gpt-5.6-sol` route as the main Master with a distinct
`side-master:<session>` identity and no fallback. It has no Tool, CLI, MCP, browser, worker,
lifecycle, Audit, risk, or broker capability.

## Dynamic bodies and CLI diagnostics

Normal operation uses the vn.py UI, which builds the signed request envelope. Automation may call
the same versioned interface with a bounded signed JSON file or stdin:

```powershell
agentctl guidance health
agentctl guidance templates --request .\signed-templates-request.json
agentctl guidance open --request .\signed-open-request.json
agentctl guidance turn --request .\signed-turn-request.json
agentctl guidance prepare --request .\signed-prepare-request.json
agentctl guidance send --request .\signed-send-request.json
agentctl guidance inspect --request .\signed-inspect-request.json
agentctl guidance recover --request .\signed-recover-request.json
agentctl guidance disable --request .\signed-disable-request.json
```

`--request -` reads at most 128 KiB from stdin. Never put credentials, raw OS identity, signing
keys, or the guidance body in command-line arguments. Every mutating request uses an expected
revision, deadline, operation ID, idempotency key, payload digest, and current OS-session assertion.
Same-key/same-digest replay returns the stored result; a different digest fails closed.

Resource inspection and Master-only mutation use the separate contract:

```powershell
agentctl resources inspect --request .\resource-inspect.json
agentctl resources plan --request .\resource-plan.json
agentctl resources grant --request .\resource-grant.json
agentctl resources reclaim --request .\resource-reclaim.json
agentctl resources rebalance --request .\resource-rebalance.json
agentctl resources forecast --request .\resource-forecast.json
agentctl resources starvation --request .\resource-starvation.json
```

The main Master may mutate only unprotected allocations. vn.py, Side Master, Workers, Audit Agents,
and model-authored text have read-only resource visibility. Independent Audit, workflow-control,
and recovery minima cannot be reclaimed, redirected, relabeled, or starved. The console displays
the full mission envelope, uncertain reservations, forecasts, deficits, and blocking findings.

## Safe boundaries and priority

Confirmed guidance is routine, durable, ordered, non-coalescible traffic. It is considered after
the current atomic model, Tool, CLI, or subagent action completes and before any dependent model
turn, workflow node, Tool/CLI call, or subagent dispatch. Dynamic workflows must expose each child
boundary. Guidance does not cancel an in-flight action by itself. Critical market, independent-risk,
emergency-stop, and recovery events retain higher priority.

The UI shows eligible sequence watermarks, preceding action identity, boundary state, processing
lag, and acknowledgement. Accepted status changes should be visible within 2 seconds, and an
acknowledgement within 5 seconds of the next eligible boundary.

## Retention, recovery, and disablement

Unconfirmed turns and drafts are retained until exactly 90 days after session close. Confirmed
notifications and effective history are retained until 10 years after both the mission and every
guidance-influenced derived strategy terminate. Holds, incomplete lifecycle evidence, or failed
physical-purge verification block deletion and remain visible. Purge receipts contain identities
and digests, never deleted content.

Confirmed notifications, acknowledgements, and effective revisions have RPO=0. Dirty unconfirmed
state has RPO<=5 seconds. Once local processes and durable storage are ready after restart, the
authoritative projection must be visible within 10 seconds and pending work must resume or report a
specific actionable block within 30 seconds. Unknown provider outcomes preserve uncertain resource
use and are never silently replayed.

**Disable** stops new sessions and delivery but does not delete accepted history or alter market
observation, hard risk, emergency stop, strategy lifecycle, orders, or positions. Stale, malformed,
secret-bearing, incompatible, or out-of-order projections cannot replace the last known valid view.

## Trading authority

Natural-language guidance is research intent, never an order, risk command, model decision, Audit
approval, release gate, or lifecycle transition. Side Master and Rust do not receive broker
credentials and cannot submit/cancel orders, mutate positions or risk, clear market action points,
approve artifacts, or promote strategies. Simulation, paper, shadow, gray, and production requests
remain exact-version model/lifecycle operations enforced by independent Audit, deterministic policy,
hard risk, emergency stop, and vn.py.

## Verification

From the workspace root:

```powershell
Set-Location auto-tride-rust
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File scripts\run_side_guidance_acceptance.ps1 -Case all

Set-Location ..\vnpy
python -m pytest -q --import-mode=importlib tests\agent_bridge
```

The cost-bearing live Harness is not part of the default command. It must be explicitly enabled
with the frozen suite, workspace `.env`, three runs, and the acceptance runner's live flag.
