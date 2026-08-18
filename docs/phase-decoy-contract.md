# PHASE → DECOY Execution Contract

## Ownership Boundary

PHASE decides **what behavior should be emitted**. RUSE DECOY validates the document and applies supported actuators; it must not infer new targets, optimize weights, or compensate for missing fields. The machine-readable capability inventory is `contracts/decoy/capabilities-v1.json` and the document shape is `contracts/decoy/behavior-v1.schema.json`.

New PHASE output should set:

```json
"_metadata": {
  "contract_version": "ruse.decoy.behavior/v1",
  "mode": "feedback",
  "sup_config": "B0R.gemma",
  "seed": 4185606330
}
```

RUSE rejects a declared version it does not support. Unversioned files remain readable only as a migration path for existing feedback artifacts.

## Emission Rules

Each SUP receives one complete `behavior.json`. PHASE must emit desired state, not a patch:

- `timing.active_minute_windows` uses UTC minute-of-day half-open ranges.
- `content.schedule` must cover hours `0..23` exactly once. Weights control selection mix; `workflow_budget.target_execs_per_hour` controls cadence.
- Values must use canonical workflow names from the capability manifest.
- Optional actuator omission means **disabled/default**, including prompt guidance and persistent traffic channels.
- Diagnostic/provenance fields may explain a decision but cannot change execution semantics.

PHASE should not emit fields listed under `accepted_but_not_actuated` expecting behavioral improvement. They are retained for measurement compatibility only.

## Consumption and Reload

The deployment engine validates that every non-control SUP has a source file, copies it atomically, and starts the service only after it lands. Runtime reload computes the file SHA-256 at each cluster boundary. Identical content is a no-op; a changed document is applied as a full replacement. Failed application does not advance the consumed digest, so it is retried and remains visible.

Removing a field restores native workflow defaults. Removing or disabling `persistent_sessions` or `connection_shape` stops its daemon. Prompt guidance is rebuilt from the immutable native prompt, preventing repeated `[PHASE Behavioral Guidance]` accumulation.

## Deployment and Canary Acceptance

Every DECOY run records `ruse-revision.txt`; Ansible checks out that exact 40-character SHA on all VMs and verifies `git rev-parse HEAD` before installation.

Use one complete single-target cohort as a canary:

```bash
RUSE_GIT_REF=<published-40-char-sha> ./deploy --decoy --feedback \
  --preset <namespace> --target <dataset> --gpu cpu
./audit --decoy
./teardown <exact-config-name>-<run-id>
```

The CPU tier is the inexpensive first pass (M2, B2C, S2C). Repeat on RTX/V100 only when validating GPU-specific installation, model loading, or workflows. Acceptance requires successful install assertions, behavior distribution, PHASE registration, active services, fresh logs, and a clean DECOY audit through an active window.
