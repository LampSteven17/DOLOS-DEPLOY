# Deploy System Audit — 2026-04-20

Point-in-time audit prompted by a live incident: experiments.json showed
stale IPs for every active RUSE + GHOSTS deploy even though session logs
reported "Registered in PHASE" for each. Root cause: `/mnt/AXES2U1` NFS
mount likely blipped between deploy-time write and visibility check, so
atomic-rename'd data never actually persisted. Session logs passed; the
file state did not.

Scope: the three spinup modules (`spinup.py` / `ghosts.py` / `rampart.py`),
the teardown / list / audit commands, the shared libs
(`register_experiment.py`, `vm_naming.py`, `enterprise_ssh_config.py`),
and the Ansible playbooks.

**Design constraint respected in this audit:** the three deployment types
are kept **separate** by policy. Duplication across spinup modules is
noted but **not refactored**. Only the shared libs and per-system fail-
loud gaps are candidates for change.

## Silent-failure gaps found

### 1. PHASE registration wasn't mount-blip-safe — **FIXED**
`register_experiment.py` did an atomic read → modify → rename under
`fcntl.LOCK_EX`. Rename succeeded, script returned rc=0, caller printed
"Registered in PHASE", but on an NFS mount that briefly disconnected
the rename can land on a soon-to-vanish dentry. No post-write check
existed → silent data loss.

**Fix:** after the lock is released, re-read `experiments.json` and
assert the entry exists and contains every IP we intended to write. If
missing → rc=1 with a specific error. Caller (spinup.py's fail-loud
P1 path, likewise ghosts.py + rampart.py) already aborts the deploy on
rc=1.

### 2. Neighborhood sidecar IPs were never registered — **FIXED**
`spinup.py::_provision_and_install_neighborhood` writes
`neighborhood-inventory.ini` with the sidecar VM's host+IP, but
`_register_phase_subprocess` only passed the main `ssh_config_snippet.txt`
(which contains only SUPs). Result: PHASE and downstream anon pipelines
never saw the sidecar. Today required manual `--extra-ip` patching via
ad-hoc Python to register 7 neighborhoods retroactively.

**Fix:** new `--extra-ip IP=HOSTNAME` (repeatable) flag on
`register_experiment.py`; spinup.py's register path now reads
`neighborhood-inventory.ini` and appends one `--extra-ip` per row.
Baseline + non-topology-gated deploys have no neighborhood inventory
and no-op through the new path.

### 3. `end_date` bled across re-deploys — **FIXED** (earlier commit `29331b6`)
`existing["end_date"] = None` was gated on `if "end_date" not in existing`
which never fires after the first teardown. Re-deploy on the same
`name` inherited the prior teardown's end_date. Caused 6 inverted
ranges in experiments.json (end before start). Now unconditional.

### 4. Teardown used `today` for end_date — **FIXED** (earlier commit `29331b6`)
Teardown-day Zeek captures are partial. Now uses yesterday for a clean
last-full-day boundary.

## Gaps noted but deferred

### D1. Broad `except Exception:` in 6 places
`rampart.py:243`, `rampart.py:338`, `rampart.py:487`, `rampart.py:800`,
`ghosts.py:522`, `ghosts.py:542`, `spinup.py:417`, `audit.py:369`,
`audit.py:438`, `audit.py:458`, `teardown.py:72`, `list_cmd.py:43`,
`__main__.py:312`, `register_experiment.py:63`.

Each swallows all exception types including `KeyboardInterrupt`
pre-Python-3.8-semantics or unexpected `OSError`. Most already log the
exception type + message via `{type(e).__name__}: {e}`, which is
acceptable — the risk is the few that don't.

**Action:** leave alone for now, revisit once the three spinup modules
get touched for other reasons. Low ROI to edit each independently.

### D2. `failed_when: false` / `ignore_errors: yes` in teardown playbooks
`teardown.yaml` and `teardown-all.yaml` use these liberally on volume
deletes. Intentional: best-effort cleanup, don't let one volume-delete
failure block the rest of a 40-VM teardown. Each still logs via the
Ansible stream parser, so a human sees the failures. Not a silent
failure pattern.

### D3. Duplication across spinup modules
`_make_dep_id`, `_register_phase`, `_register_phase_subprocess`,
`_test_ssh_all`, `_safe_rmtree`, `_parse_inventory` each live in 2–4
separate copies. `make_dep_id` in particular is in 4 places
(`list_cmd.py`, `rampart.py`, `ghosts.py`, `teardown.py`) plus
`_make_run_dep_id` in `spinup.py` as a near-duplicate.

**Deliberately not refactored** per the "keep systems separate" constraint.
Shared `lib/vm_naming.py` already owns the canonical VM-naming primitives
and each duplicate should ideally call those, but forcing that today
would touch every module — too much blast radius for a live system.
Flag for a dedicated refactor session.

### D4. Audit doesn't probe neighborhood VMs
`audit.py` excludes `-neighborhood-0` from the orphan check (correct)
but never SSH-probes them. A dead neighborhood daemon wouldn't show up
in `./audit`. Today the neighborhood sidecars turned out to be healthy,
but the check is a blindspot.

**Action:** separate task. Add a `--neighborhoods` audit flag or a new
"Neighbor" column to the summary. Small addition; defer so this session
stays tight.

### D5. `register_experiment.py` and `_close_phase_experiment` have
separate fcntl lock implementations
Both take `experiments.json.lock` correctly, but the code is copied.
Extract to a single helper once other register_experiment.py changes
settle.

## Coverage confirmations (not bugs)

The existing fail-loud posture is solid where it's been applied:

- `spinup.py`: SSH threshold 90%, install-sups rc-check, install-recap
  parsing, register fail-loud (P1)
- `ghosts.py`: API-install rc-check (G1), clients rc-check (G2), SSH
  threshold (G3), ACTIVE threshold + IP audit (G4), timeline coverage
  (G6), register fail-loud (P1)
- `rampart.py`: Windows emulation 90% threshold (C2), `_check_step_results`
  after every parallel batch, install-rampart-emulation NRestarts assertion,
  register fail-loud (P1)
- Playbooks: `set -euo pipefail` on Docker/dotnet shells, explicit asserts
  replacing `ignore_errors: yes` on readiness checks

## Summary

| Finding | Risk | Status |
|---|---|---|
| register_experiment.py no post-write verify | **high** (silent data loss on mount blip) | fixed |
| Neighborhood IPs not registered | **medium** (PHASE blindspot) | fixed |
| end_date stale on re-deploy | medium | fixed (earlier commit) |
| Teardown uses today not yesterday | low | fixed (earlier commit) |
| Broad `except Exception` in 14 places | low–medium | deferred |
| Duplication across spinup modules | design debt | deferred per constraint |
| Audit doesn't probe neighborhoods | medium | deferred, separate task |
