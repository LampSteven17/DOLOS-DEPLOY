---
name: feedback-investigation
description: RUSE-side feedback-investigation reference (pinned) — how PHASE realism feedback drives DECOY behavior, what the runtime actuates, and (critically) the RUSE-fact vs PHASE-model-claim boundary. Covers the realism contract (exp model scores SUP-day near-binary on per-connection byte/packet/duration SHAPE + conn_state mix, NOT volume/coverage — a PHASE finding), the behavior.json shape contract (diversity.connection_shape + conn_state_mix), the Phase-1 closed-loop ShapeController (common/network/shape_controller.py), the build backlog (#1+#5 done, #2 response-endpoints, #3 packetization, #6 internal-responders), how to verify shape on the wire (canary methodology: [shape]/[psess]/[scripted-svc src=rate] logs + tcpdump SYN ground-truth + emit-side ledger), what the audit's volume column actually measures (D4 floor, not total), and the open questions that must be routed to PHASE (e.g. is cptc's score volume-sensitive). Use when investigating realism feedback, reasoning about whether a deploy will move the model score, or deciding RUSE-side vs PHASE-side ownership of a question. Does NOT cover deploy mechanics (/decoy-deploy), audit columns (/decoy-audit), or runtime call-graph (/decoy-mechanical-systems); PHASE feedback generation is read-only upstream (~/PHASE/).
---

# feedback-investigation

The RUSE-side companion to PHASE's realism investigation. PHASE measures what
makes a synthetic SUP-day look real to the exp model and emits a `behavior.json`
contract; this skill is how the **runtime maintainer** reasons about that
contract — what RUSE actuates, how to verify it on the wire, and (the part that
bit us) **which questions RUSE can answer vs which belong to PHASE**.

> This skill is the durable "pinned" reference. Operational mechanics live in the
> deploy/audit/mechanical-systems skills; this is the *why* + the *epistemics*.

## 0. The one rule — RUSE-fact vs PHASE-claim

The single most important discipline here. There are two kinds of statement and
they must never be conflated (we conflated them once on cptc/volume, 2026-06-19,
and it was wrong):

| RUSE-fact (verifiable here) | PHASE-claim (model-side, NOT RUSE-verifiable) |
|---|---|
| What a behavior.json field contains | Whether that field moves the model score |
| What the runtime emits on the wire (tcpdump, `/proc`, logs) | Whether that emission is "realistic enough" to pass |
| What the audit column measures (read the code) | Whether a red/green column predicts pass-rate |
| The emit-side ledger: bytes/dur/conn_state we actually produced | Which feature flips a dead SUP-day to passing |
| That D4 is capped at ~16/min and can't hit a 185/min target | Whether hitting 185/min would change anything |

**RUSE owns the mechanism. PHASE owns the score.** The exp model lives on the
PHASE side; RUSE cannot prove "this deploy will pass" or "this lever is dead" —
only PHASE's dredge → re-infer → pass-rate can. When you catch yourself asserting
that an on-wire fact *causes* a score outcome, stop: that's a PHASE question.
Verify the mechanism, state it as fact; route the score implication to PHASE.

**Steering tier — don't false-flag absent shape as a defect (2026-06-27, the lesson
re-learned).** PHASE `decoy_generator._steering_tier` is a do-no-harm guard keyed on
`baseline_score` (invisible to RUSE): **full** (<0.50) gets `connection_shape`+pool+psess;
**cautious** (0.50–0.60) gets NO shape, psess on, pool=8; **hold** (≥0.60) gets NO shape,
**psess off, pool=[]**. So absent `connection_shape` / empty `endpoint_pool` on a
high-baseline SUP is BY DESIGN (the floor would be pure footprint-downside). When auditing a
feedback gen's shape coverage, **do NOT require "all 7 configs shaped"** — verify only the
tier-agnostic invariants RUSE CAN check: (1) `enabled=true` ⇒ non-empty pool, (2) `enabled=true`
⇒ monotonic percentiles, (3) empty pool ⇒ `psess.enabled=false`. Those three failing are the
only real defects; everything else is steering. Memory `project_phase_steering_tier`.

## 1. The realism contract (PHASE finding — relayed, not RUSE-proven)

From PHASE's investigation (`~/PHASE/feedback_engine/knob_investigation/`):

1. **Score is near-binary per SUP-day** (≈0.999 pass / ≈0.00 dead). Judge by
   **pass-rate**, never mean score.
2. **The lever is per-connection BYTE / PACKET / DURATION shape + conn_state
   mix** — one controllable shape feature flips 40–100% of dead SUP-days to
   passing on 9/11 non-cptc datasets.
3. **Coverage / active-minutes is a disproven lever** on AXES (flips ~0,
   "disproven 4×"). **Volume (conn/min)** is *mostly* dead too — BUT not
   universally: spring25 had a VOLUME-group win (`f_vol` 0.69), and **cptc9 is
   flagged "genuinely coverage-limited."** So "volume is dead" is an AXES
   generalization, NOT a universal law — see §6 cptc.
4. **The failure mode is day-to-day VARIANCE**, not missing capability — the
   engine emits a passing shape on good days and a dead one on others. The
   contract's job: specify the target distribution and hit it consistently.

Per-dataset "realism key" table (which lever per dataset) lives in
`~/PHASE/.../dataset_realism_keys.md` — READ-ONLY upstream, point to it, don't
re-derive. Taxonomy: already-working / byte-duration-fixable-now /
packet-lever-needs-knob / network-hard / cptc-separate.

## 2. The behavior.json shape contract (what RUSE consumes)

PHASE emits under `diversity.*`; RUSE parses in
`common/behavioral_config.py` (feedback branch), absent-safe (missing → OFF →
scalar fallback; controls mode stays shape-free). Schema CONFIRMED with PHASE
2026-06-16.

```jsonc
"diversity": {
  "connection_shape": {                 // NEW Phase 1; absent → OFF
    "enabled": true,
    "orig_bytes": {"p25","p50","p75","p90","max"},  // ACTUATED (persistent-session per-conn sampling)
    "duration":   {"p25","p50","p75","p90","max"},  // ACTUATED (per-conn lifetime)
    "orig_pkts":  {...},                // parsed, NOT actuated → build #3
    "resp_bytes": {...}, "resp_pkts": {...},  // parsed, NOT actuated → build #2
    "_skipped": ["orig_pkts","resp_pkts"],    // PHASE: features it omitted for this dataset (e.g. cptc)
    "_source": "...", "_schema": "..."        // informational; parser ignores
  },
  "conn_state_mix": {"SF","failed_conn","OTH","RSTR"},  // FOLD: REJ+S0 → failed_conn; SF uncontrolled baseline (reference only)
  "persistent_sessions": { ... "orig_bytes_per_session","session_duration_seconds" }  // scalars DEMOTED to fallback
}
```

- `max` is now **p99** (PHASE corrected 2026-06-16 — physical, not the unphysical
  dataset max). RUSE clamps every sample to it as a hard ceiling. `_valid_dist`
  asserts monotonic + `max ≥ p90` (PHASE clamps on emit; cheap to re-assert).
- **Absent / `enabled:false`** → feature OFF, scalar fallback. **`enabled:true` +
  malformed dist** → warn-loud (`[WARNING] [shape]`, audit-caught) + fallback,
  NEVER a fleet crash (fail-loud is reserved for the load-time mode/missing-file
  contract, not producer typos).
- Rollout is UNEVEN across datasets/configs (HOLD tier = no shape; some patchy
  per-config). Always check coverage before assuming a deploy is shaped.

Full parse→consumer map: `/decoy-deploy` "PHASE feedback runtime consumption".

## 3. What the runtime actuates — the Phase-1 ShapeController

`common/network/shape_controller.py` (PUSHED main `2aebd2e`; failed_conn fix
`c721886`). The closed-loop actuator + corrector. Code-map: `/decoy-mechanical-systems` §7.

| Feature | Status | Channel / mechanism |
|---|---|---|
| `orig_bytes` p25–p75 | ✅ ACTUATED | per-conn sample on the persistent-session channel; bias-corrected from the **emit-side ledger** each minute toward target p50 |
| `duration` | ✅ ACTUATED | per-conn session lifetime (sample replaces the scalar lognormal center) |
| `orig_bytes` p90/max | ⏸ saturates | 4 KB/request cap + early server-close → high percentiles need build #3; **read p25–p75 only, hold p90/max** |
| `conn_state_mix.failed_conn` | ✅ ACTUATED | wall-time **token bucket** → non-blocking SYN probes at `failed_conn_frac × active_opens`/min (cadence-independent; `scripted_services.py`) |
| `orig_pkts` | ❌ build #3 | packetization (TCP_NODELAY + split writes) |
| `resp_bytes` / `resp_pkts` | ❌ build #2 | needs a RUSE-controlled response endpoint |

**Measurement source (critical, RUSE spec §B.1):** the closed loop measures from
the **emit-side ledger** — each channel reports every connection it closes
(`bytes_cum`, wall-duration, conn_state). `/proc`/`conn_sampler` give only an
aggregate COUNT (`active_opens`), used to scale the failed_conn rate — NOT a
per-connection byte/pkt/duration distribution, and conn_state is invisible to
`/proc` entirely. Do not "close the loop on conn_sampler" — it can't see shape.

## 4. Build backlog (RUSE-side, ranked)

1. **Closed-loop shape controller** — ✅ DONE (Phase 1 + 1.1). Fixes variance +
   p50-vs-distribution + channel dilution; failed_conn now cadence-independent.
2. **RUSE-controlled response endpoints** — unlocks `resp_bytes`/`resp_pkts`
   (today server-bound; `Range: bytes=0-N` on range-honoring statics is a partial
   stopgap). New conn source must feed D4 net-out.
3. **Packetization control** — `orig_pkts` via `TCP_NODELAY` + split `send()`;
   also lifts the orig_bytes p90/max ceiling.
4. **Distribution sampling + workflow byte-padding** — extend per-conn sampling to
   the other `requests`-based channels (download/whois/controls). NOTE browser
   workflow bytes are NOT paddable (Selenium/Playwright) without a proxy — that's
   what #5 substitutes for.
5. **Universal shape-floor channel** — ✅ DONE (2026-06-25, `common/network/shape_floor.py`).
   Was the coverage bottleneck: PHASE found only ~30% of conns shaped (psess = the
   on-target p75–p90 tail); ~52% are unpaddable instant browser GETs (≤200B/~0s) that
   dominated the median (emitted orig_bytes p50 ~128B vs ~1022B; dur p50 ~0s vs ~14.5s).
   `ShapeFloorDaemon` is the persistent daemon's own-thread twin but **coverage-driven**:
   `ShapeController.floor_opens_target_per_min()` computes `T/(1−T)×unshaped` synthetic
   shaped opens (sampling the SAME `connection_shape` dist, full range not just the tail)
   so SHAPED becomes share **T=0.55** (`_FLOOR_SHARE_TARGET`; **recalibrated 0.82→0.55 on
   2026-06-29** — the sim's 0.82 OVERSHOT the human shape on the live deploy: orig_bytes
   1.35×/orig_pkts 2.18×/resp_bytes 1.6×, which crashed axes-2025 −0.15; PHASE T-sweep on
   the deployed conns put 0.55 on target for both bytes 0.96× and packets 1.01×. Residual:
   packets stay ~1.0–1.3× regardless of T — each floor conn is a real TLS conn with fixed
   packet overhead; lighter floor conns are a follow-up build, not a T tweak) of per-conn
   mass — drags the
   aggregate median to target. **Duration is the binding constraint** (PHASE sim: bytes
   clears ~0.69, dur needs ~0.80, since human dur p25 0.3s/p50 14.5s is far right-skewed).
   2nd emit-side reporter (channel `"floor"`); opens net out of D4. `[shape]` log now carries
   `agg_bytes_p50`/`agg_dur_p50` (estimated aggregate, acceptance ≥ ~0.6× target p50) +
   `shaped_share` + `wf_complete` (over-aggression signal — if completion sags the floor is
   starving browse-workflows, lower T). T is a module constant; first deploy is the
   calibration run. **NOT pushed yet.**
6. **Internal decoy-intranet responders** — hosts answering enterprise ports on
   internal IPs → `local_resp=T` + real `service`/`conn_state` (incl. reliable
   REJ, which lets PHASE split REJ back out of `failed_conn`). The only path for
   topology-bound / cptc-class network features. Big infra.
7. **Lighter floor connections** (NEW, surfaced by the 2026-06-29 T-recalibration) —
   each shape-floor conn is a real TLS connection with FIXED per-conn packet overhead, so
   `orig_pkts`/`resp_pkts` stay ~1.0–1.3× the human target at ANY `_FLOOR_SHARE_TARGET`
   (the packet overshoot is structural, not a coverage-share knob). Make floor conns
   lighter — fewer round-trips per conn, smaller responses (e.g. drop/trim the response
   drain, single-request lifetimes, `Range:` tighter) — to pull packets onto target without
   sacrificing the byte/duration coverage T buys. Required before the floor's packet shape
   is clean; T can only fix bytes.
8. **Rate/budget throttle (task-value engine)** — ✅ DONE (2026-08-14, response to
   PHASE's `RUSE_HANDOFF_taskvalue_2026-08-14.md`). PHASE's v11.2.1 batch flipped the
   problem on AXES: the fleets ran **8× OVER** budget (fall24 target 6.3 conn/min vs 53
   achieved), with `multiplier` pinned at the **0.15 clamp floor** and
   `target_execs_per_hour` ≈ **0** — i.e. "stop nearly all workflow execution AND cut the
   idle floor." Root cause was structural, not tuning: nothing paced workflow EXECUTIONS
   to a connection budget (weights chose the mix, nothing chose the rate), and the
   shape-floor had neither a rate ceiling nor a window gate while self-inflating off the
   global ActiveOpens delta. Built: `common/exec_governor.py::ExecGovernor` (wall-clock
   token buckets on `target_execs_per_hour` + `cadence_cap_per_hour`) and a two-part
   idle-floor cap (budget clamp via `ShapeController.set_conn_budget_per_min`, plus a
   `active_minute_windows` gate on `ShapeFloorDaemon`). Verified in-repo against all 56
   v11.2.1 configs: axes drops ~1200 → ~3 execs/day, cptc11 paces to its 35/hr cadence
   cap, absent-budget path byte-identical. **Awaiting canary + PHASE re-infer** — RUSE
   owns the mechanism, PHASE owns whether the score moves.

### cptc11 — the handoff §4 scan primitive does NOT match the emitted targets

Read this before anyone builds `NetworkProbe`. The handoff prose describes a red-team
SYN-sweep (64% SF / 22% S0, sub-ms, ~74 B resp, ~0 orig pkts, 364 conn/min). The
**emitted** `cptc11-zeektx` behavior.json (stamped `_source: "human active-minute
distribution, cptc11-zeektx"`, so these ARE the human targets) says something else
entirely, identical across all 7 sup_configs:

| Feature | Handoff §4 prose | Emitted cptc11 |
|---|---|---|
| conn_state | 64% SF / 22% S0 | **56% OTH**, 39% SF, 5% failed_conn |
| duration p50 | sub-ms | **4.8 s** |
| resp_bytes p50 | ~74 B | **13.8 KB** |
| resp_pkts p50 | ~0 | **19** |
| service mix | — | **dns 85% / ssl 11% / http 4%** |
| rate | 364/min | **2592/min** (achieved 38 → 68× gap, `multiplier` at the 1.0 ceiling) |

A raw-TCP scanner emits precisely the WRONG shape here (sub-ms, empty-service, S0).
The reachable levers for cptc11 are, in order: **service mix** (dns 85% — a dedicated
high-rate short-conn DNS channel, since D4's ~16/min `burst_n≤8` ceiling is the binding
constraint, not sockets), then **orig_bytes/duration** (actuatable today), then
**resp_bytes 13.8 KB** (blocked on build #2). Rate 2592/min ⇒ 43 conn/s ⇒ ~207
concurrent conns ⇒ ~78 GB/day across a 5-VM cohort — a stretch, and the
`achieved_conn_per_active_min`/`multiplier` clamp loop is exactly the mechanism for
discovering the reachable ceiling. **PHASE confirmed the mismatch 2026-08-14 and owns
the §4 correction; it does not block the axes work.**

## 5. How to verify shape ON THE WIRE (canary methodology)

The canary is the only place a real shape claim can be checked. The ladder that
caught the failed_conn under-fire bug (a unit test could not):

1. **On-disk:** `connection_shape`(enabled)+`conn_state_mix` present, `_valid_dist`
   passes, 0 `[WARNING] [shape]`.
2. **Controller live (logs):** `[shape] bytes_med=<emitted>/<target> bias=… dur_med=…/…`
   `agg_bytes_p50=<est>/<target> agg_dur_p50=<est>/<target> shaped_share=N% floor_target=K`
   `wf_complete=c/s failed_conn_rate=… active_opens=… n_obs=…` each minute — bias should
   track measured→target; `n_obs>0` means the emit-side ledger is receiving closes.
   **`agg_*_p50` = ESTIMATED aggregate (the model-read median); acceptance is per-target,
   `agg_*_p50 ≥ ~0.6× the /target shown` (NOT a literal 800B/8s — those were spring25).**
3. **Channels firing (logs):** `[psess] open … bytes_cum≈target` (byte/dur tail);
   `[shape-floor] daemon started endpoints=N max_concurrent=80` (Build #5 floor channel up —
   note floor opens have **no** per-open log by design; confirm it's working via concurrent
   `ESTABLISHED :443` conns (`ss`) or `n_obs>0`, NOT a `grep open` — and `grep floor` matches
   `floor_target=` in every `[shape]` line, so it's NOT evidence of floor conns);
   `[scripted-svc] failed_conn fired=N rate=R src=rate` (conn_state lever).
4. **Ground-truth (tcpdump):** `sudo tcpdump -ni any 'dst host 1.1.1.1 and dst port 1 and tcp[tcpflags] & tcp-syn != 0'`
   while firing — confirms the non-blocking SYNs actually hit the wire (Zeek-visible).
   Byte/dur lever ground-truths via Zeek `conn.log` (PHASE-side dredge).

**Gotchas that look like faults but aren't (verify before alarming):**
- **Off-window / off-band quiet.** Workflows + scripted probes fire in-window only;
  the persistent daemon opens only during non-zero `session_opens_per_hour` hours.
  A deploy whose schedule doesn't cover the current UTC hour shows `persistent=0
  opens` + `scripted=0 firings` + low BG — CORRECT, self-clears at its window.
  ALWAYS check `active_minute_windows` + `session_opens_per_hour[utc_hour]` vs now
  before calling it broken (2026-06-17: 2025 deploy active only 19:36–20:36 UTC;
  cptc band hours 14–23). See `/decoy-audit` benign-states.
- **failed_conn fires only in-window AND once there's traffic** (rate = frac ×
  active_opens; off-window active_opens≈0 → rate 0). To test immediately, force
  always-active: edit the on-VM `behavior.json` `active_minute_windows` → `[[0,1440]]`
  (sudo; `.bak` it), `systemctl restart <svc>`. Throwaway hot-patch; teardown clears.
- **Slow-brain cadence.** BU blocks the main loop in `agent.run()` for minutes, so
  main-loop-driven channels look sparse; the persistent daemon (own thread) and the
  token-bucket actuator are cadence-independent by design.

**Shape-floor (Build #5) — canary watch-points (validated 2026-06-25, `exp-ctrls-all_v7.1.7`
axes-2025 CPU canary):**
- **Per-minute `shaped_share`/`agg_*_p50` are NOISY — do NOT over-read them.** `shaped_share`
  = closes-this-minute (`n_obs`) ÷ opens-this-minute (`active_opens`), offset by the 13–70s
  hold times → swings 0→150%+ minute-to-minute, brutal at low (CPU) volume. **The SUP-DAY
  aggregate the model reads converges (closes≈opens over a day); the minute log is
  observability only.** Read the TREND across high-volume minutes, not any single line.
- **`agg_dur_p50=0` in lean minutes is benign** (a conn opened this minute with a 40s hold
  isn't in `n_obs` yet — biases the minute down, not the day). The real signal: `agg_dur_p50`
  **clears ~0.6× target in shaped-heavy minutes**. "Flat near 0 across ALL minutes incl
  shaped-heavy" = floor not holding → real. (Historical: the T=0.82 canary hit dur 13–14s at
  95–103% share; at the recalibrated **T=0.55** steady share is **~50–65%**, so the on-target
  minutes cluster at lower share — judge against ~55%, not the old canary's 95%.)
- **`wf_complete` is the over-aggression gate, but distinguish floor-starvation from
  brain-LLM slowness.** A low/0 `wf_complete` only means "T too aggressive" if accompanied by
  **socket errors** — grep the SUP log: `too many open files|connection refused|EMFILE`
  present ⇒ floor starving conns ⇒ drop `_FLOOR_SHARE_TARGET` further (now 0.55). If instead
  `llm_error|timeout|cancel` is high with **0** socket errors ⇒ it's the brain (BrowserUse on
  CPU/`gemma4:e2b` times out on big prompts) — NOT the floor. Decisive cross-check: on the
  canary B2C/BU read 0/2 (30 LLM timeouts, 0 socket errs) while S2C/Smol read 5/6 under the
  SAME floor load → floor is innocent. **CPU-tier canaries will always show BU low-completion;
  fleet V100 brains (gemma4:26b) won't** — don't gate the fleet on a CPU-BU `wf_complete`.
- **`floor_target` hitting the `_FLOOR_MAX_OPENS=120` cap at high volume is benign** — it's a
  guardrail, and the floor's own opens inflate `active_opens` → inflate the apparent unshaped
  residual (mild feedback). At **T=0.55 the cap rarely binds** (multiplier 1.22× vs 4.56× at
  0.82); the system converges to **~50–65% share** (not the old 95–103%). Raise the cap only if
  the highest-volume V100 minutes ever need to push past it.
- **Single-peer/endpoint concentration: sample over a WINDOW, not one `ss` snapshot.** A single
  instant can falsely show 1 peer; over ~40s (several samples) the floor spreads across the
  pool via `rng.choice` (canary: 6 hosts). A host listed twice in `endpoint_pool` (PHASE pool
  quirk) gives a mild ~2× skew — cosmetic, no dest-host-diversity feature in the 20.

## 6. The volume question — a worked example of §0

`volume=FAIL` on cptc is the canonical RUSE-fact / PHASE-claim trap.

**RUSE-fact (verified in code):**
- The audit BG/volume column reads the **D4-only** `conns=` field
  (`audit.py:361`, `WIN_VOL_MEDIAN` ← `[bg-counter]…conns=`), NOT total outbound.
  It excludes workflow conns, persistent opens, scripted probes.
- D4 is capped (`burst_n ≤ 8`/call, ~2 calls/min → low-tens/min ceiling,
  `background_services.py`; memory `feedback_d4_throughput_ceiling`).
- cptc `target_conn_per_minute_during_active` = 185 (cptc9) / 208 (cptc8).
- ⇒ `volume=FAIL (ratio ≈0.04)` is permanent + expected. The real total
  (`active_opens`, same `[bg-counter]` line) is healthy (~150–170/min on BU).

**PHASE-claim (NOT RUSE-verifiable — route to PHASE):**
- "Therefore cptc scores ~0 / volume doesn't matter for cptc." UNKNOWN here.
  `dataset_realism_keys` flags cptc9 as *genuinely coverage-limited* — so volume/
  coverage may actually matter for cptc, unlike AXES. The BG=FAIL is a measurement
  artifact, NOT evidence about the score.

**The question for PHASE:** does cptc's exp-model score respond to connection
volume/rate, or only to per-connection shape + coverage? Is `target_conn_per_minute
=185/208` a score-relevant target or a vestigial D4-floor knob? Empirically it
resolves on PHASE's next **dredge → re-infer → pass-rate** on the cptc deploys: if
cptc pass-rate moves on the `connection_shape` feedback despite the unchanged
volume gap, volume wasn't the wall; if it stays ~0, volume/coverage is a real cptc
wall (different conclusion than AXES). Carry-forward memory: `project_service_mix_targets`
(cptc structural skew, service-mix era — partially superseded by the shape contract).

**Tooling note:** the BG column could read `active_opens` (true total, on the same
line) instead of the D4-only `conns=` floor — a ~1-line `audit.py` change that
would stop the column crying wolf on cptc. HOLD it until PHASE answers the volume
question: if volume IS score-relevant for cptc, you want the column honest about
total rate, not hidden.

## 7. Open questions to route to PHASE

- **cptc volume sensitivity** (§6) — blocks deciding whether cptc BG=FAIL is
  cosmetic or real. Highest priority.
- **conn_state_mix REJ split** — RUSE fires S0/REJ to a drop host; REJ-vs-S0 split
  needs the build-#6 RST responder. Confirm PHASE's `failed_conn` fold is the
  durable contract until then.
- **resp_bytes/orig_pkts datasets** — won't move until builds #2/#3 land; PHASE
  should expect no movement on spring25(resp_pkts)/fall24/year(orig_pkts) until then.
- **Shape-floor cap tuning** — the failed_conn bucket cap (120) + startup-interval
  drop are conservative defaults; tune against measured `failed_conn` fraction once
  Zeek data returns.

## 8. PHASE source docs (read-only, ~/PHASE/feedback_engine/knob_investigation/)

- `RUSE_connection_shape_spec.md` — the consumer contract + the RUSE-verification
  appendix (field→code map, spec corrections, phased plan).
- `PHASE_realism_feedback_guide.md` — producer side (what PHASE emits + why).
- `dataset_realism_keys.md` — per-dataset realism key table + taxonomy + scripts.

NEVER edit `~/PHASE/` (HARD RULE). RUSE does the read-side; tell PHASE what
producer change is needed.

## Related

- Deploy mechanics + behavior.json plumbing: `/decoy-deploy`
- Audit columns + benign-state semantics: `/decoy-audit`
- Runtime call-graph (controller, channels, ledger): `/decoy-mechanical-systems`
- Memories: `project_connection_shape_controller`, `feedback_d4_throughput_ceiling`,
  `project_service_mix_targets`, `project_tier_plans_exp1`,
  `project_persistent_session_daemon`, `project_decoy_brain_logging_honest`.
