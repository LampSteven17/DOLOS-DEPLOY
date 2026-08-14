"""Per-workflow execution-rate governor (PHASE `workflow_budget`, 2026-08-14).

PHASE's task-value engine emits, on each ACTIVE `content.schedule` block:

    "workflow_budget": {
      "target_conns_per_hour": 57.0,
      "target_execs_per_hour": {"BrowseWeb": 0.0, "BrowseYouTube": 0.001, ...},
      "cadence_cap_per_hour": 35,
      ...
    }

This is the connection-budget translation: `workflow_weights` say what MIX to
run, `target_execs_per_hour` says HOW OFTEN. PHASE solves the rates from each
workflow's measured per-execution connection cost so total connection spend
lands on `target_conns_per_hour`. Pacing on weights alone is what produced the
measured overshoot on the axes fleets (target 6.3 conn/min vs 53 achieved) —
weights control the mix but nothing controlled the rate.

Mechanism: one wall-clock token bucket per workflow, plus a global cadence
bucket for `cadence_cap_per_hour`. A workflow is eligible only while it holds
at least one whole credit; running it spends one from both buckets. Credits
accrue continuously at rate/3600 per second, so 0.001/hr genuinely means
"essentially never" and 17/hr means "roughly every 3.5 minutes" — without the
caller needing to know its own cadence.

Cadence-independent by construction: credits accrue on wall time, not on loop
ticks, so a slow BrowserUse cluster and a fast MCHP one converge to the same
executions/hour. Same reasoning as the failed_conn token bucket in
`network/scripted_services.py`.

ADDITIVE: absent `workflow_budget` leaves the governor inactive and selection
behaves exactly as before.
"""

from time import monotonic
from typing import Dict, Iterable, Optional, Set

# How much unspent budget a bucket may bank, expressed in hours of accrual.
# Credits accrue during inter-cluster sleeps and long LLM workflows; without
# any banking a slow brain would under-run its rate, and with unbounded
# banking a SUP idle for hours would dump a whole day's executions at once.
# 15 minutes is enough to ride out a normal cluster gap.
_BURST_HOURS = 0.25


class ExecGovernor:
    """Wall-clock token-bucket rate limiter over workflow executions.

    Inactive (`active` False) until `update_budget` is handed a budget with a
    usable `target_execs_per_hour` map — callers must treat inactive as
    "no opinion" and select workflows exactly as they did pre-budget.
    """

    def __init__(self, logger=None):
        self._logger = logger
        self._rates: Dict[str, float] = {}
        self._credits: Dict[str, float] = {}
        self._cap_rate: Optional[float] = None
        self._cap_credits: float = 0.0
        self._last_refill: float = monotonic()
        self._active: bool = False
        # Identity of the budget currently loaded, so a per-tick update from
        # the same schedule block doesn't reset credits (which would let the
        # bucket refill from empty every tick and defeat the pacing).
        self._budget_key = None

    @property
    def active(self) -> bool:
        return self._active

    def update_budget(self, budget: Optional[dict]) -> None:
        """Load a `workflow_budget` block. Safe to call every tick.

        Credits survive an unchanged budget; a changed budget (new schedule
        block) starts each bucket with one credit so a workflow can fire
        promptly on entering a new block rather than waiting a full period.
        """
        rates = (budget or {}).get("target_execs_per_hour") or {}
        if not isinstance(rates, dict) or not rates:
            if self._active:
                self._info("[exec-budget] no target_execs_per_hour — governor off")
            self._active = False
            self._budget_key = None
            return

        try:
            parsed = {str(k): max(0.0, float(v)) for k, v in rates.items()}
        except (TypeError, ValueError):
            self._warn(f"[exec-budget] malformed target_execs_per_hour: {rates!r} "
                       f"— governor off (weights-only selection)")
            self._active = False
            self._budget_key = None
            return

        cap = (budget or {}).get("cadence_cap_per_hour")
        try:
            cap_rate = float(cap) if cap is not None else None
        except (TypeError, ValueError):
            cap_rate = None

        key = (tuple(sorted(parsed.items())), cap_rate)
        if key == self._budget_key and self._active:
            return  # same block — keep accrued credits

        self._budget_key = key
        self._rates = parsed
        self._cap_rate = cap_rate
        # Seed one credit per bucket so entering a block isn't dead time.
        self._credits = {name: 1.0 if rate > 0 else 0.0
                         for name, rate in parsed.items()}
        self._cap_credits = 1.0 if (cap_rate is None or cap_rate > 0) else 0.0
        self._last_refill = monotonic()
        self._active = True
        self._info(
            f"[exec-budget] governor ON — "
            f"{sum(parsed.values()):.3f} execs/hr across {len(parsed)} workflows"
            + (f", cadence cap {cap_rate:g}/hr" if cap_rate is not None else ""))

    def _refill(self) -> None:
        now = monotonic()
        elapsed_h = max(0.0, now - self._last_refill) / 3600.0
        self._last_refill = now
        if elapsed_h <= 0:
            return
        for name, rate in self._rates.items():
            ceiling = max(1.0, rate * _BURST_HOURS)
            self._credits[name] = min(ceiling,
                                      self._credits.get(name, 0.0) + rate * elapsed_h)
        if self._cap_rate is not None:
            ceiling = max(1.0, self._cap_rate * _BURST_HOURS)
            self._cap_credits = min(ceiling,
                                    self._cap_credits + self._cap_rate * elapsed_h)

    def eligible(self, names: Iterable[str]) -> Set[str]:
        """Names holding a full credit right now (empty when the cadence cap
        is exhausted). Workflows absent from the budget map are NOT eligible —
        PHASE omitting a workflow from `target_execs_per_hour` means it has no
        budget this block, which is the whole point of the throttle."""
        if not self._active:
            return set(names)
        self._refill()
        if self._cap_rate is not None and self._cap_credits < 1.0:
            return set()
        return {n for n in names if self._credits.get(n, 0.0) >= 1.0}

    def consume(self, name: str) -> None:
        """Spend one credit for `name` (and one from the cadence cap)."""
        if not self._active:
            return
        self._credits[name] = max(0.0, self._credits.get(name, 0.0) - 1.0)
        if self._cap_rate is not None:
            self._cap_credits = max(0.0, self._cap_credits - 1.0)

    def status(self) -> dict:
        """Compact snapshot for logging/diagnostics."""
        return {
            "active": self._active,
            "rates_per_hour": dict(self._rates),
            "credits": {k: round(v, 4) for k, v in self._credits.items()},
            "cadence_cap_per_hour": self._cap_rate,
            "cadence_credits": round(self._cap_credits, 4),
        }

    def _info(self, msg: str) -> None:
        print(msg, flush=True)
        if self._logger:
            try:
                self._logger.info(msg)
            except Exception:
                pass

    def _warn(self, msg: str) -> None:
        print(f"[WARNING] {msg}", flush=True)
        if self._logger:
            try:
                self._logger.warning(msg)
            except Exception:
                pass
