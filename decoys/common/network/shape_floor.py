"""Universal shape-floor channel (Build #5, 2026-06-25).

PHASE diagnostic (2026-06-25): the exp model scores a SUP-day on the per-connection
orig_bytes/duration SHAPE, and the deployed feedback only steers it part-way (graded
partials ~0.30-0.40) for ONE measured reason — only ~30% of connections are under
shape control. The persistent-session channel hits the target distribution perfectly
(it IS the on-target p75-p90 tail), but ~52% of a SUP's connections are tiny instant
browser-workflow GETs (<=200B, ~0s) that no actuator can touch (unpaddable without
MITM), and they dominate the MEDIAN the model reads (emitted orig_bytes p50 ~128B vs
~1022B human; duration p50 ~0s vs ~14.5s).

This channel closes the coverage gap by emitting SYNTHETIC shaped filler connections
sampled from the SAME diversity.connection_shape distribution (no new PHASE field),
enough of them that the SHAPED connections become a super-majority of the per-conn
mass — which drags the aggregate median up toward the human target. It is a SHAPE
shift, not a volume play: the open count is coverage-driven (computed by the
ShapeController to hit a target shaped-share), and the opens net out of D4 so total
volume stays near target while the mix shifts to ssl.

Duration is the binding constraint (PHASE sim 2026-06-25): because human duration is
far more right-skewed than bytes (p25 0.3s, p50 14.5s), the unshapeable instant GETs
hold the median down until shaped conns are a large super-majority — orig_bytes p50
clears its target near share 0.69 but duration p50 needs ~0.80. Hence the controller's
_FLOOR_SHARE_TARGET = 0.82 (see shape_controller.py).

Mechanically this is the persistent-session daemon's twin (own thread, private RNG,
resolve-once IP cache, padded request to hit orig_bytes, hold for the sampled
duration, FIN->Zeek conn_state=SF, report to the controller's emit-side ledger) with
two differences:
  • OPEN RATE is coverage-driven (controller.floor_opens_target_per_min()), not a
    fixed session_opens_per_hour schedule.
  • it samples the FULL connection_shape distribution (incl. the short connections),
    where persistent-session is only the long-lived tail.

It is the "second emit-side reporter" the shape_controller docstring names. Channel
label on the ledger is "floor".

Correctness invariants (same rules as persistent_session.py):
  - private RNG on this daemon's OWN thread (global random.* would corrupt the
    seed-derived AgentLogger.session_id)
  - absent-safe: no ShapeController / no orig_bytes distribution → no opens (the
    controller returns target 0); enabled-but-broken dist is warned loud by the
    controller, this channel just stays quiet
  - the `max` hard per-connection clamp lives in the controller's sample_*()
  - TCP-TLS only; graceful FIN close
  - opens reported to opens_in_current_minute() for the D4 net-out
"""
from __future__ import annotations

import base64
import random
import socket
import ssl
import threading
import time
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

# Shared with persistent_session's tuning (kept local for channel isolation).
_SEND_INTERVAL_CEILING_S = 10.0
_SEND_INTERVAL_FLOOR_S = 2.0
_MAX_REQUEST_BYTES = 4096
_DRAIN_CAP_BYTES = 32 * 1024
_SOCK_TIMEOUT_S = 5.0
# Coverage-driven opens can run high (T=0.82 → ~4.6× the unshaped rate); bound the
# live socket set well under the 1024 fd default. Excess scheduled opens are skipped
# (logged) when at cap, which self-corrects as sessions reap.
_DEFAULT_MAX_CONCURRENT = 80

_TLS_PORT = 443
_LOG_PREFIX = "[shape-floor]"


class _FloorConn:
    """One held TLS socket sampling the connection_shape distribution."""

    __slots__ = ("sock", "host", "ip", "open_mono", "end_mono",
                 "next_keepalive_mono", "bytes_cum", "bytes_target", "n_requests")

    def __init__(self, sock, host, ip, open_mono, end_mono,
                 first_keepalive_mono, bytes_target):
        self.sock = sock
        self.host = host
        self.ip = ip
        self.open_mono = open_mono
        self.end_mono = end_mono
        self.next_keepalive_mono = first_keepalive_mono
        self.bytes_cum = 0
        self.bytes_target = bytes_target
        self.n_requests = 0


class ShapeFloorDaemon:
    """Background thread emitting coverage-driven shaped filler connections."""

    def __init__(self, config: Optional[dict] = None, logger=None, seed: int = 0):
        self._logger = logger
        # Private RNG — off-main-thread (see module docstring). "FLOR".
        self._rng = random.Random((seed or 0) ^ 0x464C4F52)

        self._lock = threading.Lock()
        self._stop_evt = threading.Event()
        self._thread: Optional[threading.Thread] = None

        self._ip_cache: dict = {}          # resolve-once (zero steady-state dns)
        self._enabled = False
        self._send_interval = _SEND_INTERVAL_CEILING_S
        self._endpoints: list = []         # list of (host, url)
        self._max_concurrent = _DEFAULT_MAX_CONCURRENT
        # Active-window gate (2026-08-14). Half-open [start, end) UTC
        # minute-of-day spans from timing.active_minute_windows. Empty list =
        # ungated (prior 24/7 behavior). Self-gated on the daemon's own 1s
        # thread rather than pushed by the main loop, which sleeps up to 30min
        # between window checks — same idiom as PersistentSessionDaemon's
        # session_opens_per_hour envelope.
        self._windows: list = []

        self._conns: list = []
        self._controller = None            # injected by the loop; owns sampling

        # Per-minute open accounting (D4 net-out) + sub-minute open schedule.
        self._opens_this_minute = 0
        self._minute_stamp = self._utc_minute_key()
        self._pending_opens: list = []     # scheduled monotonic open times
        self._pending_minute = (-1, -1)

        self.update_config(config or {}, seed=seed)

    # ── Public API ────────────────────────────────────────────────────

    def set_controller(self, controller) -> None:
        """Inject (or clear) the ShapeController. Idempotent across reloads."""
        self._controller = controller

    def update_config(self, config: dict, seed: int = 0) -> None:
        """Hot-reload. `enabled` is the loop's gate (connection_shape.enabled).
        `endpoint_pool` is REUSED from the persistent_sessions block — the floor
        needs no new PHASE field."""
        config = config or {}
        self._enabled = bool(config.get("enabled"))

        ka = config.get("keepalive_interval_seconds")
        try:
            ka = float(ka)
        except (TypeError, ValueError):
            ka = _SEND_INTERVAL_CEILING_S
        self._send_interval = max(_SEND_INTERVAL_FLOOR_S,
                                  min(ka, _SEND_INTERVAL_CEILING_S))
        self._max_concurrent = int(config.get("max_concurrent", _DEFAULT_MAX_CONCURRENT))

        windows = []
        for w in (config.get("active_minute_windows") or []):
            try:
                s, e = int(w[0]), int(w[1])
            except (TypeError, ValueError, IndexError):
                continue
            if e > s:
                windows.append((s, e))
        self._windows = windows

        pool = config.get("endpoint_pool") or []
        endpoints = []
        seen = set()
        for url in pool:
            try:
                host = urlparse(url if "://" in url else "https://" + url).hostname
            except Exception:
                host = None
            if host:
                endpoints.append((host, url))
                seen.add(host)
        self._endpoints = endpoints
        for host in list(self._ip_cache.keys()):
            if host not in seen:
                self._ip_cache.pop(host, None)

        if self._enabled and not self._endpoints:
            self._warn("enabled=true but endpoint_pool is empty/unparseable — "
                       "no floor connections will open")

    def start(self) -> None:
        if not self._enabled:
            return
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_evt.clear()
        self._thread = threading.Thread(
            target=self._run, name="shape-floor", daemon=True)
        self._thread.start()
        self._info(f"daemon started send_interval={self._send_interval:.0f}s "
                   f"endpoints={len(self._endpoints)} "
                   f"max_concurrent={self._max_concurrent}")

    def stop(self) -> None:
        self._stop_evt.set()
        t = self._thread
        if t is not None:
            t.join(timeout=5.0)
        with self._lock:
            for c in self._conns:
                self._close_conn(c, reason="shutdown")
            self._conns = []

    def opens_in_current_minute(self) -> int:
        """Opens fired this UTC minute — added to the D4 net-out alongside the
        persistent-session opens. Rolls lazily so a stale count is never returned."""
        with self._lock:
            self._roll_minute_locked()
            return self._opens_this_minute

    # ── Manager thread ────────────────────────────────────────────────

    def _run(self) -> None:
        while not self._stop_evt.is_set():
            try:
                self._tick()
            except Exception as e:
                self._info(f"tick error {type(e).__name__}: {str(e)[:80]}")
            self._stop_evt.wait(1.0)

    def _tick(self) -> None:
        now_mono = time.monotonic()
        now_utc = datetime.now(timezone.utc)
        # Idempotent minute-roll (the persistent daemon may also drive it; this
        # covers the case where only the floor channel is running).
        if self._controller is not None:
            try:
                self._controller.maybe_tick()
            except Exception:
                pass
        with self._lock:
            self._roll_minute_locked(now_utc)
            self._reap_and_keepalive_locked(now_mono)
            self._maybe_open_locked(now_mono, now_utc)

    def _roll_minute_locked(self, now_utc: Optional[datetime] = None) -> None:
        now_utc = now_utc or datetime.now(timezone.utc)
        key = (now_utc.hour, now_utc.minute)
        if key == self._minute_stamp:
            return
        self._minute_stamp = key
        self._opens_this_minute = 0
        # Schedule this minute's coverage-driven opens at random sub-minute offsets.
        n = 0
        if self._controller is not None and self._endpoints and self._in_window(now_utc):
            try:
                n = int(self._controller.floor_opens_target_per_min())
            except Exception:
                n = 0
        n = max(0, min(n, 600))  # absolute backstop on a pathological target
        base = time.monotonic()
        self._pending_opens = sorted(
            base + self._rng.uniform(0.0, 58.0) for _ in range(n))
        self._pending_minute = key

    def _in_window(self, now_utc: datetime) -> bool:
        """True when the current UTC minute-of-day falls in an active window.

        No windows configured → True (ungated, prior behavior). Open conns are
        never force-killed on a window close; _reap_and_keepalive_locked lets
        them expire into a graceful FIN (Zeek conn_state=SF), so the gate only
        stops NEW opens.
        """
        if not self._windows:
            return True
        minute_of_day = now_utc.hour * 60 + now_utc.minute
        return any(s <= minute_of_day < e for s, e in self._windows)

    def _reap_and_keepalive_locked(self, now_mono: float) -> None:
        survivors = []
        for c in self._conns:
            if now_mono >= c.end_mono:
                self._close_conn(c, reason="duration")
                continue
            if now_mono >= c.next_keepalive_mono:
                if not self._keepalive(c):
                    self._close_conn(c, reason="server")
                    continue
                c.next_keepalive_mono = now_mono + self._send_interval
            survivors.append(c)
        self._conns = survivors

    def _maybe_open_locked(self, now_mono: float, now_utc: datetime) -> None:
        if not self._endpoints or self._controller is None:
            return
        while self._pending_opens and now_mono >= self._pending_opens[0]:
            self._pending_opens.pop(0)
            if len(self._conns) >= self._max_concurrent:
                continue  # at cap — drop this scheduled open, reaps will free room
            self._open_conn(now_mono)

    # ── Connection lifecycle ──────────────────────────────────────────

    def _open_conn(self, now_mono: float) -> None:
        ctrl = self._controller
        # The controller owns sampling (orig_bytes + duration drawn from the PHASE
        # percentiles, bias applied, clamped to max). None → nothing to shape →
        # skip (absent-safe; floor only exists to carry the shaped distribution).
        try:
            bytes_target = ctrl.sample_orig_bytes()
            dur_sample = ctrl.sample_duration()
        except Exception:
            bytes_target = dur_sample = None
        if bytes_target is None and dur_sample is None:
            return
        if bytes_target is None:
            bytes_target = 0
        if dur_sample is None:
            dur_sample = 2.0

        host, _url = self._rng.choice(self._endpoints)
        ip = self._resolve(host)
        if ip is None:
            return
        try:
            raw = socket.create_connection((ip, _TLS_PORT), timeout=_SOCK_TIMEOUT_S)
            ctx = ssl.create_default_context()
            sock = ctx.wrap_socket(raw, server_hostname=host)
            sock.settimeout(_SOCK_TIMEOUT_S)
        except (OSError, ssl.SSLError):
            self._ip_cache.pop(host, None)  # stale CDN IP — re-resolve next time
            return

        lifetime = max(0.5, float(dur_sample))
        c = _FloorConn(sock, host, ip, now_mono, now_mono + lifetime,
                       now_mono + self._send_interval, int(bytes_target))
        # First request front-loads orig_bytes (servers may close idle keep-alives
        # fast; orig_bytes is the per-conn total Zeek bins to the start minute).
        if not self._keepalive(c):
            try:
                sock.close()
            except Exception:
                pass
            return
        self._conns.append(c)
        self._opens_this_minute += 1

    def _keepalive(self, c: _FloorConn) -> bool:
        remaining = max(0, c.bytes_target - c.bytes_cum)
        req = self._build_request(c.host, remaining)
        try:
            c.sock.sendall(req)
            c.bytes_cum += len(req)
            c.n_requests += 1
            self._drain_response(c.sock)
            return True
        except (OSError, ssl.SSLError):
            return False

    def _build_request(self, host: str, pad_target: int) -> bytes:
        token = base64.urlsafe_b64encode(
            self._rng.getrandbits(48).to_bytes(6, "big")).decode().rstrip("=")
        base = (f"GET /?_={token} HTTP/1.1\r\n"
                f"Host: {host}\r\n"
                f"User-Agent: Mozilla/5.0\r\n"
                f"Accept: */*\r\n"
                f"Range: bytes=0-0\r\n"
                f"Connection: keep-alive\r\n")
        tail = "\r\n"
        fixed = len(base) + len("X-Ruse-Pad: \r\n") + len(tail)
        pad_len = max(0, min(pad_target - fixed, _MAX_REQUEST_BYTES - fixed))
        if pad_len > 0:
            pad = base64.urlsafe_b64encode(
                self._rng.getrandbits(pad_len * 6).to_bytes(
                    (pad_len * 6 + 7) // 8, "big")).decode()[:pad_len]
            return (base + f"X-Ruse-Pad: {pad}\r\n" + tail).encode("ascii", "ignore")
        return (base + tail).encode("ascii", "ignore")

    def _drain_response(self, sock) -> None:
        buf = b""
        try:
            while b"\r\n\r\n" not in buf and len(buf) < _DRAIN_CAP_BYTES:
                chunk = sock.recv(2048)
                if not chunk:
                    return
                buf += chunk
            head, _, body = buf.partition(b"\r\n\r\n")
            clen = 0
            for line in head.split(b"\r\n"):
                if line.lower().startswith(b"content-length:"):
                    try:
                        clen = int(line.split(b":", 1)[1].strip())
                    except ValueError:
                        clen = 0
                    break
            got = len(body)
            while got < clen and got < _DRAIN_CAP_BYTES:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                got += len(chunk)
        except (OSError, ssl.SSLError):
            return

    def _close_conn(self, c: _FloorConn, reason: str) -> None:
        try:
            c.sock.shutdown(socket.SHUT_RDWR)  # FIN -> Zeek conn_state=SF
        except Exception:
            pass
        try:
            c.sock.close()
        except Exception:
            pass
        ctrl = self._controller
        if ctrl is not None:
            try:
                ctrl.observe_connection(
                    "floor", c.bytes_cum,
                    max(0.0, time.monotonic() - c.open_mono), "SF")
            except Exception:
                pass

    # ── Helpers ───────────────────────────────────────────────────────

    def _resolve(self, host: str) -> Optional[str]:
        ip = self._ip_cache.get(host)
        if ip is not None:
            return ip
        try:
            infos = socket.getaddrinfo(host, _TLS_PORT, family=socket.AF_INET,
                                       type=socket.SOCK_STREAM)
        except OSError:
            return None
        ips = [info[4][0] for info in infos if info[4] and info[4][0]]
        if not ips:
            return None
        ip = self._rng.choice(ips)
        self._ip_cache[host] = ip
        return ip

    def _utc_minute_key(self):
        now = datetime.now(timezone.utc)
        return (now.hour, now.minute)

    def _info(self, msg: str) -> None:
        line = f"{_LOG_PREFIX} {msg}"
        print(line, flush=True)
        if self._logger is not None:
            try:
                self._logger.info(line)
            except Exception:
                pass

    def _warn(self, msg: str) -> None:
        line = f"[WARNING] {_LOG_PREFIX} {msg}"
        print(line, flush=True)
        if self._logger is not None:
            try:
                self._logger.warning(line)
            except Exception:
                pass
