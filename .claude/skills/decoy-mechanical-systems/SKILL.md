---
name: decoy-mechanical-systems
description: DECOY runtime mechanics — how a deployed SUP actually runs, as a maintainer code-map. Traces the call-graph from `python -m sup <KEY>` (`decoys/sup/__main__.py`) → config registry (`runners/run_config.py`) → brain dispatch → the shared cluster engine (`common/emulation_loop.py::BaseEmulationLoop`) → a single workflow action. Covers the three brains (MCHP scripted-Selenium, BrowserUse Playwright+Ollama, SmolAgents CodeAgent+Ollama) + the controls floor, the 10 MCHP workflows, calibrated timing (`common/timing/phase_timing.py`), the behavior.json read-side contract (`common/behavioral_config.py`), the between-workflow traffic channels (D3 scripted / D4 background / persistent-session daemon / conn sampler under `common/`), JSON-Lines logging (`common/logging/agent_logger.py`), and LLM content augmentation (`augmentations/`). Use when modifying SUP runtime behavior (add a workflow, change a timing/diversity knob, add a traffic channel or behavior.json field) and you need to know where the code is and what reads each knob. Does NOT cover deployment/spinup (see /decoy-deploy) or health auditing (see /decoy-audit); PHASE-side feedback generation is read-only upstream (~/PHASE/).
type: skill
---

# decoy-mechanical-systems

The third per-system skill: **operate** (`/decoy-deploy`) → **verify**
(`/decoy-audit`) → **understand/modify** (this). A maintainer code-map for the
`decoys/` runtime — the code that turns a deployed `behavior.json` into traffic
on the wire and actions on screen. All paths below are relative to `decoys/`.
Line anchors verified 2026-06-15; prefer the named symbol over the number when
they drift.

## Mental model

A SUP is a deterministic, cluster-based emulation loop. Each cluster boundary it
hot-reloads `behavior.json`, (re)builds calibrated timing, gates on UTC
active-minute windows, then runs a burst of workflows — each selected by
per-hour schedule / flat weights / diversity rotation, executed by a **brain**
(scripted MCHP, or LLM-driven BrowserUse / SmolAgents), and logged event-by-event
as JSON-Lines. Between workflows, brain-independent **traffic channels** fire
(D4 background DNS/HTTP/NTP, D3 scripted protocol probes, a persistent-TLS
daemon). `mode: controls` SUPs bypass all of this for a pure-`requests` floor.

The whole thing is one shared engine — `common/emulation_loop.py::BaseEmulationLoop`
— with four abstract hooks each brain fills in. Read that file first; everything
else is a knob it reads or a callee it dispatches to.

## 1. Boot path (entry → brain dispatch)

`python -m sup <KEY>` → `sup/__main__.py::main` (`:24`):
- Arg parse (`:43` `--brain`, `:51` `--seed` default 42, `0`=nondeterministic).
  Config key resolves via `runners/get_config` (`:83`) or `build_config` from
  `--brain/--model/--calibration` (`:92`); default `M1` (`:100`).
- **Seed is resolved before `random.seed()`** (`:105-122`): `resolve_behavioral_config_dir`
  + `peek_seed` read `_metadata.seed` from behavior.json; a PHASE seed overrides
  the CLI default so every (config,dataset) pair is stably distinct. The chosen
  seed then seeds `random` **and** `os.environ["SUP_OLLAMA_SEED"]` (`:119-122`) —
  which downstream makes the AgentLogger session_id, Ollama generation, and loop
  RNG all reproducible.
- Brain dispatch (`:132-140`): `mchp`→`run_mchp`, `browseruse`→`run_browseruse`,
  `smolagents`→`run_smolagents`. (`run_m0.py` is the read-only MITRE control.)

`runners/run_config.py` — the registry: `SUPConfig` dataclass (`:38`),
`config_key` canonicalizer property (`:55`), `CONFIGS` dict (`:78`), deprecated
`_ALIASES` (`:137`, e.g. `M1a.llama`→`M1`), `get_config` (`:196`), `build_config`
(`:223`). Naming scheme `[Brain][Version].[Model]` is in CLAUDE.md.

**Controls bypass:** `run_mchp` (`run_mchp.py:22`) loads behavior.json and, when
`fc.mode == MODE_CONTROLS` (`:52`), calls `run_controls` instead of building an
MCHPAgent — no brain, no LLM, no emulation loop.

## 2. The engine — `common/emulation_loop.py`

`BaseEmulationLoop` (`:25`) is shared by all three brain loops. Four abstract
hooks each brain implements: `_load_workflows` (`:90`), `_execute_workflow`
(`:95`), `_apply_brain_specific_config` (`:100`), `_agent_type_label` (`:105`).

**Lifecycle** — `run` (`:676`): seed RNG → `_load_workflows()` → first
`_reload_behavioral_config()` → `_emulation_loop()`. `stop` (`:717`) FIN-closes
the persistent-session thread first (→ Zeek `conn_state=SF`) then `cleanup()`s
each workflow. SIGINT/SIGTERM wired at `:695-696`.

**Per-cluster sequence** — `_emulation_loop` (`:518`):
1. `_reload_behavioral_config()` (`:521`) — hot-swap (see below).
2. `_window_gate_sleep_then_continue()` (`:527`) — if outside an active window
   sleep until next start (capped 30min, `_WINDOW_GATE_SLEEP_CAP_S:459`); if
   inside but `<90s` usable (`_START_ONLY_FLOOR_S:465`) sleep through the end;
   else set `_cluster_deadline_ts` and fall through.
3. Push D4 window-state (`:535-547`), netting out the persistent daemon's
   per-minute opens so total traffic targets — not stacks past —
   `target_conn_per_minute_during_active`.
4. `cluster_size = _get_cluster_size()` (`:559`); reset D2 per-cluster tracking.
5. For each task in the cluster (`:573`): soft-fence check → `_get_task_delay()`
   sleep (`:587`) → re-check fence → `_background_svc.maybe_generate()` (`:604`)
   → `_scripted_svc.maybe_run()` (`:610`) → schedule-OFF gate (`:617`) →
   `_select_workflow()` (`:626`) → log decision/workflow_start →
   `_execute_workflow()` (`:661`) → on success `record_activity()`.
6. `group_delay = _get_cluster_delay()` (`:669`) → sleep.

**Timing helpers** (`:121-137`) branch on `self._phase_timing`: if a
`CalibratedTiming` is set, defer to it; else baseline `random.randint/randrange`
over the constructor's `cluster_size/task_interval/group_interval`.

**Hot-swap reload** — `_reload_behavioral_config` (`:141`): loads behavior.json
(fail-loud, `:156`), stashes `mode`/`_volume_target`; for `MODE_FEEDBACK` builds
`_workflow_weights` (`:181`) and parses `content.schedule` into `_schedule_by_hour`
(`:189`); rebuilds `CalibratedTiming` from `fc.timing_profile` **with no
try/except** (`:202-220` — a malformed schema must fail loud, since swallowing it
silently disabled both the window gate and D4); wires D4 / scripted / persistent
services from `fc.diversity_injection` (`:230-258`); pushes the window contract to
timing (`:261-267`); prints `[INFO]` section-absent status lines (`:285-321` —
absent sections are canonical under the two-shapes contract, NOT warnings).

**Workflow selection** (`:323-452`):
- `_build_schedule_by_hour` (`:325`) — 24-elem per-hour weights from
  `content.schedule`; **empty `{}`/zero-sum block → `[]` OFF sentinel** (`:363`),
  NOT an error; FATAL if any hour 0-23 uncovered (`:368`).
- `_current_workflow_weights` (`:378`) — schedule (current UTC hour) wins, else
  flat `_workflow_weights`, else `None`.
- `_schedule_off_for_now` (`:391`) — true when this UTC hour's sentinel is `[]`;
  the loop then skips workflow execution but D4/scripted traffic still fires.
- `_select_workflow` (`:405`) → `_select_workflow_with_rotation` (`:414`) when
  `diversity_injection` present: D2 knobs `max_consecutive_same` (0.1× penalty,
  `:430`) and `min_distinct_per_cluster` (0.01× near cluster end, `:438`).

## 3. The three brains + controls (the hooks)

| Brain | Files | Engine it drives | LLM |
|---|---|---|---|
| MCHP | `brains/mchp/agent.py` (`MCHPAgent:47`) | Selenium-Firefox + pyautogui, **scripted** | none in the brain (uses augmentations for content) |
| BrowserUse | `brains/browseruse/{agent.py,loop.py}` (`BrowserUseLoop:23`) | Playwright via `browser-use==0.12.7` | Ollama via `create_logged_chat_ollama` (`agent.py:177`) |
| SmolAgents | `brains/smolagents/{agent.py,loop.py}` (`SmolAgentLoop:20`) | `smolagents==1.25.0` CodeAgent + DuckDuckGo | LiteLLM→Ollama (`agent.py:22,71`) |
| controls | `brains/controls/runner.py` (`run_controls:128`) | pure `requests` HTTP floor | none |

- **MCHP** `_load_workflows` (`:90`) dynamic-imports `app/workflows/*.py`, applying
  `WINDOWS_ONLY_WORKFLOWS` (`:32`, excludes `ms_paint.py` off-Windows) and
  `BEHAVIOR_GATED_WORKFLOWS` (`:41`, `download_files`→`enable_download`,
  `whois_lookup`→`enable_whois`). `_execute_workflow` (`:137`) calls
  `workflow.action(extra, logger)`. `_apply_brain_specific_config` (`:149`)
  injects PHASE pools onto workflow objects (`:159-171`) and BrowseWeb modifiers
  `page_dwell` (`:192`), `navigation_clicks` (`:204`), `keep_alive_probability`
  (`:215`). Firefox autoplay prefs (so BrowseYouTube actually streams) in
  `app/utility/webdriver_helper.py:42-43`.
- **BrowserUse / SmolAgents** loops load native workflows via
  `workflows/loader.py::load_workflows` (`:13`) — modules `browse_web`,
  `browse_youtube`, `download_files`, `web_search`, `whois_lookup`.
  `_execute_workflow` runs the LLM agent and parses steps from `browser_use`'s
  `AgentHistoryList` (BU `agent.py:118` `_log_bu_steps`) / smolagents step
  callbacks. SmolAgents alone consumes `content.site_categories` (W3, `loop.py:144`).
- **Controls** `run_controls` (`:128`): round-robins `google_search_pool` then
  `browse_url_pool` (`_fetch_search:97`, `_fetch_browse:112`) on a fixed
  `page_fetch_interval_seconds` (default 30, `:140`), fixed Chrome UA (`:50`).

Cross-refs (memory): `project_brain_lib_pin_parser_coupling` (the lib pins —
bump silently breaks step parsing), `project_decoy_brain_logging_honest` (step
counts are real executed actions; on-wire BU ≫ MCHP ≫ Smol/min),
`project_youtube_streaming_fix`.

## 4. MCHP workflows (the unit of work) — `brains/mchp/app/workflows/`

`BaseWorkflow` (`app/utility/base_workflow.py:9`): `__init__(name, description,
driver)` sets `self.name`/`self.description` (`:20-21`); abstract
`action(extra, logger)` (`:25`); `cleanup` (`:35`); `display` property (`:12`).
Each module sets module-level `WORKFLOW_NAME`/`WORKFLOW_DESCRIPTION`, exports a
`load()` factory, and logs via `logger.step_start/step_success/step_error(name,
category=...)`. The logged `workflow` field is `WORKFLOW_NAME` (joins to PHASE
weight keys); the human description goes in params.

| Module | `WORKFLOW_NAME` | category | PHASE pool / gate | data file |
|---|---|---|---|---|
| `browse_web.py` | `BrowseWeb` | browser | `browse_url_pool` | `data/websites.txt` (999) |
| `google_search.py` | `WebSearch` | browser | `google_search_pool` | `data/google_searches.txt` (28) |
| `browse_youtube.py` | `BrowseYouTube` | video | `youtube_video_pool` | `data/browse_youtube.txt` (3) |
| `download_files.py` | `DownloadFiles` | browser | gate `enable_download` | xkcd/wiki/NIST (direct HTTP) |
| `whois_lookup.py` | `WhoisLookup` | browser | `whois_domain_pool`, gate `enable_whois` | TCP/43 |
| `execute_command.py` | `ExecuteCommand` | shell | `extra` cmd list | — |
| `spawn_shell.py` | `ListFiles` | shell | — | — |
| `open_office_calc.py` | `SpreadsheetEditor` | office | — | pyautogui → LibreOffice Calc |
| `open_office_writer.py` | `DocumentEditor` | office | — | pyautogui → LibreOffice Writer |
| `ms_paint.py` | `MicrosoftPaint` | office | WINDOWS_ONLY (excluded on Linux) | — |

`browse_youtube` quirks (recent commits): direct-pool path guards dead/private
videos via the oEmbed check (`common/network/youtube.py`); a 0-result suggested
sidebar after the full lazy-load wait logs **INFO, not WARNING** (transient, not a
dead-video signal). Full jsonl schema reference: see `/decoy-deploy` (don't
duplicate). Cross-ref memory `project_jsonl_log_schema`.

## 5. Timing — `common/timing/phase_timing.py` + `profiles/`

Two timing systems live here — don't confuse them:
- **Preset** path: `PhaseTimingConfig` (`:28`) / `PhaseTiming` (`:87`) /
  `get_preset_config` (`:323`) — older named presets.
- **Calibrated** path (what feedback uses): `CalibratedTimingConfig` (`:347`) /
  `CalibratedTiming` (`:356`), loaded by `load_calibration_profile` (`:692`) or
  built from behavior.json by `build_calibrated_timing_config`.

`CalibratedTiming` outputs (sampled, not hardcoded): `get_cluster_size` (`:465`),
`get_task_delay` (`:482`), `get_cluster_delay` (`:489`) — all via
`_sample_percentile` interpolation (`:441`) scaled by `_get_hourly_scale`
(`:460`). Window contract: `update_window_contract` (`:642`), `has_windows`
(`:553`), `current_window` (`:557`) — **UTC minute-of-day, half-open `[start,
end)`**, with a `hard_fence_seconds` no-new-workflow zone at the tail. Profiles in
`profiles/{summer24,fall24,spring25}_profile.json`.

**UTC hour-of-day contract** (CLAUDE.md 2026-05-06): all hour reads are
`datetime.now(timezone.utc).hour`. PHASE write-side and these consumers must move
together or behavior fires 4-5h offset. Cross-ref `project_mandatory_behavior_json`.

## 6. behavior.json — the read side — `common/behavioral_config.py`

`BehavioralConfig` dataclass (`:41`); modes `MODE_FEEDBACK` (`:36`, full schema)
vs `MODE_CONTROLS` (`:37`, floor). Field groups: `workflow_weights`, `schedule`,
`behavior_modifiers`, `site_config`, `prompt_augmentation`, `timing_profile`,
`variance_injection`, `diversity_injection`, plus the five pools
(`browse_url_pool`, `google_search_pool`, `youtube_video_pool`,
`whois_domain_pool`, `download_url_pool`).

- `load_behavioral_config` (`:307`) — **3-layer fail-loud**: missing behavior.json
  `raise RuntimeError` (`:351`) → service crash-loops; bad/absent `_metadata.mode`
  `raise RuntimeError` (`:396`). (Deploy-time + install-time are the other two
  layers — see CLAUDE.md "Deploy fail-loud contract".)
- `resolve_behavioral_config_dir` (`:184`): override → `RUSE_BEHAVIOR_CONFIG_DIR`
  (`:205`) → prod `/opt/ruse/deployed_sups/<key>/behavioral_configurations/`
  (`:213`) → dev `<root>/deployed_sups/...` (`:221`).
- `peek_seed` (`:254`), `apply_phase_seed` (`:226`), `build_workflow_weights`
  (`:472`), `build_calibrated_timing_config` (`:501`).

This is the consumer of exactly what `/decoy-deploy` plumbs onto each VM.

## 7. Between-workflow traffic channels (brain-independent)

Fire from inside the cluster loop (or their own thread), independent of which
brain/workflow runs:
- **D4 background** `common/background_services.py`: `BackgroundServiceGenerator`
  (`:52`), `set_window_state` (`:156`), `maybe_generate` (`:177`). DNS from
  `BACKGROUND_DOMAINS` (`:22`), HTTP-HEAD from `BACKGROUND_URLS` (`:37`), rare NTP.
  Deficit-burst tops up to `volume_target`, capped `burst_n ≤ 8` per call
  (`:234`) — the throughput ceiling (memory `feedback_d4_throughput_ceiling`).
- **Persistent-session daemon** `common/network/persistent_session.py`:
  `PersistentSessionDaemon` (`:96`), `start` (`:198`) / `stop` (`:212`),
  `opens_in_current_minute` (`:223`, read by the main loop for D4 net-out),
  `_resolve` resolve-once IP cache → zero-DNS steady state (`:440`),
  `_build_request` keepalive padding toward `orig_bytes_per_session` (`:375`).
  `set_controller` (Phase 1) lets the ShapeController override per-conn
  `orig_bytes`/`duration` sampling; `_open_session` asks the controller first,
  `_close_session` reports `(bytes_cum, wall-duration, "SF")` into its ledger.
  Own thread so it survives inter-window sleeps. Ships dormant until PHASE emits
  an enabled block (memory `project_persistent_session_daemon`).
- **Closed-loop ShapeController** `common/network/shape_controller.py`:
  `ShapeController`, `sample_orig_bytes`/`sample_duration` (per-conn draw from the
  `connection_shape` percentiles × a bounded bias), `observe_connection`
  (emit-side ledger), `maybe_tick` (minute-roll: correct bias from measured-vs-
  target p50, recompute `failed_conn_rate_per_min` = frac × own-sampler
  active_opens). Built in `_reload_behavioral_config` when PHASE ships
  `connection_shape`(enabled)/`conn_state_mix`; injected into the persistent +
  scripted channels. Actuates orig_bytes/duration + failed_conn TODAY; orig_pkts
  (#3) / resp_* (#2) parsed-not-actuated. The measurement source is the emit-side
  ledger, NOT `conn_sampler` per-conn (RUSE spec §B.1). Now also computes the
  shape-floor coverage deficit (`floor_opens_target_per_min`) + a per-minute
  `note_workflow` completion ratio + estimated aggregate p50s, all in the `[shape]`
  log. Memory `project_connection_shape_controller`.
- **Universal shape-floor channel** `common/network/shape_floor.py` (Build #5,
  2026-06-25): `ShapeFloorDaemon` — own-thread twin of the persistent daemon, but
  the open RATE is coverage-driven (`controller.floor_opens_target_per_min()`,
  `T=0.55` shaped-share, recalibrated from 0.82 on 2026-06-29) instead of a fixed schedule, and it samples the FULL
  `connection_shape` distribution (not just the p75–p90 tail). Opens synthetic
  shaped TLS fillers → holds for the sampled duration → FIN→SF → reports to the
  controller's emit-side ledger as channel `"floor"` (the "2nd emit-side reporter").
  Closes the ~30%→majority coverage gap that held the aggregate median below target
  (duration is the binding feature). Built in `_reload_behavioral_config` when
  `connection_shape.enabled`; reuses the `persistent_sessions` endpoint_pool; opens
  net out of D4. Ships dormant until PHASE emits the block; controls never get it.
  **Two bounds added 2026-08-14 (idle-floor throttle):** (1) **window gate** — the
  daemon self-gates on UTC minute-of-day from `timing.active_minute_windows`
  (`_in_window`, config key `active_minute_windows` passed in `floor_cfg`). It
  previously ran 24/7, the only always-on channel with neither a window gate nor a
  rate ceiling, so every off-window minute was pure idle-floor spend with no shape
  benefit. Open conns are never force-killed at window close — they expire into a
  graceful FIN (`SF`); the gate stops NEW opens only. Empty windows → ungated
  (prior behavior). (2) **budget cap** — `ShapeController.set_conn_budget_per_min()`
  clamps `_floor_target` to `max(0, budget − active_opens)`. Coverage alone is
  unbounded AND self-inflating (the `unshaped` term derives from the global
  ActiveOpens delta, which counts the floor's own opens; at idle there's no workflow
  traffic to offset it). `[shape]` log gained `floor_cap=` + `budget=`.
- **Exec-rate governor** `common/exec_governor.py` (PHASE task-value engine,
  2026-08-14): `ExecGovernor` — wall-clock token buckets pacing workflow EXECUTIONS
  to `content.schedule[*].workflow_budget.target_execs_per_hour` plus a global
  `cadence_cap_per_hour` bucket. Not a traffic channel itself; it throttles the
  largest traffic SOURCE (workflow fan-out). Wired in `_reload_behavioral_config`
  (budget parsed by `_build_budget_by_hour`, non-fatal/additive) and gated in the
  cluster loop via `_exec_budget_blocked()`; ineligible workflows are masked to
  weight 0 in `_current_workflow_weights` — the single choke point both selection
  paths read. Credits accrue on wall time, not loop ticks, so slow-BU and fast-MCHP
  converge to the same execs/hour. Absent budget → inactive → selection unchanged.
- **D3 scripted probes** `common/network/scripted_services.py`:
  `ScriptedServiceScheduler` (`:193`), `maybe_run` (`:231`) — smb/ldap/imap/doh/
  mdns/failed_conn on a cron-style schedule, in-window catch-up
  (memory `project_scripted_services_in_window_catchup`). `set_controller` +
  `_maybe_fire_failed_conn_rate` add the Phase-1 target-driven failed_conn rate
  (bypasses the cron `failed_conn` slot when `conn_state_mix` ships a target).
- **Real-traffic sampler** `common/network/conn_sampler.py`: `OutboundConnSampler`
  (`:33`), `sample` (`:97`) reads `/proc` (Tcp ActiveOpens + distinct peers);
  D4 logs it via `logger.network_sample` on each minute roll. NOTE: this reads a
  COUNT, not a per-connection byte/pkt/duration distribution — shape measurement
  is emit-side (ShapeController ledger), not `/proc`.

## 8. Logging — `common/logging/agent_logger.py`

`AgentLogger` (`:104`): deterministic `session_id` from `SUP_OLLAMA_SEED`
(`:127-140`); log dir from `RUSE_LOG_DIR` → `/opt/ruse/deployed_sups/<id>/logs/`
→ dev (`:165-177`). `EventType` enum (`:34`) families: SESSION (`session_start`
`:224` / `session_end` `:234`, success/fail before end), WORKFLOW (`workflow_start`
`:298` / `workflow_end` `:305`), STEP (`step_start` `:422` / `step_success` `:450`
/ `step_error` `:492`), LLM (`llm_request/response/error`), DECISION (`:395`,
`method` ∈ schedule_block/behavior_weighted/random/calibrated), TIMING_DELAY
(`:646`, reason inter_task/inter_cluster), NETWORK_SAMPLE (`:659`, fields
active_opens / distinct_hosts / d4_synthetic). One-shot `session_end` via
shutdown handlers.

## 9. LLM content augmentation — `augmentations/content/llm_content.py`

How MCHP (no LLM *brain*) gets LLM *content*: drop-in `llm_paragraph/sentence/
word/filename/search_query/select/comment/spreadsheet_headers` (`:288-353`).
`LLMContentGenerator` (`:44`) is **no-fallback** — `_query_llm` (`:105`) raises
`LLMUnavailableError` (`:34`) on any failure (experiment validity; CLAUDE.md "No
LLM fallback"). Seeded → `temperature=0.0` for determinism (`:90`). Wire a logger
in via `set_logger` (`:255`).

## 10. Quick reference + "where do I change X?"

| File | Role |
|---|---|
| `sup/__main__.py` | CLI entry, seed resolution, brain dispatch |
| `runners/run_config.py` | `SUPConfig` registry + aliases |
| `common/emulation_loop.py` | the cluster engine (read first) |
| `common/behavioral_config.py` | behavior.json read-side + fail-loud |
| `common/timing/phase_timing.py` | `CalibratedTiming` + window contract |
| `common/logging/agent_logger.py` | JSON-Lines events |
| `common/background_services.py`, `common/network/*` | D3/D4/persistent/sampler |
| `brains/mchp/agent.py` + `app/workflows/` | MCHP brain + scripted workflows |
| `brains/{browseruse,smolagents}/{agent,loop}.py` | LLM brains |
| `augmentations/content/llm_content.py` | LLM content for MCHP |

- **Add an MCHP workflow** → new `brains/mchp/app/workflows/<x>.py` subclassing
  `BaseWorkflow` with `WORKFLOW_NAME` + `load()`; gate it in
  `WINDOWS_ONLY_WORKFLOWS`/`BEHAVIOR_GATED_WORKFLOWS` if needed; add a PHASE
  weight key matching `WORKFLOW_NAME`. (LLM brains: add under each
  `workflows/loader.py`.)
- **Change timing sampling** → `phase_timing.py::CalibratedTiming`
  (get_cluster_size/get_task_delay/get_cluster_delay/_sample_percentile).
- **Add a diversity/traffic channel** → new `common/network/<x>.py`, then wire
  creation + `update_config` into `_reload_behavioral_config`
  (`emulation_loop.py:230-258`) and (if inline) call it in the cluster loop.
- **Add a behavior.json field** → add it to `BehavioralConfig` +
  `load_behavioral_config` (`behavioral_config.py`), then read it in the consumer
  (`_reload_behavioral_config`, a brain's `_apply_brain_specific_config`, or a
  workflow). Keep the fail-loud contract.
- **Change a logged event/field** → `agent_logger.py` `EventType` + the emitter;
  update the jsonl schema notes in `/decoy-deploy` in the same change
  (memory `feedback_keep_skills_memory_in_sync`).

## Sibling skills (template)

This file's skeleton (boot → engine → brains → workflows → timing → config →
traffic → logging → augmentation → where-do-I-change-X) is the template for
`/rampart-mechanical-systems` (pyhuman engine) and `/ghosts-mechanical-systems`
(.NET NPC engine) when those are written. Keep the three plus their deploy/audit
siblings in sync when an engine changes.

For the *why* behind the shape channels (the realism contract, what the
ShapeController actuates vs defers, and the RUSE-fact vs PHASE-model-claim
boundary), see `/feedback-investigation`.
