---
name: decoy-deploy
description: DECOY SUP deployment — running ./deploy --decoy [scope flags], 5-phase spinup, behavior.json plumbing, audit semantics, hot-patch path. Four tiers via --gpu {v100,rtx,rtx-a,cpu} (V100 → gemma4:26b, RTX 2080 Ti non-A pool → gemma4:e4b on B2R/S2R, RTX 2080 Ti A-pool → same model, separate physical cards, cpu → card-free M2+B2C+S2C on gemma4:e2b). Inputs deployments/decoy-controls/config.yaml + /data/axes-mirror/feedback/decoy-controls/{controls (un-namespaced), {preset}_v{version}/{dataset} (feedback, needs --preset)}/. Outputs deployments/decoy-{controls,feedback-...}/runs/{run_id}/. Does NOT cover RAMPART AD enterprise (see /rampart-deploy) or GHOSTS NPC clients (see /ghosts-deploy). Cross-type CLI shape, fail-loud contract, and SSH key matrix live in CLAUDE.md.
type: skill
---

# decoy-deploy

DECOY = synthetic user persona VMs that simulate human computer use. Each
SUP is a brain (MCHP / BrowserUse / SmolAgents) optionally driven by a
PHASE-tuned `behavior.json`. Brain naming scheme `[Brain][Version].[Model]`
is in CLAUDE.md.

| | |
|---|---|
| Inputs | `deployments/decoy-controls/config.yaml`, `/data/axes-mirror/feedback/decoy-controls/controls/{behavior}/{sup}/behavior.json` (baseline, **un-namespaced**), `/data/axes-mirror/feedback/decoy-controls/{preset}_v{version}/{dataset}/{behavior}/{sup}/behavior.json` (feedback, namespaced 2026-06 — needs `--preset`), `INSTALL_SUP.sh` + `decoys/` cloned from github at install time |
| Outputs | `deployments/decoy-{controls,feedback-{preset}-{dataset}-{scope}}/runs/{run_id}/` (inventory.ini, ssh_config_snippet.txt, deployment_type), per-VM `/opt/ruse/deployed_sups/{key}/`. `{preset}` = sanitized full-ns token incl. version (`stdctrlsv712`), so different lineages/versions don't collide |
| Manifest | `manifest.json` in PHASE source; loaded via `core/feedback.py::load_manifest`, validated against deploy type via `validate_manifest_target` |
| Upstream | PHASE feedback engine (`feedback_engine.baseline` writes `controls/`; `feedback_engine.decoy_generator` writes `{dataset}/`) |
| Downstream | PHASE Zeek pipeline (`PHASE.py --decoy`), reads `experiments.json` for active deploys (carries `dataset`/`scope`/`gpu_tier` descriptive fields since 2026-05-22, + `preset` sanitized-namespace token since 2026-06; see CLAUDE.md "experiments.json schema"). **Dredge is a HISTORICAL op — it iterates EVERY decoy entry (active + torn-down), not just live ones**, re-dredging stored Zeek conn.log bounded by `[start_date, end_date]`. **Gotcha:** `stage2_dredge.py` does `exit 1` (aborts the WHOLE run) when an entry's window yields 0 conn rows. A same-day deploy/teardown (`start_date == end_date`, no traffic captured) leaves a 0-row entry that kills the pipeline. RUSE-side fix = remove the invalid entry from `experiments.json` (back it up first; the key is RUSE-owned state, NOT PHASE); durable fix = PHASE skip-on-empty in `stage2_dredge.py` (PHASE-side, can't touch). Confirmed 2026-06-07: a June-3 `decoy-feedback-expctrlsv716-vt50g-all` (0-day span) aborted PHASE at [14/26]; removed it, run cleared. |
| Narrow exceptions | C0 (no install — bare Ubuntu, only provisioned + SSH-tested); M0 (upstream MITRE pyhuman, crash-loops on Linux by design — `os.startfile()` Windows-only) |

## Topology

`decoy-controls/config.yaml` provisions 9 VMs (gemma-only; V100 + RTX + CPU pairs):

```
d-{dep_id}-C0-0          Bare Ubuntu control (no software)
d-{dep_id}-M0-0          Upstream MITRE pyhuman (read-only control)
d-{dep_id}-M1-0          MCHP baseline (no timing, no LLM)
d-{dep_id}-B0-gemma-0    BrowserUse + gemma4:26b on V100
d-{dep_id}-S0-gemma-0    SmolAgents  + gemma4:26b on V100
d-{dep_id}-B0R-gemma-0   BrowserUse + gemma4:e4b on RTX 2080 Ti (flavor rtx2080ti-A-1gpu.14vcpu.28g)
d-{dep_id}-S0R-gemma-0   SmolAgents  + gemma4:e4b on RTX 2080 Ti (flavor rtx2080ti-A-1gpu.14vcpu.28g)
d-{dep_id}-B0C-gemma-0   BrowserUse + gemma4:e2b on CPU
d-{dep_id}-S0C-gemma-0   SmolAgents  + gemma4:e2b on CPU
```

B0R/S0R baseline the RTX feedback tiers. **R-tier reads its OWN baseline
(2026-06-12): `B2R.gemma → B.gemma/B0R.gemma`, `S2R.gemma → S.gemma/S0R.gemma`** —
the `behavior_dir` is `B.gemma`/`S.gemma` (family dir; the regex `[A-Z]*` consumes
the R) but the `baseline_config` keeps its R. The old R-strip (B2R→B0) was removed
in both `_derive_behavior_paths` (spinup.py) and distribute-behavior-configs.yaml
because PHASE now emits **distinct** B0R/S0R configs (differ from B0/S0 by
seed/pools/behavior-modifiers/`persistent_sessions` on-off — 59-141 leaf keys); the
strip shipped wrong-tier V100 behavior to the RTX SUPs. The VMs are still correctly
on RTX hardware + gemma4:e4b; only the behavior.json content was wrong pre-fix.
Placed on the **rtx-a** pool (`rtx2080ti-A-1gpu`)
2026-05-26 because the non-A `rtx` pool was full with sum25+vt1g feedback —
the axyear rtx-a feedback deploy was dropped to make room (net-zero on rtx-a).

Feedback template (5 VMs per `./deploy --decoy --feedback`). Shape varies
by `--gpu` tier:

**V100 tier** (default, `--gpu v100`):
```
d-{dep_id}-M2-0          MCHP + PHASE timing
d-{dep_id}-B2-gemma-0    BrowserUse + gemma4:26b on V100  (flavor v100-1gpu.14vcpu.28g)
d-{dep_id}-S2-gemma-0    SmolAgents  + gemma4:26b on V100  (flavor v100-1gpu.14vcpu.28g)
d-{dep_id}-B2C-gemma-0   BrowserUse + gemma4:e2b on CPU
d-{dep_id}-S2C-gemma-0   SmolAgents  + gemma4:e2b on CPU
```

**RTX tier** (`--gpu rtx`, dep_name suffix `-rtx`):
```
d-{dep_id}-M2-0           MCHP + PHASE timing
d-{dep_id}-B2R.gemma-0    BrowserUse + gemma4:e4b on RTX 2080 Ti  (flavor rtx2080ti-1gpu.14vcpu.28g, PCI alias rtx2080ti:1)
d-{dep_id}-S2R.gemma-0    SmolAgents  + gemma4:e4b on RTX 2080 Ti  (flavor rtx2080ti-1gpu.14vcpu.28g, PCI alias rtx2080ti:1)
d-{dep_id}-B2C-gemma-0    BrowserUse + gemma4:e2b on CPU
d-{dep_id}-S2C-gemma-0    SmolAgents  + gemma4:e2b on CPU
```

**RTX A-pool tier** (`--gpu rtx-a`, dep_name suffix `-rtx-a`):
```
d-{dep_id}-M2-0           MCHP + PHASE timing
d-{dep_id}-B2R.gemma-0    BrowserUse + gemma4:e4b on RTX 2080 Ti  (flavor rtx2080ti-A-1gpu.14vcpu.28g, PCI alias 2080ti-rtx-a:1)
d-{dep_id}-S2R.gemma-0    SmolAgents  + gemma4:e4b on RTX 2080 Ti  (flavor rtx2080ti-A-1gpu.14vcpu.28g, PCI alias 2080ti-rtx-a:1)
d-{dep_id}-B2C-gemma-0    BrowserUse + gemma4:e2b on CPU
d-{dep_id}-S2C-gemma-0    SmolAgents  + gemma4:e2b on CPU
```

RTX and RTX-A use identical B2R.gemma / S2R.gemma behavior keys and the
same gemma4:e4b runtime model. Only the OpenStack flavor differs — they
map to two distinct physical card pools (separate PCI aliases). The
`-rtx` vs `-rtx-a` deployment-name suffix lets the OpenStack provision
calls land on either pool without VM-name collision, so you can fan
across pools when one is exhausted. Each tier deploy is its own
independent experiments.json entry — no automatic linkage. If you want
to swap sum25 from V100 to RTX-A, run `./teardown` on the V100
deployment first, then deploy the RTX-A one; both stay registered
independently for as long as their VMs exist.

Plus `d-{dep_id}-neighborhood-0` sidecar (feedback only, when any
`topology_mimicry` rate is non-zero).

`dep_id` = `{name_no_hyphens}{run_id}` where `run_id` is `MMDDYYHHmmss`.

## CLI scope flags

```bash
# --preset {preset}_v{version} REQUIRED whenever feedback is in scope (2026-06).
./deploy --decoy --preset std-ctrls_v7.1.2                          # controls + ALL feedback in that ns
./deploy --decoy --controls                                        # controls only (no --preset needed)
./deploy --decoy --feedback --preset std-ctrls_v7.1.2              # all feedback in ns (no controls)
./deploy --decoy --feedback --preset std-ctrls_v7.1.2 --target sum24   # single dataset
./deploy --decoy --feedback --preset std-ctrls_v7.1.2 --target sum24,axyear,vt50g  # batch on one tier
./deploy --decoy --feedback --source /path                        # explicit PHASE source (path encodes ns; no --preset)
./deploy --decoy --controls --preset std-ctrls_v7.1.2 --target sum24    # controls + single feedback
./deploy --decoy --feedback --preset std-ctrls_v7.1.2 --gpu rtx --target sum24   # RTX (PCI alias rtx2080ti:1)
./deploy --decoy --feedback --preset exp-ctrls_v7.1.6 --gpu rtx-a --target axall # other lineage + A-pool
./deploy --decoy --exp1 --preset exp-ctrls_v7.1.6                 # static tier plan (see below)
```

## Cluster capacity (2026-08-12 shrink — CURRENT operator knowledge)

The cluster shrank in Aug 2026. Verified totals (from the 2026-08-12
deploy round — nova `No valid host` probes, NOT queryable from OpenStack):

| Pool | Total cards | Holders (2026-08-12) | Free |
|---|---|---|---|
| v100 | **7** | controls B0/S0 (2) + fall24 (2) + sum24 (2) | 1 |
| RTX (both aliases) | **12** (3 machines × 4) | controls B0R/S0R on rtx-a (2) + spr25 (2) + cptc11 (2) on rtx | 6 |

Each feedback deploy = 2 GPU cards. A no-tier 4-dataset v100 batch needs
8 cards → 3 of 4 fail S2 provisioning (observed 2026-08-12: B2s grab
cards first, S2s ERROR). Both RTX PCI aliases exist on the new machines
(non-A `rtx2080ti:1` ≥ 4 cards, A `2080ti-rtx-a:1` ≥ 2); exact per-alias
split unconfirmed — fan across `--gpu rtx`/`rtx-a` on `No valid host`.
**The `exp1` TIER_PLANS is sized for the OLD 19-v100 cluster — do not
run `--exp1` as-is on this cluster.**

## Static tier plans (`--exp1`, 2026-06-10 — STALE for post-2026-08 cluster, see above)

Named operator-curated dataset→tier assignments in
`core/feedback.py::TIER_PLANS`, sized to the physical GPU pools (totals
are NOT queryable from OpenStack — operator knowledge baked in). `exp1`:

| Tier | Datasets | Cards |
|------|----------|-------|
| v100 | 2025, axall, axyear, fall24, fall25, spr25, sum24, sum25 | 16 of 19 (controls hold 2, 1 spare) |
| rtx | cptc8, cptc9 | 4 of 4 |
| rtx-a | vt50g | 2 of 4 (controls B0R/S0R hold 2) |
| cpu | vt1g, vt10g | 0 (card-free) |

**Rev 2026-06-16:** the two non-A `rtx` slots were swapped from `vt1g`/`vt10g`
to `cptc8`/`cptc9` (operator decision) — same 4-card footprint. cptc now ships
physical `connection_shape` byte/duration targets (cptc9 also `conn_state_mix`),
so it's a real Phase-1 shape target — this supersedes the old "structurally
unreachable" framing (`project_service_mix_targets`, service-mix era). Its high
`target_conn_per_minute` (185–208) WILL show a red BG/volume audit column — that's
a D4-floor measurement artifact (RUSE-fact), see `/decoy-audit`. **Open question
(2026-06-19):** whether VOLUME matters to cptc's exp-model score is unresolved
and PHASE-side — cptc9 is flagged coverage-limited, so do NOT assume "red BG =
fine for the score." Resolves on PHASE's re-infer. See `/feedback-investigation`.
**Rev 2026-06-23:** `vt1g`/`vt10g` rejoin `exp1` on a new **card-free `cpu`
tier** (`FEEDBACK_TEMPLATE_CPU`: M2 + B2C.gemma + S2C.gemma, all
`v1.14vcpu.28g`, gemma4:e2b, NO GPU brains) so they at least emit traffic
without holding a card. `--gpu cpu` also works for ad-hoc single-dataset
deploys. Name suffix `-cpu` (registered in `_GPU_TIER_SUFFIXES`); the cpu
tier's CPU brains read the same `B.gemma/B0C.gemma` + `S.gemma/S0C.gemma` +
`M1` baseline PHASE already ships, so no new feedback content is needed.
`--exp1` implies `--feedback`, requires
`--preset`, and conflicts with `--target`/`--source`/`--gpu` (each task
carries its own `gpu_tier`, shown as `[tier]` in the task label and
`tier=` in the plan confirm). Resolution is fail-loud per dataset; the
whole plan aborts if any target is missing from the namespace. Tasks run
4-wide parallel like any batch (2026-07-21; `--parallel 1` for the old
sequential behavior). To change the split, edit `TIER_PLANS`
(one dict) — new plans get their own flag wired in `__main__.py`.

## Feedback namespace `{preset}_v{version}` (`--preset`, 2026-06)

Canonical reference (rampart/ghosts skills point here). PHASE feedback lives one
level deeper, under a lineage namespace inserted between `{type}-controls` and
`{dataset}`:

```
OLD:  /data/axes-mirror/feedback/{type}-controls/{dataset}/...
NEW:  /data/axes-mirror/feedback/{type}-controls/{preset}_v{version}/{dataset}/...
```

- **`--preset NS` REQUIRED** for any feedback deploy (`--feedback`, `--target`, or
  the default controls+all-feedback). Missing/not-found → fail-loud, lists
  available namespaces. NOT needed for `--controls`-only or `--source /full/path`
  (the explicit path already encodes the ns). `controls/` stays **un-namespaced**
  (`{type}-controls/controls/`, reached via config.yaml `behavior_source`).
- **Mechanism:** `core/feedback.py::_type_root(deploy_type, preset)` folds the
  preset into root resolution once → `{type}-controls/{preset}`. The discovery
  functions, the resolved source Path, and every downstream spinup/distribute glob
  inherit the namespace transparently — **no per-site path edits**. Config JSON
  schemas (behavior/user-roles/timeline) are UNCHANGED.
- **Lineage-assert:** spinup compares each config's stamped
  `_metadata.model_preset`/`model_version` (DECOY) / `_phase_metadata.*`
  (RAMPART/GHOSTS) to the deployed ns and aborts on mismatch (absent stamp defers,
  per the manifest-optional contract).
- **Collision-safety:** the deploy NAME stamps the FULL ns incl. version (sanitized
  `[a-z0-9]` via `_ns_preset_token` from `source_dir.parent.name`) —
  `std-ctrls_v7.1.2` → `decoy-feedback-stdctrlsv712-{ds}-{scope}` vs `_v7.1.5` →
  `…stdctrlsv715…`. So two lineages OR two versions of one dataset get distinct
  `deployment_name → run_dir → VM prefix (dep_id) → experiments.json key` and
  coexist (no idempotent-refresh teardown clash). `experiments.json` carries a
  `preset` attr (sanitized token) per entry via `deploy_metadata.derive_metadata`.
- **Hard cutover:** RUSE read-side + PHASE write-side migration land together —
  until PHASE creates the `{ns}` dirs on the mount, feedback deploys fail-loud
  (intended). Confirm w/ PHASE: stamp field names (`model_preset`/`model_version`),
  manifest.json placement (RUSE assumes per-dataset `{ns}/{dataset}/manifest.json`),
  and that `ablation/` is a `_metadata` field, not a directory RUSE reads.

GPU tier selection via `--gpu {v100,rtx,rtx-a,cpu}` (default v100). RTX
tiers swap B2.gemma/S2.gemma → B2R.gemma/S2R.gemma and the V100 flavor
→ RTX 2080 Ti flavor; M2 + B2C.gemma + S2C.gemma stay identical across
tiers. The two RTX tiers target distinct physical card pools — when
one pool is exhausted (`No valid host was found` on B2R/S2R provision),
switch to the other. The **`cpu`** tier (2026-06-23) is card-free: it
DROPS the GPU brains entirely, leaving just M2 + B2C.gemma + S2C.gemma
(3 VMs, gemma4:e2b) — for datasets you want "at least running" when no
card is free. PHASE feedback is portable across gemma4 variants so the
same `.gemma/` source ships behavior.json for V100, RTX, RTX-A, and CPU
deploys with no re-roll.

Granular per-config-file flags (`--timing`, `--workflow`, `--modifiers`,
`--sites`, `--prompts`, `--activity`, `--diversity`, `--variance`,
`--all-feedback`) were removed when PHASE consolidated to a single
`behavior.json` per SUP. There's no longer a per-file filter to apply.

Batch is the default when `--feedback` is given without a single-target
selector. CLI scans `/data/axes-mirror/feedback/decoy-controls/`, prompts
confirmation, then **fans multi-task plans out as parallel child
subprocesses, 4 at a time by default** (`--parallel N`; `--parallel 1` =
the old sequential loop; reinstated 2026-07-21 — an earlier in-CLI
parallel flag was removed 2026-05-11). Parallelism is across
deployments ONLY: each child is a full single-task `./deploy` invocation
(feedback re-expressed as `--feedback --source {path}` + per-task
`--gpu`, controls as `--controls`, "y" fed on stdin for its confirm)
that keeps the strict phase order provision → ssh → install stage1 →
reboot → stage2 → distribute → start. The parent terminal is a live
phase board: it tails each child's log and surfaces ONLY phase banners
(`--- X ---` decoy / `[N/M]` rampart+ghosts), WARNING/ERROR lines, and
completions — ansible task chatter and retry spam stay in the child's
`logs/deploy-parallel-*.log`. **Validated in production 2026-07-22**: the
full 13-dataset exp1 fleet deployed 4-wide in one invocation, all VMs
healthy on first audit. Cosmetic quirk: children launched in the same
second share the same `MMDDYYHHMMSS` run_id across DIFFERENT config dirs
(e.g. 2025/axall/axyear/fall24 all `072126144158`) — not a collision,
run state + VM prefixes still key on config_name. The 4-cap
guards the control plane (13+ concurrent children drove nova to 500s on
2026-07-20 — deletes then, but the lesson generalizes). Code:
`core/plan.py::_execute_plan_parallel`. Running SEPARATE `./deploy`
invocations concurrently (e.g. backgrounded) is also fine and expected —
the old "no cross-deploy parallel fan-out" operator preference was
RETRACTED 2026-06-23.

Dataset target aliases (`core/feedback.py::DATASET_TARGETS`): `sum24` →
`summer24`, `spr25` → `spring25`, `vt1g` → `vt-fall22-1gb`, `vt50g` →
`vt-fall22-50gb`, `cptc8` → `cptc8-23`, `axall` → `axes-all`, `2025` →
`axes-2025`. Resolution is substring against
`/data/axes-mirror/feedback/decoy-controls/`.

**Pool-math footgun (2026-07-20):** a no-tier batch (`--feedback` with no
`--gpu`/`--exp1`) plans EVERY dataset on the default v100 tier — 14
datasets × 2 v100 GPU VMs = 28 cards against a 19-card pool. Tasks run
alphabetically, so the 8 axes datasets succeed and every later dataset
fails provisioning with nova `No valid host was found`, leaving partial
cohorts (2 ERROR GPU VMs + ACTIVE CPU VMs, run stamped `failed` →
`./teardown --decoy --failed`). Full-fleet deploys must be tiered
(`--exp1`, or per-tier `--gpu` + `--target` invocations).

**`ragged-ctrls-all_v10.0.5` namespace (2026-07, 14 datasets):** cptc
dirs renamed with a `-zeektx` suffix (`cptc8-23-zeektx`,
`cptc9-24-zeektx`) — existing aliases still resolve via substring — plus
a NEW `cptc11-zeektx` dataset that is NOT in `TIER_PLANS['exp1']`, so
`--exp1` silently skips it (deliberate, operator decision 2026-07-21:
"not running cptc11 for now"; no free GPU cards for it anyway — deploy
ad-hoc via `--gpu cpu --target cptc11` if that changes). This preset
ships much lower cptc `target_conn_per_minute` than the 185-208 of older
lineages (cptc9: 53, verified 2026-07-22 audit) — still above the D4
~16/min floor ceiling, so cptc9's red BG column persists, though cptc8
now mostly clears the 0.3 ratio (4/5 BG OK; see `/decoy-audit`).

## Deploy plan / confirm (`core/plan.py::show_plan_and_confirm`)

Before provisioning, the CLI prints a per-task plan and asks `y/N`. Each
task renders a manifest summary (`target env`, `preset`, source path,
`generated_at_utc` + age) AND a **VMs to provision** table
(Behavior/Brain/Flavor/LLM model):

- **Feedback** tasks: table from the GPU-tier template
  (`FEEDBACK_TEMPLATES_BY_TIER[gpu_tier]`, 5 VMs) — `tier=` shown.
- **Controls** task: table from `decoy-controls/config.yaml`'s `deployments`
  (9 VMs: C0/M0/M1 + V100 B0/S0 + rtx-a B0R/S0R + CPU B0C/S0C) via
  `config_vm_table_lines` (added 2026-06; C0/M0 special-cased as
  `bare ubuntu`/`MITRE pyhuman`, not brain SUPs).

**Quirk:** a plan that is a **single controls-only task** auto-proceeds with
NO `y/N` (`if n==1 and is_controls: return True` — "nothing to confirm"); it
still prints the plan first. To force the gate on controls, bundle it with
feedback (`--controls --feedback …`) so the plan is multi-task. A
manifest.target ≠ deploy-type aborts the whole plan.

## Spinup phases (`decoy/spinup.py`)

0. `_validate_behavior_source` — walk every non-C0/M0 SUP's expected
   `{behavior_dir}/{baseline_config}/behavior.json`, abort with
   missing-path list before any VM work
0.5. `_teardown_matching_prior_runs` — for each `runs/{old_rid}/` whose
   saved `config.yaml` has SAME `gpu_tier` AND SAME `deployments[]` list as
   the new config, openstack-delete its VMs (`wait_until_zero`) and
   `safe_rmtree` the prior run_dir. Makes `./deploy` idempotent against
   the same logical deploy; orphan accumulation across reruns goes
   away. Mismatching prior runs are left intact (operator can ./teardown).
1. Provision VMs (`provision-vms.yaml`) — abort if < 90% reach ACTIVE
2. SSH connectivity test (Python `concurrent.futures`, 20 workers) —
   abort if < 90% reachable
3. Install (`install-sups.yaml`) — stage1 system deps → reboot (exit 100)
   → stage2 brain deps + systemd service. INSTALL_SUP.sh runs with
   `RUSE_NO_SERVICE_START=1` so the service is enabled but NOT started —
   distribute starts it (next phase) once behavior.json is on disk. M0 is
   started here (it skips distribute, expected to crash on Linux). C0 skipped.
4. Distribute behavior configs (`distribute-behavior-configs.yaml`) —
   copy + JSON-validate + on-VM stat assert, then `systemd state=started`,
   poll up to 30s for `state=active` AND `NRestarts ≤ 5`, abort if either
   fails. With this ordering NRestarts stays at 0 on a clean deploy
   (pre-fix it sat at 60-100 from crash-loops in the install→distribute gap).
   C0/M0 skip via `meta: end_host`.
5. Neighborhood sidecar (feedback only, gated on non-zero
   `topology_mimicry`)
6. SSH config install (`install_ssh_config()` writes block to
   `~/.ssh/config`) + PHASE register (return False → abort with
   `return 1`)

Run outcome stamp (2026-06-05): `run_dir/deploy_status.json` is written
`failed` right after `run_dir.mkdir` and flipped to `ok` only on the final
clean return (`install_result.rc == 0`). Any phase abort / exception / kill
leaves it `failed` → `./teardown --decoy --failed` targets it. Runs from
before 2026-06-05 are unstamped (`unknown`) → not matched; use positional
teardown or retro-stamp them. See `core/run_status.py` + the `/teardown` skill.
Any teardown that would touch `decoy-controls` (filter sweep incl. `--failed`,
or positional) requires an extra typed `TEARDOWN CONTROLS` confirmation
(2026-07-02); declining in filter mode drops controls and proceeds — see
`/teardown` "Controls gate". DECOY teardown hardening 2026-07-21: after
`teardown.yaml`, `decoy/teardown.py` sweeps VMs nova stranded in ERROR
(500-on-delete) through a re-request + stall-retrying poll — see
`/teardown` "VM delete robustness".

## Service naming

`{behavior_lowercase}.service` with dots → underscores:

- `M1` → `m1.service`
- `B0.gemma` → `b0_gemma.service`
- `S2C.gemma` → `s2c_gemma.service`
- `B2R.gemma` → `b2r_gemma.service`  (RTX, both pools)
- `S2R.gemma` → `s2r_gemma.service`  (RTX, both pools)

Per-behavior service, NOT generic `mchp` / `bu` / `smol`. Logs redirect
to `/opt/ruse/deployed_sups/{key}/logs/systemd.log` and
`systemd_error.log` — use `tail`, not `journalctl -u`.

MCHP maintenance cron (auto-installed for M-brain VMs to mitigate
Selenium/pyautogui memleak):

- `0 3 * * * systemctl restart {svc}.service` — daily restart at 03:00 UTC
- `0 4 * * 0 /sbin/reboot` — weekly reboot Sunday 04:00 UTC

## SSH access

```bash
ssh d-controls050826193122-M1-0
ssh d-controls050826193122-B0-gemma-0 "systemctl status b0_gemma"

# Brain output (NOT journalctl)
ssh d-controls050826193122-B0-gemma-0 \
  "sudo tail -f /opt/ruse/deployed_sups/B0.gemma/logs/systemd.log"

# Structured agent log
ssh d-controls050826193122-B0-gemma-0 \
  "tail -f /opt/ruse/deployed_sups/B0.gemma/logs/latest.jsonl | jq ."
```

## behavior.json schema (PHASE-emitted)

`BehavioralConfig.load` slices the file into 9 dataclass fields, no key
renaming. See `decoys/common/behavioral_config.py` for the loader; consumers
match the shape PHASE emits verbatim.

```json
{
  "_metadata": {"source", "sup_config", "dataset", "current_score", "target_score",
                "generated_at", "mode", "ablation_gate", "timezone": "UTC",
                "seed": int},  // optional; PHASE-emitted, overrides CLI --seed default
                               // via peek_seed() in sup/__main__.py
  "timing": {
    "active_minute_windows": [[start_min, end_min), ...],   // hard 0/1 schedule
    "target_conn_per_minute_during_active": 7.0,
    "min_window_minutes": 15,
    "hard_fence_seconds": 90,
    "burst_percentiles": {
      "connections_per_burst":  {"5","25","50","75","95","max"},
      "idle_gap_minutes":       {"5","25","50","75","95"},
      "burst_duration_minutes": {"5","25","50","75","95"}
    },
    // hourly_distribution / activity_probability_per_hour / long_idle_*
    // were the pre-window soft schedule. Window-mode (2026-05-08)
    // replaced them with active_minute_windows + per-minute target rate.
    // PHASE no longer emits them. RUSE defaults hourly_fractions to
    // uniform [1/24]*24 if absent — windows gate the real schedule.
    "variance": {
      "cluster_size_sigma": 0.5, "idle_gap_sigma": 0.5,
      "hourly_std_targets": {
        "volume":   {"hourly_std_target": [24 floats]},
        "duration": {"hourly_std_target": [24 floats]}
      }
    }
  },
  "content": {
    "workflow_weights": {"BrowseWeb": 0.3, "GoogleSearch": 0.22, ...},
    "site_categories":  {"lightweight": 0.55, "medium": 0.3, "heavy": 0.15},
    "download_url_pool": ["https://...", ...],
    "whois_domain_pool": ["wikipedia.org", ...]
  },
  "behavior": {
    "page_dwell": {"min_seconds": 2, "max_seconds": 43},
    "navigation_clicks": {"min": 10, "max": 30},
    "keep_alive_probability": 0.8,
    "max_steps": 10,
    "enable_whois": true,
    "enable_download": true
  },
  "diversity": {
    "background_services": {
      "dns_per_hour": [24 ints], "http_head_per_hour": [24 ints],
      "ntp_checks_per_day": 4
    },
    "workflow_rotation": {"max_consecutive_same": 2, "min_distinct_per_cluster": 3},
    "topology_mimicry": {"inbound_smb_per_hour": ..., ...},
    "persistent_sessions": {                          // PersistentSession daemon (2026-06-11)
      "enabled": true,
      "session_opens_per_hour": [24 ints, UTC],       // new ssl session opens/hr; non-zero hours = active-hours envelope (day/night gate)
      "keepalive_interval_seconds": 45,               // PHASE upper bound; RUSE clamps actual send to <=10s
      "session_duration_seconds": 120,                // single target (median); RUSE owns lognormal spread, floors at 2s
      "orig_bytes_per_session": 2000,                 // FALLBACK scalar (2026-06-16) — connection_shape.orig_bytes preferred when present
      "endpoint_pool": ["https://...", ...]           // ~8 live external https (TCP-TLS) sites
    },
    "connection_shape": {                             // NEW Phase 1 (2026-06-16); absent → OFF, scalar fallback
      "enabled": true,
      "orig_bytes": {"p25":.., "p50":.., "p75":.., "p90":.., "max":..},  // ACTUATED (persistent-session per-conn sampling)
      "duration":   {"p25":.., "p50":.., "p75":.., "p90":.., "max":..},  // ACTUATED (per-conn lifetime)
      "orig_pkts":  {"p25":.., "p50":.., "p75":.., "p90":.., "max":..},  // parsed, NOT actuated → packetization build #3
      "resp_bytes": {"p25":.., "p50":.., "p75":.., "p90":.., "max":..},  // parsed, NOT actuated → response-endpoint build #2
      "resp_pkts":  {"p25":.., "p50":.., "p75":.., "p90":.., "max":..}   // parsed, NOT actuated → build #2
    },
    "conn_state_mix": {"SF":.., "failed_conn":.., "OTH":.., "RSTR":..}   // NEW Phase 1; FOLD: REJ+S0→failed_conn. ACTUATED: failed_conn rate via scripted_services. SF = uncontrolled baseline (reference only)
  },
  "prompt_content": "... optional free-form prompt guidance ..."
}
```

`_metadata.mode` ∈ `{baseline, dumb_baseline, None}`. Baseline mode emits
a degenerate timing schema; `emulation_loop._reload_behavioral_config`
detects via `fc.mode in {"baseline", "dumb_baseline"}` OR by schema sniff
(`burst_percentiles.burst_duration_minutes is not a dict`) and skips
CalibratedTiming/variance/activity setup. Workflow gating + content pools
still honored.

## Per-flag workflow gating

`behavior.behavior.{enable_whois, enable_download}` controls workflow
registration. PHASE `feedback_engine.baseline` emits both `false`
(controls = single-workflow degenerate mode); feedback proper emits both
`true` (or omits, defaulting `true`).

| Brain | Both flags False | Both flags True |
|---|---|---|
| Smol | BrowseWeb, WebSearch, BrowseYouTube (3) | + WhoisLookup, DownloadFiles (5) |
| BU | BrowseWeb, WebSearch, BrowseYouTube (3) | + WhoisLookup, DownloadFiles (5) |
| MCHP | 7 baseline (no whois, no download) | + WhoisLookup, DownloadFiles |

Mechanism:

- Smol/BU loaders — `load_workflows(enable_whois=, enable_download=)`
- MCHP — `BEHAVIOR_GATED_WORKFLOWS = {'download_files.py': 'enable_download', 'whois_lookup.py': 'enable_whois'}` map; `_load_workflows` skips files whose flag is False
- All 3 brains read flags via `common.behavioral_config.load_workflow_gates(config_dir)`

WhoisLookup + DownloadFiles bypass the Agent's tool-decision loop:

- Smol — dedicated workflow, ONE `LiteLLMModel` picker → domain/URL from PHASE pool
- BU — dedicated workflow, ONE Ollama HTTP picker (loopback `127.0.0.1:11434`, invisible to Zeek), browser never invoked
- MCHP — `random.choice(pool)` no-LLM picker

Helpers in `decoys/common/network/`: `whois.py`, `downloader.py`,
`probes.py`, `neighborhood_traffic.py`, `youtube.py`. Brain workflow files
import directly — no cross-brain imports.

## BrowseYouTube real streaming (2026-06-04)

All three brains now generate REAL video traffic from `content.youtube_video_pool`,
each in-character (NOT unified onto one engine). Before this, none streamed:

- **MCHP / Firefox**: autoplay was blocked → player never started. Fix:
  `webdriver_helper.py` sets `media.autoplay.default=0` → Firefox streams (state 1,
  currentTime advances). Suggested-video selector fixed: watch-page recommendations
  live at `#secondary a[href*="watch"]` (the old `By.ID 'video-title'` only matched
  the SEARCH page, returned 0 on watch pages).
  - **`suggested_videos empty` warning fix (2026-06-13, M-brain incl controls M1):**
    the warning was a FALSE ALARM — NOT pool rot (warned videos oEmbed-200 alive,
    markup-identical to ones that succeed). Real cause = intermittent
    **geckodriver 0.34/Firefox-151 Marionette instability**. Fixes: (1) pin
    **geckodriver 0.34.0 → 0.37.0** in `INSTALL_SUP.sh` = THE fix (canary repro on
    0.37: warned video renders 40 `#secondary` links at 5s); (2) suggested-sidebar
    wait `5s → 10s` (match search path; defensive — 5s was NOT universally short);
    (3) empty case `[WARNING] → [INFO]` + `logger.info` (was `step_error`, flooded
    the audit). Stale "dead video" comment removed.
- **BU / Chromium**: needed `--autoplay-policy=no-user-gesture-required`. The 4
  duplicate `BrowserSession(args=[...])` lists were dedup'd into
  `brains/browseruse/config.py::CHROMIUM_ARGS` (single source of truth) with the
  autoplay arg added.
- **Smol / yt-dlp**: no browser by design. In pool mode it now STREAMS the real
  media over HTTP via `common/network/youtube.stream_youtube_video` (yt-dlp resolves
  the googlevideo URL; ~30 MB byte-capped fetch) → genuine CDN traffic, in-character
  with its HTTP nature. No-pool falls back to the DuckDuckGo research path. Also
  fixed a name-match bug in `smolagents/loop.py` (`"BrowseYoutube"` → `"BrowseYouTube"`)
  that meant the pool was NEVER wired into Smol. `INSTALL_SUP.sh` adds `yt-dlp` to
  Smol deps.

**Dead-video guard** (`common/network/youtube.py`, all three): the PHASE pool rots
(~30% deleted/private). `pick_available_video` oEmbed-checks each pick (HTTP 401/404
→ skip) before navigating/streaming. Root fix is PHASE-side (re-validate the pool at
emit time); this is RUSE-side defense-in-depth.

## Distribute flow (`distribute-behavior-configs.yaml`)

1. Derive baseline config key from versioned key: `B2C.gemma → B0C.gemma`,
   `B2R.gemma → B0R.gemma`, `S2R.gemma → S0R.gemma`, `M2 → M1` (the R-strip was
   removed 2026-06-12 — R-tier reads its own B0R/S0R, see Topology note)
2. Resolve `{feedback_source}/{behavior_dir}/{baseline_config}/behavior.json`
3. `python3 -m json.tool` validate on localhost — corrupt aborts before
   shipping
4. Copy to `/opt/ruse/deployed_sups/{key}/behavioral_configurations/behavior.json`
5. Assert file on disk after copy

Runs for ALL non-C0/M0 SUPs — controls' `decoy-controls/config.yaml`
points `behavior_source` at `/data/axes-mirror/feedback/decoy-controls/controls`
so baselines flow through the same path as feedback.

The `controls/` slot is excluded from feedback dataset auto-discovery via
`core/feedback.py::BASELINE_DATASET_SLOTS = {"controls"}` in three call sites:
`find_all_feedback_sources`, `auto_detect_feedback_source`,
`find_feedback_by_target`. To force PHASE re-roll the baseline:
`rm -rf /data/axes-mirror/feedback/decoy-controls/controls/`.

## LLM models

| Alias | Ollama tag | Tier | Notes |
|---|---|---|---|
| `gemma` | `gemma4:26b` | V100 32GB | MoE 25.2B/3.8B active, fits 89% VRAM, ~10 tok/s. Used by B2.gemma / S2.gemma (V100 feedback) and B0.gemma / S0.gemma (V100 controls). |
| `gemmar` | `gemma4:e4b` | RTX 2080 Ti 11GB | Edge 4B variant (~3 GB int4 weights, ~10 GB loaded with KV cache). Used by B2R.gemma / S2R.gemma on both `--gpu rtx` and `--gpu rtx-a` deploys. Same model across both pools — only the underlying flavor / PCI alias differs. |
| `gemmac` | `gemma4:e2b` | CPU only | Edge-optimized 2.3B, ~7 tok/s on Smol; BU on CPU times out on big prompts. Used by B2C.gemma / S2C.gemma + B0C.gemma / S0C.gemma. |
| `llama` | `llama3.1:8b` | (legacy) | Kept for back-compat, not in any deploy template |

Three gemma4 tiers (V100 / RTX / CPU) keep results structurally
comparable — same family, different VRAM-fit variants. PHASE-shipped
`.gemma/` feedback is portable across all three.

**Brain framework versions are PINNED** (`INSTALL_SUP.sh`):
`browser-use==0.12.7`, `smolagents==1.25.0`. The step-action log parser is
keyed to each version's action/tool vocabulary —
`_BU_ACTION_MAP` (`brains/browseruse/agent.py`) and `_SMOL_ACTION_PATTERNS`
(`common/logging/llm_callbacks.py`). These libs rename actions between
versions (browser-use's pre-0.12 `go_to_url`/`click_element` → 0.12
`navigate`/`click`), so an unpinned bump silently zeroes out per-step
logging (confirmed 2026-05-25: ~99% of BU steps dropped). When bumping a
pin, re-derive the maps from a live VM's emitted action names and update
both in lockstep. A `[parser-drift]` `[WARNING]` (caught by `./audit`'s
Warn column) fires if N consecutive responses parse but map to nothing.

Aliases must agree across **four** call sites:
`INSTALL_SUP.sh::MODEL_NAMES`, `decoys/common/config/model_config.py::MODELS`,
runner argparse `choices=[...]` in **all three** of `run_browseruse.py`,
`run_smolagents.py`, `run_mchp.py`. Adding a new alias and missing any
runner argparse silently crashes the SUP at startup — INSTALL_SUP.sh
generates `run_agent.sh` with `--model={alias}`, the runner rejects it
with `argument --model: invalid choice`, the service crash-loops, and
NRestarts blows past the install-time 30s grace before
distribute-behavior-configs.yaml's service-active assertion catches
it (observed when `gemmar` was added to MODEL_NAMES + model_config.py
but missed in the runners — commit `f2ad12a` is the fix; original miss
was in `755fc0c`).

`get_num_ctx()` in `model_config.py` detects nvidia-smi at runtime: GPU
→ `num_ctx=32768`, CPU → `num_ctx=16384`. Override via `SUP_NUM_CTX`.
Ollama default is 4096 on CPU which silently truncates DOM/tool-use
prompts.

Wired in:
- BrowserUse (`brains/browseruse/agent.py`) — injected into Ollama client
  `chat()` options dict via `create_logged_chat_ollama` wrapper. Uses
  `kwargs.get('options') or {}` (browser_use sometimes passes
  `options=None`)
- SmolAgents (`brains/smolagents/agent.py` + 3 workflow files) — passed
  as `num_ctx` in `LiteLLMModel` constructor

## BrowserUse Agent tuning (`brains/browseruse/agent.py`)

Non-default settings cap token usage to keep CPU BU forward-progressing:

```python
Agent(
    task=full_prompt, llm=self._get_llm(), browser_session=...,
    use_vision=False,                   # gemma is text-only
    use_judge=False,                    # skip extra LLM eval per step
    max_clickable_elements_length=8000, # ~2K tokens vs 40K default
    max_history_items=5,
    include_attributes=["id", "class", "name", "type", "value",
        "placeholder", "aria-label", "role", "href", "title", "alt"],
    llm_timeout=300,                    # CPU LLM calls can take 2-3 min
)
```

Per-step uniform delay from `behavior.behavior.page_dwell` is wired via
`Agent(register_new_step_callback=...)`.

## PHASE feedback runtime consumption

Loader (`load_behavioral_config`) → consumers:

| behavior.json path | BehavioralConfig field | Consumer |
|---|---|---|
| `timing.active_minute_windows` + `target_conn_per_minute_during_active` + `min_window_minutes` + `hard_fence_seconds` | `timing_profile` | `phase_timing.update_window_contract` → window gate in `emulation_loop` + D4 deficit-burst in `background_services` |
| `timing.burst_percentiles.*` | `timing_profile` | `CalibratedTimingConfig.{burst_duration,idle_gap,connections_per_burst}` |
| `timing.hourly_distribution` (legacy, vestigial) | `timing_profile` | `CalibratedTimingConfig.hourly_fractions` — defaults uniform when absent; windows gate the real schedule |
| `timing.variance.cluster_size_sigma` | `variance_injection` | `get_cluster_size()` lognormal noise |
| `timing.variance.idle_gap_sigma` | `variance_injection` | `get_cluster_delay()` lognormal noise |
| `timing.variance.hourly_std_targets.{volume,duration}.hourly_std_target` | `variance_injection` | D1 per-hour sigma in `_init_variance_targets` |
| `timing.activity_probability_per_hour` | `activity_pattern` | `should_skip_hour()` |
| `timing.long_idle_probability` + `long_idle_duration_minutes` | `activity_pattern` | `should_take_long_idle()` |
| `content.workflow_weights` | `workflow_weights` | `build_workflow_weights()` for `random.choices()` |
| `content.schedule[*].workflow_budget` | (raw, rides `schedule`) | **ExecGovernor rate pacing (PHASE task-value engine, 2026-08-14, `common/exec_governor.py`).** Weights say the MIX; `target_execs_per_hour` says HOW OFTEN. PHASE solves per-workflow exec rates from measured per-execution connection cost so total conn spend lands on `target_conns_per_hour` — pacing on weights alone is what produced the measured axes overshoot (6.3 conn/min target vs 53 achieved). `emulation_loop._build_budget_by_hour` parses it into a 24-elem per-hour list (**non-fatal / additive** — unlike `_build_schedule_by_hour`, an absent budget or uncovered hour just leaves that hour ungoverned); `ExecGovernor` runs one wall-clock token bucket per workflow + a global `cadence_cap_per_hour` bucket. Ineligible workflows are masked to weight 0 in `_current_workflow_weights` (the single choke point both `_select_workflow` and `_select_workflow_with_rotation` read, so rotation penalties still apply to the remainder); `_exec_budget_blocked()` skips the tick when nothing holds budget — background channels still fire. Credits accrue on WALL TIME not loop ticks, so slow BU and fast MCHP converge to the same execs/hour; banking capped at `_BURST_HOURS=0.25`. Entering a new block seeds 1 credit per non-zero-rate workflow. `target_conns_per_hour` also feeds the idle-floor cap (below). Logs `[exec-budget] governor ON …` + per-skip `[exec-budget] all workflows over their target_execs_per_hour`. **Additive: absent block → governor inactive → selection byte-identical to pre-2026-08-14.** |
| `content.schedule[*].workflow_budget.{target_conns_per_hour,_idle_overrun,idle_floor_conns_per_hour}` | (raw) | **Idle-floor budget cap (2026-08-14).** `ShapeController.set_conn_budget_per_min()` (fed from `target_conns_per_hour/60`, falling back to `timing.target_conn_per_minute_during_active`) clamps `_floor_target` to `max(0, budget − active_opens)`. The shape-floor was the ONLY always-on channel with neither a rate ceiling nor a window gate, and it self-inflates (its `unshaped` term derives from the global ActiveOpens delta, which counts the floor's own opens — at idle there's no workflow traffic to offset it). PHASE measured the behavior idle floor at ~370 conn/hr against a 57 conn/hr budget with `_idle_overrun: true`; scaling `session_opens_per_hour` alone cannot close that. `[shape]` log gained `floor_cap=` + `budget=`. None/absent → uncapped (prior behavior). `idle_floor_conns_per_hour.system` (~4.6/hr) is OS noise, NOT RUSE's to cut. |
| `content.site_categories` | `site_config` | SmolAgents `BrowseWebWorkflow` task pool filter |
| `content.download_url_pool` | `download_url_pool` | Smol/BU `DownloadFiles` LLM picker (falls back to `FALLBACK_URLS`) |
| `content.whois_domain_pool` | `whois_domain_pool` | Smol/BU/MCHP `WhoisLookup` (falls back to `FALLBACK_DOMAINS`) |
| `behavior.page_dwell` / `navigation_clicks` | `behavior_modifiers` | MCHP `BrowseWeb.{min,max}_sleep_time`; BU per-step delay |
| `behavior.enable_whois` / `enable_download` | (read via `load_workflow_gates`) | Workflow registration |
| `behavior.keep_alive_probability` | `behavior_modifiers` | MCHP `BrowseWeb.keep_alive_probability` |
| `behavior.max_steps` | `behavior_modifiers` | BU/Smol per-workflow max_steps |
| `diversity.background_services.*` | `diversity_injection` | `BackgroundServiceGenerator` (D4) — `maybe_generate` |
| `diversity.background_services.{name}_enabled` | (read by `ScriptedServiceScheduler`) | Phase-3 scripted protocol probes (`scripted_services.py`: smb/ldap/imap/doh/mdns/websocket/failed_conn). `maybe_run` fires from the in-window cluster loop (`emulation_loop.py:583`, same gating as D4) — **never outside an active window**. Per-probe cron slots (e.g. `failed_conn` :17/:47). **Catch-up scheduling (2026-06-05, commit `26c2489`):** fires the latest slot at/before the current minute not yet fired this hour, so a sleepy loop that misses the exact minute still fires; the prior exact-minute match fired 0× over 8.5h. `[scripted-svc] {name} ok= state= latency_ms=` → stdout/systemd.log (+ jsonl `info`); `[scripted-svc] {name}=enabled` config marker is jsonl-only. |
| `diversity.background_services.service_mix_targets` | **NOT CONSUMED — no RUSE reader (reverted). PHASE still emits it on cptc; RUSE silently ignores it.** | **service_mix_targets v1 — ABANDONED / DEAD-END (2026-06-09). DO NOT re-chase; REVERTED out of RUSE same day (revert `bed8350` of commits `6ec6b8d`+`f53f79d`).** Note: with the service-mix precedence gone, cptc behavior.json's `smb_enabled`/`failed_conn_enabled` are honored normally again by `ScriptedServiceScheduler` (the old `covered_services()` force-disable is removed) — the scripted `smb` probe fires in-window as a plain fire-and-observe SYN (`[scripted-svc] smb ok=False state=S0` is correct; no responder exists anymore). The idea: own-thread generators (`common/network/service_mix.py`) + a sidecar responder (`common/network/service_responder.py`: TCP 445 SMB, TCP 9997 splunk, UDP echo) to emit Zeek service types `smb`/`splunk`/`udp` that no workflow produces, to close the cptc service-mix gap. **Built, deployed, validated on the live cptc9 dredge — and it CANNOT work.** Conclusive reasons: **(1) Vocabulary skew** — targets are computed from the *CPTC9-competition* Zeek (`TRAINING_DATA/CPTC9_24.parquet`: `smb`/`splunk`/`udp`/`dcerpc`), but the *AXES tap* Zeek observing our SUPs **never emits `splunk` (0/65 deploys) or bare `udp`** (it labels discovery 137/5355/5353→`dns`, 138/1900→`None`, 123→`ntp`) and labels SMB only as **`gssapi,smb`** (auth'd SMB2), not bare `smb`. Responder mechanics worked (flows completed: 445 RSTR, 9997 RSTO, udp SF, data both ways) but **all landed `service=None`** — minimal SMB1 negotiate isn't what Zeek confirms; splunk has no analyzer on this sensor. **(2) Exact-string target match** in `decoy_generator.py` (PHASE, can't edit) — `gssapi,smb`≠`smb`, and critically `quic,ssl`≠`ssl` + `http,websocket`≠`http`: a REAL bug that under-credits the *good* axes targets. **Carry-forward win → ask PHASE to normalize comma-joined Zeek labels component-wise at the target matcher** (NOT the frozen LabelEncoder). **(3) cptc structurally unreachable** — wrong network + wrong sensor. std cptc9 model (MinMax, continuous-blind, `log_transform_bytes=False`) scored ~0.50 = a *false positive* foolable via shared categorical vocab (dns/http/ssl); exp cptc9 model (quantile, 20 feat, continuous un-blinded) correctly rejects AXES browsing SUPs at ~0 because browsing-scale bytes/timing ≠ cptc9 hostile-competition scale (~185 conn/min, 3.2 MB/conn splunk). No categorical responder or PHASE knob closes "wrong activity on wrong network." **`service_mix_targets` is emitted ONLY on cptc** (axes/vt are ≤1%-skew, workflow-reachable → field omitted) → it only applies where it can't work. Full write-up: memory `project_service_mix_targets.md`. |
| `diversity.workflow_rotation.*` | `diversity_injection` | D2 rotation in `emulation_loop` |
| `diversity.persistent_sessions.*` | `diversity_injection` | **PersistentSession daemon (2026-06-11, `common/network/persistent_session.py`).** Brain-agnostic background **thread** (NOT a workflow — never occupies the sequential slot), unlike D4/scripted-svc which are inline. Holds long-lived TCP-TLS sessions to `endpoint_pool` during the active-hours envelope (non-zero `session_opens_per_hour` hours, read **circularly** so student bands wrap midnight), opening new sessions spread across active minutes to close PHASE's ssl-dominant-minute / duration / orig_bytes gap. **Start-minute binning** → opens not concurrency; **resolve-once + connect-by-IP** → zero steady-state dns (so ssl starts win the per-minute MODE tie vs dns); lifetime = `min(sampled_duration, time-to-block-end)` → graceful FIN/SF at workday boundary; orig_bytes front-loaded into the first request. D4 **net-out**: daemon opens are subtracted from D4's deficit-burst via `set_window_state(external_conns=)` so total volume stays at target + mix shifts to ssl. Absent block or `enabled:false` → daemon off (no loader change — rides `diversity_injection` verbatim like `topology_mimicry`). Logs `[psess] open/close/daemon started`. **Endpoint caveat:** keep-alive longevity is endpoint-dependent (Fastly-fronted hosts cnn/stackoverflow/bloomberg/reddit/cisco server-close ~2 req; github/docs.python.org/azure/wikipedia sustain) — quick-closers still give the ssl-minute + orig_bytes win, only DURATION needs sustainers; RUSE clamps send to ≤10s, PHASE curates the pool. |
| `diversity.connection_shape.{orig_bytes,duration}` | `connection_shape` | **Closed-loop ShapeController (Phase 1, 2026-06-16, `common/network/shape_controller.py`).** Per-connection target percentile distributions `{p25,p50,p75,p90,max}`. The controller draws a per-conn target (NOT the p50 scalar) for the **persistent-session channel** and applies a bounded, damped multiplicative **bias** corrected each minute from an **emit-side ledger** (each closed session reports `bytes_cum`/wall-duration/`SF` — `/proc` can't see per-conn shape, RUSE spec §B.1) toward target p50. `max` is a HARD post-bias ceiling. When active it owns sampling and supersedes the persistent-session lognormal; absent/`enabled:false`/malformed-dist → warn-loud + fall back to scalar `orig_bytes_per_session`/`session_duration_seconds` (never crashes — additive). `orig_pkts`/`resp_bytes`/`resp_pkts` are PARSED but NOT actuated yet (packetization build #3 / response-endpoint build #2). **Build #5 (2026-06-25): the persistent-session channel is no longer the ONLY shaped channel** — `common/network/shape_floor.py::ShapeFloorDaemon` (own-thread twin, reuses the `persistent_sessions` endpoint_pool) opens coverage-driven synthetic shaped fillers (count from `controller.floor_opens_target_per_min()`, `T=0.55` shaped-share — recalibrated from 0.82 on 2026-06-29 after the live deploy overshot byte/packet shape; T-sweep put 0.55 on-target) sampling the FULL `connection_shape` dist, reports to the same emit-side ledger as channel `"floor"` — closing the ~30%→majority coverage gap so the AGGREGATE per-conn median (not just the shaped tail) reaches the human target. Built when `connection_shape.enabled`; opens net out of D4. Logs `[shape] bytes_med=.../... agg_bytes_p50=.../tgt agg_dur_p50=.../tgt shaped_share=N% floor_target=K wf_complete=c/s failed_conn_rate=...` each minute (`agg_*_p50` = estimated aggregate, acceptance ≥ ~0.6× target p50; `wf_complete` drop = floor too aggressive, lower T). Ships dormant until PHASE emits the block. |
| `diversity.conn_state_mix.failed_conn` | `conn_state_mix` | **ShapeController → scripted_services failed_conn rate (Phase 1).** FOLD (2026-06-16): PHASE collapses REJ+S0 into a single `failed_conn` fraction; SF is the uncontrolled baseline (reference only). Controller computes `failed_conn_rate_per_min = failed_conn_frac × per-minute aggregate active_opens` (own `OutboundConnSampler` — a valid count use of `/proc`); `ScriptedServiceScheduler` fires `probe_failed_conn` (S0/REJ to closed port) toward that rate via a per-minute budget, **bypassing the fixed cron `failed_conn` slot** when a target is present (other cron probes unchanged). Logs `[scripted-svc] failed_conn ... src=rate`. Honored regardless of the `failed_conn_enabled` cron toggle. REJ vs S0 split deferred to the RST/responder build #6. |
| `diversity.topology_mimicry.inbound_*_per_hour` | `diversity_injection` | Neighborhood sidecar daemon |
| `_metadata.mode` | `mode` | Baseline short-circuit in `_reload_behavioral_config` |
| `_metadata.ablation_gate` | `ablation_gate` | **DEAD field (2026-06-12):** PHASE deleted `ablation_gate` in the two-shapes simplification (`8f91240a`, 2026-05-08), so `is_ablation_gated()` is always False. The old `[WARNING]`-vs-`[INFO]` tag logic it drove is removed — section-absent status lines now ALWAYS emit `[INFO]` (optional under two-shapes; commit `bc7aa66`). Loader still slices the field if present (forward-compat), but no live consumer depends on it. |
| `_metadata.seed` | `seed` | `sup/__main__.py` peeks before `random.seed()`; overrides CLI `--seed`. Also propagated into `neighborhood-sups.json` top-level `seed` field for sidecar RNG anchor. `AgentLogger.session_id` derives from this via separate `Random()` instance (no global RNG consumption) |
| `prompt_content` | `prompt_augmentation.prompt_content` | G1: BU + Smol prompt prepend |

## Logging output (jsonl)

Each SUP writes events to
`/opt/ruse/deployed_sups/{key}/logs/session_{YYYY-MM-DD_HH-MM-SS}_{session_id}.jsonl`
(+ a `latest.jsonl` symlink). Envelope on every line: `timestamp` (naive
**local** ISO; runtime hour-gating uses UTC separately — see CLAUDE.md UTC
contract), `session_id` (8 hex, seed-derived → deterministic across replays),
`agent_type` (config key), `event_type`, optional `workflow`, `details`.
None values omitted.

**17 event types**: `session_{start,success,fail,end}`,
`workflow_{start,end}`, `step_{start,success,error}`,
`llm_{request,response,error}`, `decision`, `timing_delay`, `warning`, `info`,
`network_sample`. PHASE-side consumers and the DuckDB collection
(`/data/axes-mirror/SUP_LOGS/sup-logs-<exp>.duckdb`) read these directly.
(A transient 18th type, `background_service`, existed only during the
abandoned service_mix_targets v1 window, 2026-06-09 — reverted same day.)

`network_sample` (2026-06-01) is the **representative traffic signal** — emitted
~per-minute by `background_services.py` via `OutboundConnSampler`
(`common/network/conn_sampler.py`). Workflow/step COUNTS are honest but are NOT a
traffic proxy (a BU `navigate` step = a full page-load with dozens of sub-resource
conns; an MCHP step = one local micro-action — ground-truthed 2026-06-01: on the
wire BU ~18 conn/min ≫ MCHP ~1 ≫ Smol ~0.27, the inverse of the workflow-count
ranking). `details`: `active_opens` (real outbound TCP conns opened in the window,
incl. short-lived; from `/proc/net/snmp` `Tcp:ActiveOpens` delta; minor loopback
noise), `distinct_hosts` (loopback-excluded external peers from `/proc/net/tcp{,6}`),
`d4_synthetic` (legacy D4-only count, = the `[bg-counter]` `conns=` floor), `window_s`.
The `[bg-counter]` systemd.log line gained matching `active_opens=`/`hosts=` fields.
Cadence follows the inter-task `maybe_generate` call, so for slow BU it's per-workflow
(minutes), not strictly per-minute — `window_s` carries the true interval and
`active_opens` is a delta, so volume is still complete.

BU `llm_error` now also fires on `cancelled/timeout` (2026-06-01): CPU-slow LLM
calls were cancelled mid-flight (`CancelledError`, a `BaseException`) and vanished
silently (`llm_request` ≫ `llm_response`, `llm_error=0`). The wrapper now logs them
(`fatal=False`) so the request/response gap is reconcilable.

### Canonical `workflow` field (2026-05-25)

The `workflow` top-level field carries `workflow.name` — the harmonized
cross-brain identifier (`BrowseWeb`, `BrowseYouTube`, `WebSearch`,
`WhoisLookup`, `DownloadFiles`, `DocumentEditor`, `SpreadsheetEditor`,
`ExecuteCommand`, `ListFiles`, `MicrosoftPaint`). These match exactly the
keys `feedback_engine.decoy_generator` emits in `content.workflow_weights`,
so log events join to weights directly. Human task text moved to
`params.description`; `workflow_class` was REMOVED (zero PHASE consumers
used it). Workflow names DIVERGE from Python class names in MCHP
(`google_search.py` class `GoogleSearch` → name `WebSearch`;
`browse_web.py` class `WebBrowse` → name `BrowseWeb`) — the `.name` is the
deliberately harmonized join key; class names stay legacy.

### Real per-step outcomes + durations (2026-05-25)

`step_success`/`step_error` and `duration_ms` reflect actual execution from
authoritative sources per brain:

| Brain | Step source | Timing |
|---|---|---|
| **BrowserUse** | walks `AgentHistoryList` returned by `agent.run()` (`_log_bu_steps` in `brains/browseruse/agent.py`); pairs `model_output.action` with `ActionResult.error` per step | **batched at workflow-end** |
| **SmolAgents** | `CodeAgent(step_callbacks=[make_smol_step_callback(logger)])` over each `ActionStep` (`code_action`/`error`/`timing` in `common/logging/llm_callbacks.py`) | streamed per step |
| **MCHP** | hand-instrumented `logger.step_start/success/error` in each workflow file | streamed |

⚠️ **BU batching caveat for inter-step timing**: BU `step_start` timestamps
cluster at workflow-end (since the history is walked once after
`agent.run()` returns), so they're NOT meaningful for inter-step gap
analysis (`feedback_engine/knob_investigation/inter_step_timing.py`). Use
`llm_request`/`llm_response` timestamps (still streamed via the chat
wrapper) for BU inter-step timing. Smol and MCHP stream normally.

### Action / step vocabulary (version-coupled — see `project_brain_lib_pin_parser_coupling` memory)

- **`_BU_ACTION_MAP`** (`brains/browseruse/agent.py`) maps the **full
  browser-use 0.12.7 `Tools.registry`** (24 actions: navigate, click, input,
  scroll, search, search_page, extract, find_elements, find_text,
  screenshot, evaluate, dropdown_options, select_dropdown, read_file,
  write_file, replace_file, save_as_pdf, upload_file, go_back, switch,
  close, send_keys, wait; `done` intentionally skipped). **Derive from the
  registry** (`python -c "from browser_use.tools.service import Tools;
  print(sorted(Tools().registry.registry.actions))"`), NOT sampled logs —
  sampling missed half on 2026-05-25 (drift guard caught `read_file`).
- **`_SMOL_ACTION_PATTERNS`** (`common/logging/llm_callbacks.py`) is
  bounded by what we register: `web_search`/`duckduckgo`/
  `DuckDuckGoSearchTool` → search, `visit_webpage` → navigate,
  `requests.get`/`urllib`/`fetch` → navigate, `print` → scroll;
  `final_answer` skipped. Complete by construction.
- **MCHP**: step names hardcoded in workflow files (`open_application`,
  `edit_content`, `save_document`, `download_file`, `whois_lookup`, etc.).
  No version-coupled vocabulary.

### Parser-drift guard

Both BU (`_log_bu_steps` → `_bu_note_drift`) and Smol
(`_smol_code_unmatched`) count consecutive unmapped action names / unmatched
code turns. At threshold (BU=10, Smol=25) they print one
`[WARNING] [parser-drift] ...` to stdout → systemd.log → caught by
`./audit`'s Warn column. Validated 2026-05-27: caught `read_file` (an
action the original observed-sample map missed). Pinned versions
(`browser-use==0.12.7`, `smolagents==1.25.0`) are in `INSTALL_SUP.sh` so a
silent bump can't break the maps unnoticed.

### DownloadFiles / WhoisLookup detail fields (2026-05-26 / -27)

The dedicated workflows now carry rich detail in step_success/_error
(previously discarded on success):

- **`download_file`** details: `{url, outcome, host, bytes, content_type,
  elapsed_ms}` + real `duration_ms`. MCHP variant:
  `{source, bytes}` from a `~/Downloads` scandir-delta snapshot
  (no common downloader for MCHP).
- **`whois_lookup`**: `message` = trimmed IANA referral
  (non-`%`-comment lines joined: refer / domain / organisation),
  `details = {domain}`, real `duration_ms` (the TCP/43 call time).

### Schedule-idle ≠ stuck

Outside `behavior.json` `active_minute_windows`, the SUP emits an `info`
event and sleeps without firing a workflow:
- Feedback: `[window] outside windows — sleeping Nmin until next start`
- Controls: `[controls] outside windows — sleeping 5.0min`

A SUP with `workflows=0` AND these info lines AND `svc=active` (recent
file mtime) is correctly idle per schedule — NOT hung. Different datasets
have different windows, so simultaneous on-window/off-window splits across
the fleet are normal (2026-05-27 redeploy audit: 35 on-window logging,
27 off-window idle, all healthy).

### DuckDB collection

Periodic SSH-collection from `/opt/ruse/deployed_sups/.../logs/*.jsonl`
into `/data/axes-mirror/SUP_LOGS/sup-logs-<experiment>.duckdb` `events` table.
First-class extracted columns (queryable without JSON path): `timestamp,
session_id, agent_type, event_type, workflow, duration_ms, success,
error_message, model, action, category, step_name, status,
{input,output,total}_tokens, llm_output`. The newer `details` payload
fields (`bytes`, `content_type`, `outcome`, `host`, `domain`, `description`)
live inside the `details` JSON column → query via JSON path, e.g.
`details->>'bytes'`.

## Topology mimicry (neighborhood sidecar)

Feedback-only. 1 small VM per deploy (`d-{dep_id}-neighborhood-0`,
`v1.small`, `bot-desktop` keypair). Daemon
`common.network.neighborhood_traffic` reads
`/etc/ruse-neighborhood/sups.json` and synthesizes inbound TCP/UDP
probes at each SUP IP.

10 probe types in `decoys/common/network/probes.py`:
`inbound_{smb,ldap,wsus,ntp_receive,printer,ipmi,winrm,mdns,ssdp,scan}_per_hour`.
Produces mixed conn_state (SF / S0 / REJ / RSTO / unidir) on Zeek rows
from the SUP — fights `local_orig=1` / ephemeral-port-only / `conn_state=SF`
sandbox signal.

Deploy flow (`decoy/spinup.py` phase 5, after distribute):

1. `_synthesize_neighborhood_config` walks each SUP's `behavior.json`,
   collects `topology_mimicry` rates, writes
   `neighborhood-sups.json` if any non-zero (else returns None → skip)
2. `_provision_and_install_neighborhood` creates VM, writes
   `neighborhood-inventory.ini`, runs `install-neighborhood.yaml` (asserts
   `ruse-neighborhood` service active + NRestarts ≤ 5)

Audit excludes sidecars from orphan check (live in
`neighborhood-inventory.ini`, not `sup_hosts`). Service-status audit
not yet wired to main `./audit`.

## Hot-patch path

`/opt/ruse/deployed_sups/{key}/decoys/` is a **copy**, not a symlink. Each
install copies `/opt/ruse/decoys/` → that path. `git pull` in `/opt/ruse`
does NOT propagate. Hot-patch:

1. `git push` from mlserv (INSTALL_SUP.sh and `decoys/*` are pulled from
   github at install time — clone URL in
   `deployment_engine/playbooks/decoy/install-sups.yaml::ruse_repo`)
2. SSH the VM, `cp` changed files into per-deploy `decoys/`
3. `systemctl restart {svc}.service`

Or teardown + redeploy.

## Audit (`./audit`)

Per-VM checks across all DECOY VMs. Key columns:

- `Service` — `systemctl is-active` + NRestarts + uptime probe. NRestarts is cumulative and never decays, so a service with high restart count from past crash-loops is still treated as `OK (N restarts, stable Mm)` if it's been continuously active ≥ 600s. Only services active < 600s with NRestarts > 10 are flagged `FAIL (crash-looping)`.
- `M0` — reports `EXPECTED (M0 upstream crashes on Linux)`
- `Fdbk` — checks for exactly 1 `behavior.json` in `/opt/ruse/deployed_sups/*/behavioral_configurations/`
- `Warn` — counts `[WARNING]` vs `[INFO]` separately:
  - Baseline (`bc_has_behavior=0`): `n/a (baseline)` — runtime short-circuits
  - Feedback, 0 warn + N INFO: `OK (N ablation-gated)` — PHASE deliberately omitted sections
  - Feedback, N warn: `FAIL (N unexpected warnings)` — real bug

VM probe greps `/opt/ruse/deployed_sups/{key}/logs/systemd.log` for
`[WARNING]` and `[INFO].*ablation-gated`.

## Observability recipes

```bash
# What aborted the deploy?
grep -E "FAIL|ABORTING|FAILURES" deployments/logs/session-deploy-*.log | tail -30

# What did Ansible actually say per-task?
grep -E "FAILED|fatal|UNREACHABLE" deployments/logs/ansible-*.log | tail -30

# Per-VM behavior.json present?
./audit | grep Fdbk

# All behavior.json files PHASE wrote for a dataset
ls /data/axes-mirror/feedback/decoy-controls/sum24/*/*/behavior.json
```

## Constraints

- C0 no software, M0 read-only, no LLM fallback, MCHP no LLM (see CLAUDE.md)
- Models run locally via Ollama
- Per-deploy `decoys/` is a COPY (see hot-patch path above)
- `INSTALL_SUP.sh` + `decoys/*` pulled from github → push before deploy
- VMs set `America/New_York` for log readability; runtime hour reads use
  `datetime.now(timezone.utc).hour` (UTC contract in CLAUDE.md)
