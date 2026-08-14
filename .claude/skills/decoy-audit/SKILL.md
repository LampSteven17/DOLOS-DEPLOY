---
name: decoy-audit
description: DECOY audit — `./audit --decoy` runs 11 per-VM SSH probes + 5 cross-deployment consistency checks across every active DECOY deployment. Code in `deployment_engine/decoy/audit.py`. Outputs terminal table + markdown report at `deployments/logs/audit_*.md`. All three audits are now implemented (see /ghosts-audit, /rampart-audit). Per-VM behavior.json window-mode states, NRestarts crash-loop detection, Ollama+GPU IDLE-vs-FAIL, feature-warning grep, and orphan-VM/missing-inventory diff vs OpenStack. SSH probe inlined (not via Ansible) for parallel speed + per-VM real-time output.
---

# decoy-audit

`./audit --decoy` (or just `./audit`, default) runs full health audit
across every DECOY deployment with active state. Routes through
`deployment_engine/decoy/audit.py::run_audit(deploy_dir)`.

| | |
|---|---|
| Entry point | `./audit` (default `--decoy`) at RUSE root |
| Code | `deployment_engine/decoy/audit.py` |
| Inputs | `deployments/{decoy-*}/runs/{run_id}/inventory.ini`, OpenStack server list, `/data/axes-mirror/experiments.json`, per-VM SSH probes |
| Outputs | Terminal summary table + markdown report at `deployments/logs/audit_<timestamp>.md` |
| Exit code | 0 on no failures, 1 if any check fails |

## What gets audited

Discovery: walks `deployment_engine.core.config.DeploymentConfig` for
each `deployments/*/config.yaml`. Skips RAMPART (`is_rampart()`) and
GHOSTS (`is_ghosts()`) — DECOY-only at the discovery boundary. For each
DECOY deployment, iterates `runs/*/inventory.ini` and probes every VM
in parallel.

## Per-VM SSH probe (`_ssh_probe`)

Single SSH round trip per VM, executes a bash blob that emits `KEY=value`
lines for each check. Parsed locally into a probe dict. 20-worker
ThreadPoolExecutor; 30s timeout per VM.

**`ssh=FAIL` can be a transient probe timeout, not a dead VM** — the 30s
timeout under the 20-worker fan-out occasionally expires on a healthy VM
(observed 2026-07-22: controls M0-0 flagged `ssh=FAIL`, VM ACTIVE, up 3
days, instant manual SSH). Cross-check with a direct `ssh <vm> uptime`
before diagnosing; only a repeat failure across audits is real.

Probe collects:

| key | meaning |
|---|---|
| `SVC` | `systemctl is-active {svc}` |
| `NRESTARTS` | `systemctl show {svc} -p NRestarts` (cumulative, never decays) |
| `SVC_UPTIME_S` | seconds since `ActiveEnterTimestampMonotonic` — used to ignore stale NRestarts from past crash-loops |
| `PROC_COUNT` | `pgrep -f 'runners.run_'` |
| `OLLAMA_MODEL` | `curl localhost:11434/api/ps` first model name |
| `VRAM_MIB` | `nvidia-smi --query-gpu=memory.used` |
| `GPU_NAME` | nvidia-smi name (V100 / RTX) |
| `LOG_MTIME` | newest jsonl mtime under `/opt/ruse/deployed_sups/*/logs/` |
| `CRON_COUNT` | `sudo crontab -l \| grep -c 'mchp-(daily\|weekly)'` |
| `BC_FILES` / `BC_HAS_BEHAVIOR` | behavior.json presence + total file count |
| `WARN_COUNT` / `INFO_COUNT` / `WARN_LINES` | grep `[WARNING]` and `[INFO].*ablation-gated` from systemd.log |
| `WIN_STATE` / `WIN_N` / `WIN_ON_MIN` / `WIN_TARGET` | window-mode contract from `behavior.json` |
| `WIN_VOL_MEDIAN` | median `[bg-counter]` conns/min during ON-windows over last 60 minutes |
| `PSESS_ENABLED` / `PSESS_HITS` | `diversity.persistent_sessions.enabled` + count of `[psess] open` lines in systemd.log |

## 17 columns in the terminal summary

| col | check | source |
|---|---|---|
| SSH | reachable | probe rc |
| Svc | systemd active AND (uptime ≥ 600s OR NRestarts ≤ 10) | `SVC` + `NRESTARTS` + `SVC_UPTIME_S` |
| Proc | brain process running | `PROC_COUNT >= 1` |
| Model | Ollama loaded matches `expected_model(behavior)` (V100 → gemma4:26b, R-infix → gemma4:e4b, C-infix → gemma4:e2b, M-brain → no check) | `OLLAMA_MODEL` |
| GPU | tier-aware VRAM "loaded" floor — V100 ≥ 5000 MiB (gemma4:26b ~14 GB loaded), R-tier ≥ 2000 MiB (gemma4:e4b ~3-5 GB int4, ~10 GB with KV cache). Tier detected via behavior-key regex `^[BS]\d+R(\..*)?$`. | `VRAM_MIB` |
| Logs | latest jsonl < `LOG_FRESHNESS_SECS` (4h default) | `LOG_MTIME` |
| Cron | M-brains have 2 maintenance entries (daily restart + weekly reboot) | `CRON_COUNT` |
| Fdbk | exactly one `behavior.json` in `behavioral_configurations/` | `BC_FILES + BC_HAS_BEHAVIOR` |
| Warn | 0 `[WARNING]`s. **Section-absent status lines are now `[INFO]`, not `[WARNING]` (2026-06-12, commit `bc7aa66`):** D2 `workflow_rotation`, D4 `background_services`, G1 `prompt_augmentation`, W4 `workflow_weights` omissions are OPTIONAL by design under the two-shapes contract (PHASE deleted `_metadata.ablation_gate` in `8f91240a`, 2026-05-08, so `is_ablation_gated()` was always False and every omission falsely tagged `[WARNING]` — flooded the audit 88-144/VM/day). The runtime now tags them `[INFO] … (optional — omitted per two-shapes contract)`; a genuinely broken feedback doc is caught earlier by the missing-file / wrong-mode hard fails. So these no longer count against the Warn column. W4 (workflow_weights DISABLED) only fires when BOTH legacy `content.workflow_weights` AND v2 `content.schedule` are absent (commit `4156cbe`, 2026-05-21) — empty `{}` weights inside a schedule block are intentional OFF-night sentinels, not bugs. Also catches `[parser-drift]` WARNINGs (2026-05-25) — the BU/Smol step-action parser stopped recognizing the framework's action vocabulary (version bump), step logging has gone silent, and `_BU_ACTION_MAP`/`_SMOL_ACTION_PATTERNS` need updating. **`[circuit-breaker]` WARNINGs are EXPECTED/benign (2026-06)** — the Smol parse-error breaker aborting a workflow after 4 consecutive `AgentParsingError` on small gemma is healthy, NOT a fault; counted as `WARN_BREAKER_COUNT`, subtracted from the unexpected-warning total for every brain, and reported as `OK (N circuit-breaker)` (also stripped from `WARN_LINES` so it doesn't flood the ISSUES list). High breaker counts mean the small model struggles, not a deploy bug. | `WARN_COUNT − WARN_BREAKER_COUNT + INFO_COUNT` |
| Win | window-mode contract — see below | `WIN_STATE` |
| BG | median D4-only bg-conn/min during ON-windows ≥ 30% of target (floor check — brain workflow conns NOT counted) | `WIN_VOL_MEDIAN` vs `WIN_TARGET` |
| Seed | session_id from latest `session_*.jsonl` filename matches `Random(_metadata.seed).getrandbits(32):08x` — catches Phase 0c install-path bypass bugs | `PHASE_SEED` + `EXPECTED_SID` + `ACTUAL_SID` |
| Pools | Phase 1 fields present in `content` (browse_url_pool / youtube_video_pool / google_search_pool); empty list is OK (intentional floor), missing key is FAIL | `POOL_BROWSE_N` + `POOL_YT_N` + `POOL_SEARCH_N` + `POOL_MISSING` |
| Sched | Phase 2 — `content.schedule` covers all 24 UTC hours when present; `n/a` when PHASE hasn't shipped | `SCHED_STATE` + `SCHED_BLOCKS` + `SCHED_COVERAGE` |
| Svcs | Phase 3 — when any `diversity.background_services.*_enabled` is true, `[scripted-svc]` lines must appear in systemd.log. **In-window-only + catch-up (2026-06-05, commit `26c2489`):** scripted probes fire ONLY from the in-window cluster loop (`emulation_loop.py:583`, same gating as D4 `maybe_generate` one line above) — outside an active window the loop `continue`s past the task body and nothing fires. The scheduler uses **catch-up** semantics (fire the most recent scheduled slot at/before the current minute that hasn't fired this hour) — the prior exact-minute match (`now.minute in schedule`) almost never landed a tick on the 2-min/hr slots, so `failed_conn` (:17/:47) fired 0× over 8.5h even while in-window. **`FAIL (enabled: X but 0 firings)` is BENIGN when the deploy came up off-window or hasn't reached its active window yet** (e.g. window `[[658,1073]]`=10:58–17:53 UTC but VM booted at 20:54 UTC → never in-window → self-clears at next window). **Also benign right after deploy on the slowest CPU brains (B2C/S2C):** probes fire from the in-window cluster loop, so a CPU-BU SUP that has only completed 1-2 slow workflows hasn't cycled enough to hit a probe slot yet — it fires as it iterates (seen on cptc9 2026-06-09: 4/5 SUPs firing, B2C the lone lagging holdout, cleared on its own). Only a real fault if 0 firings persists across in-window periods on a brain that IS cycling. | `SVCS_ENABLED` + `SVC_HITS` |
| Persistent-svc | PersistentSession daemon (2026-06-11) — when `diversity.persistent_sessions.enabled` is true, `[psess] open` lines must appear in systemd.log. `n/a` for C0/M0 or when not enabled; `OK (N opens)`; `FAIL (enabled but 0 opens)`. **BENIGN off-band:** the daemon only opens during the non-zero hours of `session_opens_per_hour` (the active-hours envelope), so 0 opens with a current UTC hour outside that band is correct and self-clears (e.g. sum24 S0C band `[22,23]` UTC shows 0 at 14:00 — not broken). Only a real fault if 0 persists while in-band. Markdown detail column only — not in the compact terminal row. | `PSESS_ENABLED` + `PSESS_HITS` |
| DL | Phase 4 — `download_url_pool` shape detected (dict for bucketed, list for legacy); `behavior.download.{size_mix, outcome_mix}` presence reported in detail | `DL_SHAPE` + `DL_BUCKETS` + `DL_SIZE_MIX` + `DL_OUTCOME_MIX` |

## Window-mode column states (post 2026-05-08)

| `WIN_STATE` | rendered as | meaning |
|---|---|---|
| `FEEDBACK` | `OK feedback (N wins, Mm)` | `_metadata.mode == "feedback"` |
| `CONTROLS` | `OK controls (N wins, Mm)` | `_metadata.mode == "controls"` |
| `parse_error` | `FAIL (behavior.json parse error)` | malformed JSON |
| anything else | `FAIL (mode=X — contract violated)` | schema regression / version skew |

## BG column (post 2026-05-10)

`bg-counter` log lines emitted by `decoys/common/background_services.py`
are scraped from systemd.log. Last 60 in-window samples → median. State:

- `OK ({median}/{target} D4-only)` when ratio ≥ 0.3
- `WARN (..., ratio X)` when ≥ 0.15
- `FAIL (..., ratio X)` below
- `PENDING (no bg-counter samples)` — D4 daemon not emitting
- `PENDING (N samples, no in-window yet)` — bg-counter running, just hasn't aligned with a window yet (typical for fresh deploys; sparse-window datasets can take 1-2h)
- `PENDING (N in-window samples, all conns=0)` — D4 ran in-window but every minute logged 0 conns

**Critical caveat — this is a D4-FLOOR check, NOT a total-network-rate
check.** The bg-counter ONLY tracks D4 background-service probes
(`background_services.py`: dns/http_head/ntp/smb/etc.). Brain workflow
connections (browse_web, google_search, etc.) are NOT counted.
Ground-truthed on 2026-05-10 with tcpdump: real total outbound was
~35 conn/min vs target 7 while bg-counter alone reported 2-3 on the
same SUP. Thresholds are deliberately loose because workflows dominate
the actual emitted traffic; this column only flags "D4 isn't running
at all." If `Win=OK` but `BG=FAIL`, the SUP is almost certainly fine.

**cptc datasets read BG/volume=FAIL — RUSE-mechanism fact, NOT a deploy fault.**
cptc8/cptc9 behavior.json carry a competition-scale
`target_conn_per_minute_during_active` (185 cptc9 / 208 cptc8), which D4's
~16/min ceiling (`feedback_d4_throughput_ceiling`) structurally cannot reach —
and the BG column only measures the **D4 floor** (`conns=` field, `audit.py:361`),
NOT total outbound. So `BG=0/N` and `volume=FAIL (≈8/208, ratio ≈0.04)` are
PERMANENT and EXPECTED on every cptc deploy. The real total-outbound signal
(`active_opens`, BU browsers 150-170/min) is healthy. Confirmed on
`decoy-feedback-expctrlsv716-cptc{8,9}-all-rtx` (2026-06-17 fleet audit).
Targets are preset-dependent: `ragged-ctrls-all_v10.0.5` (2026-07) ships
cptc9 target 53 (not 185) — still >16/min so cptc9 stays red (ratio
~0.15), while cptc8 under the same preset mostly clears 0.3 (4/5 BG OK,
2026-07-22 fleet audit). `ragged-ctrls-all_v11.2.1` (2026-08) ships
cptc11-zeektx target **2592**/min — the deployed cptc11 fleet
(2026-08-12, rtx tier) will read BG/volume red PERMANENTLY (ratio
≈0.006); same artifact, never a fault.

**Fresh-deploy (<~4h) benign patterns (2026-07-22 fleet audit — all
known self-clearing classes):** volume ratios just under the 0.3 floor
(0.16-0.27 WARNs) while in-window medians accumulate; scripted_svc /
persistent_session `0 firings/opens` on VMs that haven't reached their
active window or band yet (see Svcs / Persistent-svc caveats); and
neighborhood sidecars hitting only a subset of SUPs in the first hours
(probe routing is stochastic per-SUP rates). Re-audit after a few
in-window hours before treating any of these as faults.
**CAUTION (2026-06-19) — do NOT extend this to "so cptc scores ~0 on the
model."** That was an OVERSTATEMENT. Whether cptc's exp-model score depends on
connection VOLUME/rate is an OPEN PHASE QUESTION, not RUSE-verifiable: PHASE's
own `dataset_realism_keys` flags cptc9 as *"genuinely coverage-limited"* (unlike
the AXES datasets, where volume is a disproven lever, and where spring25 even had
a VOLUME-group win). The BG=FAIL is a measurement artifact of a capped minor
channel vs a large target — it is NOT evidence about the score. The score
question resolves only via PHASE's dredge/re-infer. See `/feedback-investigation`.

**Future column (not wired):** the `network_sample` jsonl event (2026-06-01,
`active_opens`/`distinct_hosts` from `OutboundConnSampler`) and the new
`active_opens=`/`hosts=` fields on the `[bg-counter]` line carry the REAL
total-outbound rate (workflow + D4), unlike `WIN_VOL_MEDIAN` which is D4-only.
A future Vol column should prefer `active_opens` for an actual-traffic check
rather than the D4 floor. Not implemented yet — `WIN_VOL_MEDIAN` still reads
the `conns=` field.

## Shape-floor / connection-shape — what to watch (Build #5, 2026-06-25)

NOT a wired audit column yet — you read the `[shape]` minute log + `[shape-floor]`
line by hand (`tail .../systemd.log | grep '\[shape'`). Fires only on the **exp**
lineage (`connection_shape.enabled`); std/controls never emit it. Validated on the
`exp-ctrls-all_v7.1.7` axes-2025 CPU canary (2026-06-25). What's benign vs a real fault:

| Signal (in `[shape]` / `[shape-floor]`) | Healthy | Real fault → action |
|---|---|---|
| `[shape-floor] daemon started endpoints=N max_concurrent=80` | present once when `connection_shape.enabled` | absent → floor not wired / empty `endpoint_pool` |
| floor activity **outside `active_minute_windows`** | **zero new opens — window-gated since 2026-08-14.** The floor previously ran 24/7 (the only always-on channel with no window gate), so every off-window minute was pure idle-floor spend with no shape benefit. It now self-gates on UTC minute-of-day in its own thread (`shape_floor._in_window`, same idiom as psess's `session_opens_per_hour` envelope). Open conns are NOT force-killed at window close — they expire into a graceful FIN (`SF`) | new opens continuing well outside every window → gate not wired |
| `shaped_share` | settles **~50–65% at T=0.55** (recalibrated 2026-06-29; was ~70–103% at the old T=0.82) in steady minutes; **bouncing 0→150% min-to-min is NORMAL** (closes÷opens offset) | stuck well <40% across MANY high-`active_opens` minutes → floor not opening |
| `agg_dur_p50` (binding feature) | clears ~0.6× the `/target` in high-share minutes (canary 13–14s @ target 13); **=0 in lean minutes is benign** (offset) | flat ~0 across ALL minutes incl high-share → floor not holding conns |
| `agg_bytes_p50` | ~target in shaped-heavy minutes | persistently ~128 (TINY) even at high share → sampler dead |
| `floor_target` | tracks **~1.22×unshaped at T=0.55** (was ~4.56× at T=0.82 — verified on the 06-29 redeploy: active_opens 74→floor_target 75, 21→7); rarely hits the 120 cap now, and doing so is benign (guardrail) | always 0 despite low share + real browsing **AND `floor_cap` not binding** → deficit calc broken |
| `floor_cap=` / `budget=` (NEW 2026-08-14) | `floor_cap` = `max(0, budget − active_opens)`, the connection-budget headroom the floor must fit in; `budget` = conn/min from PHASE `workflow_budget.target_conns_per_hour` (falls back to `target_conn_per_minute_during_active`). `floor_cap=-`/`budget=-` means no budget expressed → uncapped, prior behavior | — |
| `floor_target=0` **with `floor_cap=0`** (NEW) | **BENIGN + INTENDED** — the SUP is already at/over its connection budget, so the discretionary floor correctly yields. Expect this constantly on budget-constrained axes deploys (`_idle_overrun: true`, e.g. fall24 budget 0.95 conn/min). **Do NOT read it as "floor broken"** — cross-check `floor_cap` before diagnosing a zero `floor_target` | `floor_cap` large but `floor_target` still 0 → real |
| `wf_complete=c/s` | see the rule below | sag **with socket errors** → T too aggressive |
| `[WARNING] [shape]` | 0 | present → malformed dist (counts in the Warn column) |

**`wf_complete` — the over-aggression gate, but DON'T misread it.** A low/0 `wf_complete`
means "T too aggressive / floor starving workflows" **only if** the SUP log also shows
`too many open files|connection refused|EMFILE`. If instead it shows `llm_error|timeout|
cancel` high with **0** socket errors, it's the brain's LLM, not the floor —
**BrowserUse on CPU (`gemma4:e2b`) times out on big prompts**, so CPU-tier `B2C` will
read low completion regardless of the floor. Decisive cross-check: if one brain completes
healthily under the same floor load and another doesn't, the floor is innocent (canary:
S2C 5/6 vs B2C 0/2, B2C = 30 LLM timeouts / 0 socket errs). **Fleet V100 brains
(gemma4:26b) don't have this — never gate the fleet on a CPU-BU `wf_complete`.** If it IS
real starvation, the fix is the one-line `_FLOOR_SHARE_TARGET` (shape_controller.py — now
**0.55**, recalibrated from 0.82 on 2026-06-29 after the live deploy overshot the human
byte/packet shape; lower further if starvation appears).

**Don't over-read per-minute.** `shaped_share`/`agg_*_p50` swing because they compare
closes-this-minute vs opens-this-minute, offset by the 13–70s holds — worst at low (CPU)
volume. The **SUP-day aggregate the model reads converges**; the minute log is
observability only. Read the trend across high-`active_opens` minutes.

**Endpoint concentration:** if checking held-conn spread (`ss -tn state established '( dport
= :443 )'`), sample over a ~40s WINDOW (several snapshots) — one instant can falsely show a
single peer; over a window the floor spreads across the pool. Floor opens have **no**
per-open log; `grep floor` matches `floor_target=` in every `[shape]` line (not evidence of
conns) — confirm opening via concurrent `ESTABLISHED :443` or `n_obs>0`.

## Neighborhood sidecar probe (post 2026-05-11)

Each feedback deploy has one neighborhood VM (`d-{dep_id}-neighborhood-0`)
listed in `runs/{rid}/neighborhood-inventory.ini` — separate from
sup_hosts. `_neighborhood_probe()` SSHes the sidecar (**by inventory IP,
`ubuntu@{ip}` — fixed 2026-06-28; previously SSHed the hostname, which is
absent from `~/.ssh/config` and not reliably in internal DNS → intermittent
`FAIL (Could not resolve hostname)` false-positives on ACTIVE sidecars, e.g.
vt50g-rtx-a**) and emits:

| key | meaning |
|---|---|
| `ACT` | `systemctl is-active ruse-neighborhood` |
| `NR` | NRestarts |
| `UPTIME_S` | seconds since service became active (stale-NR gate) |
| `PROBES_LAST_HR` | probe events in `/var/log/ruse-neighborhood.systemd.log` in last hour |
| `PROBES_TOTAL` | cumulative probes in `/var/log/ruse-neighborhood.jsonl` |
| `SUPS_HIT` | distinct sup names in last 400 probe events |
| `PROBE_TYPES` | distinct probe types (out of 10 configured) |
| `TARGETS` | sup count in `/etc/ruse-neighborhood/sups.json` |

**Why two log files**: daemon writes pure JSON (no timestamps) to
`/var/log/ruse-neighborhood.jsonl` and timestamped stdout to
`/var/log/ruse-neighborhood.systemd.log` (via systemd
`StandardOutput=append:`). Time-windowed counts MUST come from
`.systemd.log` (the jsonl is timestamp-less). Don't grep journalctl —
the unit redirects stdout to a file, journalctl only has systemd's
own start/stop messages.

**Timestamps are LOCAL time, not UTC**. The bracketed prefix
`[YYYY-MM-DD HH:MM:SS,microseconds]` is whatever the daemon process saw
from `time.strftime` — VM tz is `America/New_York` per
`install-neighborhood.yaml`. The probe's cutoff uses local `date`, not
`date -u`. Lifting that to UTC requires either tz-aware daemon output
or a tz-converting awk filter.

`_classify_neighborhood()` returns:

- `OK ({probes_hr}/hr, {sups_hit}/{targets} SUPs)`
- `WARN ({probes_hr}/hr, only {sups_hit}/{targets} SUPs hit)` — partial routing
- `FAIL (silent daemon — 0 probes/hr)` — service active but emitting nothing
- `FAIL (no jsonl — daemon never wrote)`
- `FAIL (crash-looping, N restarts, up Ns)` — NR>5 within first 600s
- `FAIL (service {state})` — not active

Rendered as a separate "Neighborhood sidecars" table after the main
13-column SUP summary. Sidecar failures become issues in the same
ISSUES list. Per-row marker in the live probe progress is `..nbhd...X`
or `..nbhd....` to visually distinguish from SUP rows.

## IDLE → OK / FAIL post-pass

Ollama unloads idle models after 5 min. V2+ calibrated agents sleep up
to 1h between clusters. So `Model=IDLE` and `GPU=IDLE` are normal mid-
quiet. The post-pass rule: if `Service=OK + Process=OK`, IDLE becomes
`OK (idle)`; otherwise `FAIL (not loaded)`.

## Cross-deployment checks

Run after every per-VM probe completes:

- **Orphan / missing**: diff inventory.ini hostnames against
  `openstack server list` filtered by `_dep_prefix(dep)` =
  `make_vm_prefix(make_run_dep_id(...))`. Neighborhood VMs (in
  `neighborhood-inventory.ini`, NOT `sup_hosts`) excluded from orphan
  check by suffix `-neighborhood-0`.
- **PHASE registration**: every inventory IP must appear in
  `experiments.json[deployment_name].vm_ips`. Reports IPs missing
  from registration.
- **Duplicate run_ids**: per config_dir, more than one `runs/*` is
  fine (history); but two ACTIVE runs (both with inventory.ini) is a
  bug.
- **Orphan boot volumes**: nameless 200 GB available volumes on
  OpenStack — leftover from incomplete teardowns.

## Markdown report

Written to `deployments/logs/audit_{YYYYMMDD-HHMMSS}.md`. Two sections:

1. Summary table (one row per deployment, 13 columns including run_id and Win+BG counts)
2. Per-deployment per-VM detail (every check verbatim)

The `_row_status()` helper emits a 11-char compact status string
(`. = pass, X = fail, W = warning, ? = unknown`) for terminal one-liners.

## Behavior helpers (shared with INSTALL_SUP.sh logic)

```python
expected_model("B0.gemma")  → "gemma4:26b"
expected_model("B2R.gemma") → "gemma4:e4b"   # RTX 2080 Ti (both --gpu rtx and --gpu rtx-a)
expected_model("B0C.gemma") → "gemma4:e2b"   # CPU variant
expected_model("M1")        → None            # MCHP no LLM
expected_service("B0.gemma") → "b0_gemma.service"   # dot → underscore
expected_service("B2R.gemma") → "b2r_gemma.service" # R-tier same shape
needs_gpu("B0.gemma")       → True            # V100 flavor
needs_gpu("B2R.gemma")      → True            # RTX 2080 Ti flavor
needs_mchp_cron("M1")       → True            # M-brains only
```

These mirror the resolution in `INSTALL_SUP.sh::MODEL_NAMES`. Drift here
will cause every audit to misreport — keep the two in sync when adding
new behaviors. R-infix and C-infix behaviors share the same `.gemma`
behavior_dir as their non-infix V100 sibling (per
`config_key_to_behavior_dir` regex `^([A-Z])\d+[A-Z]*(?:\.(\w+))?$`),
so PHASE feedback at `{behavior_dir}/{baseline_config}/behavior.json`
is shared across all three tiers — only `expected_model` differs.

The audit does NOT distinguish the two RTX pools (non-A `rtx2080ti:1`
vs A `2080ti-rtx-a:1`). From the audit's POV both are R-tier with
identical expected model + VRAM thresholds; the pool a VM actually
landed on is the deploy_name suffix (`-rtx` vs `-rtx-a`) and the
OpenStack flavor in the inventory metadata, neither of which the
per-VM probe currently inspects. If a VM somehow ends up on the wrong
flavor, the audit won't catch it.

## Constants

| constant | value | purpose |
|---|---|---|
| `LOG_FRESHNESS_SECS` | 86400 (24h) | catches stuck agents past inter-cluster sleep window |
| `EXPERIMENTS_JSON` | `/data/axes-mirror/experiments.json` | PHASE registration table |
| service NRestarts threshold | 10 | only flips Service to `crash-looping` if uptime < 600s; high NRestarts on a stable service is reported as `OK (N restarts, stable Mm)` |
| `STABLE_UPTIME_S` | 600 | continuous-active gate that suppresses NRestarts noise from past crash-loops |
| BG OK / WARN ratios | 0.3 / 0.15 | median D4-only conn/min vs target_conn_per_minute_during_active (deliberately loose — D4 is one stochastic contributor; workflows dominate total traffic) |

## Common usage

```bash
./audit                                  # full report (default --decoy)
./audit --decoy                          # explicit
./audit | grep -E "FAIL|^Issues"         # just the fails
./audit | grep Fdbk                      # behavior.json status across all
ls -t deployments/logs/audit_*.md | head -1 | xargs less
```

To debug a specific VM:

```bash
ssh d-controls050826193122-B0-gemma-0 \
  "systemctl status b0_gemma; journalctl -u b0_gemma --no-pager | tail -30"
ssh d-controls050826193122-B0-gemma-0 \
  "tail -f /opt/ruse/deployed_sups/B0.gemma/logs/systemd.log"
```

## Related

- Full deploy lifecycle: `/decoy-deploy`
- RAMPART audit: `/rampart-audit`
- GHOSTS audit: `/ghosts-audit`
- behavior.json schema + window-mode contract: `/decoy-deploy`
- jsonl log schema (canonical workflow names, real step outcomes/durations,
  BU history-walk vs Smol step_callback, download/whois detail fields,
  parser-drift guard, schedule-idle interpretation): `/decoy-deploy`
  → "Logging output (jsonl)" section
