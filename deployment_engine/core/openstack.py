"""OpenStack CLI wrapper with caching."""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import time
from pathlib import Path


class OpenStackCommandError(RuntimeError):
    """An OpenStack query required for a safe scoped operation failed."""


class OpenStack:
    """Wrapper around the OpenStack CLI that sources credentials from an RC file."""

    def __init__(self, rc_file: Path | None = None):
        self.rc_file = rc_file or Path.home() / "vxn3kr-bot-rc"
        self._server_cache: dict[str, str] | None = None

    def _run(
        self,
        *args: str,
        check: bool = True,
        timeout_s: float | None = None,
    ) -> subprocess.CompletedProcess:
        """Run an openstack CLI command with sourced credentials."""
        cmd = f"source {shlex.quote(str(self.rc_file))} && openstack {shlex.join(args)}"
        return subprocess.run(
            ["bash", "-c", cmd],
            capture_output=True,
            text=True,
            check=check,
            timeout=timeout_s,
        )

    def _refresh_servers(self) -> None:
        """Populate the server cache with {name: status} from one CLI call."""
        result = self._run("server", "list", "-f", "value", "-c", "Name", "-c", "Status", check=False)
        cache: dict[str, str] = {}
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                parts = line.strip().split(None, 1)
                if len(parts) >= 2:
                    cache[parts[0]] = parts[1]
        self._server_cache = cache

    def server_list(self, refresh: bool = False) -> list[str]:
        """Return list of server names. Cached after first call."""
        if self._server_cache is None or refresh:
            self._refresh_servers()
        return list(self._server_cache.keys())

    def server_status_map(self, refresh: bool = False) -> dict[str, str]:
        """Return {name: status} for every server. Cached after first call."""
        if self._server_cache is None or refresh:
            self._refresh_servers()
        return dict(self._server_cache)

    def has_vms_with_prefix(self, prefix: str) -> bool:
        """Check if any VMs exist with the given name prefix."""
        return any(name.startswith(prefix) for name in self.server_list())

    def count_vms_with_prefix(self, prefix: str) -> int:
        """Count VMs matching a name prefix (any status)."""
        return sum(1 for name in self.server_list() if name.startswith(prefix))

    def server_list_with_ids(self, prefix: str | None = None) -> list[dict]:
        """Return list of {id, name} dicts, optionally filtered by prefix."""
        result = self._run("server", "list", "-f", "value", "-c", "ID", "-c", "Name", check=False)
        servers = []
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                parts = line.strip().split(None, 1)
                if len(parts) == 2:
                    sid, name = parts
                    if prefix is None or name.startswith(prefix):
                        servers.append({"id": sid, "name": name})
        return servers

    def server_cohort(
        self, prefix: str, *, timeout_s: float | None = None
    ) -> list[dict]:
        """Query one exact name-prefix cohort's IDs, names, and statuses.

        Unlike the older cached list helpers, failure is explicit: teardown
        must never interpret a failed query as an empty cohort. OpenStack's
        server-list response does not reliably expose attached volumes; callers
        must use server_attached_volume_ids before deletion.
        """
        result = self._run(
            "server",
            "list",
            "-f",
            "json",
            "-c",
            "ID",
            "-c",
            "Name",
            "-c",
            "Status",
            check=False,
            timeout_s=timeout_s,
        )
        if result.returncode != 0:
            raise OpenStackCommandError(
                f"server cohort query failed: {result.stderr.strip() or 'unknown error'}"
            )
        try:
            rows = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise OpenStackCommandError("server cohort query returned invalid JSON") from exc
        if not isinstance(rows, list):
            raise OpenStackCommandError("server cohort query did not return a list")

        cohort = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            server_id = str(row.get("ID", row.get("Id", row.get("id", ""))))
            name = str(row.get("Name", row.get("name", "")))
            if not server_id or not name.startswith(prefix):
                continue
            status = str(row.get("Status", row.get("status", "UNKNOWN")))
            cohort.append(
                {
                    "id": server_id,
                    "name": name,
                    "status": status,
                }
            )
        return cohort

    def server_attached_volume_ids(
        self, server_id: str, *, timeout_s: float | None = None
    ) -> list[str]:
        """Capture one exact server's attached volume IDs via server show."""
        result = self._run(
            "server", "show", server_id, "-f", "json",
            check=False, timeout_s=timeout_s,
        )
        if result.returncode != 0:
            raise OpenStackCommandError(
                f"server show failed for {server_id}: "
                f"{result.stderr.strip() or 'unknown error'}"
            )
        try:
            details = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise OpenStackCommandError(
                f"server show returned invalid JSON for {server_id}"
            ) from exc
        if not isinstance(details, dict):
            raise OpenStackCommandError(
                f"server show did not return an object for {server_id}"
            )
        attached = details.get(
            "volumes_attached",
            details.get("Volumes Attached", details.get("volumes attached", [])),
        )
        return _attached_volume_ids(attached)

    def server_lifecycle(
        self, server_id: str, *, timeout_s: float | None = None
    ) -> dict:
        """Return exact-server lifecycle fields used by bounded teardown."""
        result = self._run(
            "server", "show", server_id, "-f", "json",
            check=False, timeout_s=timeout_s,
        )
        if result.returncode != 0:
            raise OpenStackCommandError(
                f"server lifecycle query failed for {server_id}: "
                f"{result.stderr.strip() or 'unknown error'}"
            )
        try:
            details = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise OpenStackCommandError(
                f"server lifecycle query returned invalid JSON for {server_id}"
            ) from exc
        if not isinstance(details, dict):
            raise OpenStackCommandError(
                f"server lifecycle query did not return an object for {server_id}"
            )
        fault = details.get("fault", details.get("Fault"))
        if isinstance(fault, dict):
            fault = json.dumps(fault, sort_keys=True, separators=(",", ":"))
        return {
            "host": details.get(
                "OS-EXT-SRV-ATTR:host",
                details.get("host", details.get("Host")),
            ),
            "status": details.get("status", details.get("Status")),
            "vm_state": details.get(
                "OS-EXT-STS:vm_state", details.get("vm_state")
            ),
            "task_state": details.get(
                "OS-EXT-STS:task_state", details.get("task_state")
            ),
            "power_state": details.get(
                "OS-EXT-STS:power_state", details.get("power_state")
            ),
            "fault": fault,
        }

    def server_delete(self, server_id: str) -> bool:
        """Delete a server by ID. Returns True on success."""
        result = self._run("server", "delete", server_id, check=False)
        return result.returncode == 0

    def server_delete_many(
        self,
        server_ids: list[str],
        *,
        wait: bool = False,
        timeout_s: float | None = None,
    ) -> bool:
        """Delete multiple servers in a single CLI call.
        Each `openstack ...` invocation costs ~17s (python startup + auth),
        so serial-loop deletes scale linearly: 23 RAMPART VMs took ~6 min.
        One call with all IDs collapses that to a single ~17s round trip;
        `--wait` then blocks until OpenStack reports them gone.
        """
        if not server_ids:
            return True
        args = ["server", "delete"]
        if wait:
            args.append("--wait")
        args.extend(server_ids)
        result = self._run(*args, check=False, timeout_s=timeout_s)
        return result.returncode == 0

    def server_force_delete_many(
        self, server_ids: list[str], *, timeout_s: float | None = None
    ) -> bool:
        """Force-delete an already scoped set of server IDs."""
        if not server_ids:
            return True
        result = self._run(
            "server",
            "delete",
            "--force",
            *server_ids,
            check=False,
            timeout_s=timeout_s,
        )
        return result.returncode == 0

    def server_reset_state_active(
        self, server_id: str, *, timeout_s: float | None = None
    ) -> bool:
        """Reset one exact server's administrative state to ACTIVE."""
        result = self._run(
            "server", "set", "--state", "active", server_id,
            check=False, timeout_s=timeout_s,
        )
        return result.returncode == 0

    def server_stop(
        self, server_id: str, *, timeout_s: float | None = None
    ) -> bool:
        """Request shutdown of one exact server ID."""
        result = self._run(
            "server", "stop", server_id,
            check=False, timeout_s=timeout_s,
        )
        return result.returncode == 0

    def volume_delete_many(
        self, volume_ids: list[str], *, timeout_s: float | None = None
    ) -> bool:
        """Delete multiple volumes in one CLI call. Same batching reason."""
        if not volume_ids:
            return True
        result = self._run(
            "volume", "delete", *volume_ids, check=False, timeout_s=timeout_s
        )
        return result.returncode == 0

    def server_show(self, name_or_id: str) -> dict | None:
        """Get server details as dict. Returns None if not found."""
        result = self._run("server", "show", name_or_id, "-f", "json", check=False)
        if result.returncode == 0:
            try:
                return json.loads(result.stdout)
            except json.JSONDecodeError:
                return None
        return None

    def create_server(
        self,
        name: str,
        *,
        flavor: str,
        image: str,
        network: str,
        keypair: str,
        security_group: str,
        deployment: str,
        boot_volume_gb: int = 200,
    ) -> str:
        """Create one exact server and return its OpenStack ID."""
        result = self._run(
            "server", "create",
            "--flavor", flavor,
            "--image", image,
            "--boot-from-volume", str(boot_volume_gb),
            "--network", network,
            "--key-name", keypair,
            "--security-group", security_group,
            "--property", f"deployment={deployment}",
            "-f", "value", "-c", "id",
            name,
            check=False,
            timeout_s=120,
        )
        if result.returncode != 0:
            raise OpenStackCommandError(
                f"server create failed for {name}: "
                f"{result.stderr.strip() or 'unknown error'}"
            )
        server_id = result.stdout.strip()
        if not server_id:
            raise OpenStackCommandError(f"server create returned no ID for {name}")
        self.invalidate_cache()
        return server_id

    def wait_server_active(
        self, name_or_id: str, *, attempts: int = 60, delay_s: float = 5.0
    ) -> dict:
        """Wait for one exact server to become ACTIVE and return its details."""
        last_status = "UNKNOWN"
        for _ in range(attempts):
            details = self.server_show(name_or_id)
            if details is not None:
                last_status = str(
                    details.get("status", details.get("Status", "UNKNOWN"))
                ).upper()
                if last_status == "ACTIVE":
                    return details
                if last_status == "ERROR":
                    raise OpenStackCommandError(
                        f"server {name_or_id} entered ERROR"
                    )
            time.sleep(delay_s)
        raise OpenStackCommandError(
            f"server {name_or_id} did not become ACTIVE (last={last_status})"
        )

    @staticmethod
    def server_ipv4(details: dict) -> str:
        """Extract the first assigned IPv4 address from server-show details."""
        value = details.get("addresses", details.get("Addresses", ""))
        for candidate in re.findall(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", str(value)):
            octets = candidate.split(".")
            if all(0 <= int(octet) <= 255 for octet in octets):
                return candidate
        raise OpenStackCommandError("server show contained no assigned IPv4 address")

    def server_fault(
        self, server_id: str, *, timeout_s: float | None = None
    ) -> str | None:
        """Return a server fault as compact text, if OpenStack reports one."""
        result = self._run(
            "server", "show", server_id, "-f", "json",
            check=False, timeout_s=timeout_s,
        )
        if result.returncode != 0:
            return f"fault query failed: {result.stderr.strip() or 'unknown error'}"
        try:
            details = json.loads(result.stdout)
        except json.JSONDecodeError:
            return "fault query returned invalid JSON"
        fault = details.get("fault", details.get("Fault"))
        if fault in (None, "", {}):
            return None
        if isinstance(fault, str):
            return fault
        return json.dumps(fault, sort_keys=True, separators=(",", ":"))

    def server_events(
        self, server_id: str, *, timeout_s: float | None = None
    ) -> list[dict]:
        """Return Nova instance-action summaries for one exact server."""
        result = self._run(
            "server", "event", "list", server_id, "-f", "json",
            check=False, timeout_s=timeout_s,
        )
        if result.returncode != 0:
            raise OpenStackCommandError(
                f"server event list failed for {server_id}: "
                f"{result.stderr.strip() or 'unknown error'}"
            )
        try:
            events = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise OpenStackCommandError(
                f"server event list returned invalid JSON for {server_id}"
            ) from exc
        if not isinstance(events, list):
            raise OpenStackCommandError(
                f"server event list did not return a list for {server_id}"
            )
        return [event for event in events if isinstance(event, dict)]

    def server_event_show(
        self,
        server_id: str,
        request_id: str,
        *,
        timeout_s: float | None = None,
    ) -> dict:
        """Return one Nova instance-action event by exact server/request ID."""
        result = self._run(
            "server", "event", "show", server_id, request_id, "-f", "json",
            check=False, timeout_s=timeout_s,
        )
        if result.returncode != 0:
            raise OpenStackCommandError(
                f"server event show failed for {server_id}/{request_id}: "
                f"{result.stderr.strip() or 'unknown error'}"
            )
        try:
            event = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise OpenStackCommandError(
                f"server event show returned invalid JSON for "
                f"{server_id}/{request_id}"
            ) from exc
        if not isinstance(event, dict):
            raise OpenStackCommandError(
                f"server event show did not return an object for "
                f"{server_id}/{request_id}"
            )
        return event

    def volume_statuses(
        self, volume_ids: set[str], *, timeout_s: float | None = None
    ) -> dict[str, str]:
        """Query the remaining members of a captured volume-ID cohort."""
        if not volume_ids:
            return {}
        result = self._run(
            "volume", "list", "-f", "json", check=False, timeout_s=timeout_s
        )
        if result.returncode != 0:
            raise OpenStackCommandError(
                f"volume cohort query failed: {result.stderr.strip() or 'unknown error'}"
            )
        try:
            rows = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise OpenStackCommandError("volume cohort query returned invalid JSON") from exc
        if not isinstance(rows, list):
            raise OpenStackCommandError("volume cohort query did not return a list")
        statuses = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            volume_id = str(row.get("ID", row.get("Id", row.get("id", ""))))
            if volume_id in volume_ids:
                statuses[volume_id] = str(
                    row.get("Status", row.get("status", "UNKNOWN"))
                )
        return statuses

    def server_exists(self, name_or_id: str) -> bool:
        """Check if a server exists."""
        result = self._run("server", "show", name_or_id, "-f", "value", "-c", "status", check=False)
        return result.returncode == 0

    def volume_list(self, prefix: str | None = None) -> list[dict]:
        """List volumes, optionally filtered by name prefix."""
        result = self._run("volume", "list", "-f", "json", check=False)
        volumes = []
        if result.returncode == 0:
            try:
                all_vols = json.loads(result.stdout)
                for v in all_vols:
                    name = v.get("Name", "")
                    if prefix is None or name.startswith(prefix):
                        volumes.append(v)
            except json.JSONDecodeError:
                pass
        return volumes

    def volume_delete(self, volume_id: str) -> bool:
        """Delete a volume by ID."""
        result = self._run("volume", "delete", volume_id, check=False)
        return result.returncode == 0

    def find_orphaned_volumes(self, size: int = 200) -> list[dict]:
        """Find volumes with no name, given size, and 'available' status."""
        result = self._run("volume", "list", "-f", "json", check=False)
        orphans = []
        if result.returncode == 0:
            try:
                for v in json.loads(result.stdout):
                    if (
                        not v.get("Name", "").strip()
                        and v.get("Size") == size
                        and v.get("Status") == "available"
                    ):
                        orphans.append(v)
            except json.JSONDecodeError:
                pass
        return orphans

    def zone_list(self) -> list[dict]:
        """List DNS zones."""
        result = self._run("zone", "list", "-f", "json", check=False)
        if result.returncode == 0:
            try:
                return json.loads(result.stdout)
            except json.JSONDecodeError:
                return []
        return []

    def zone_find(self, name: str) -> dict | None:
        """Find a DNS zone by name."""
        for z in self.zone_list():
            zone_name = z.get("name", "")
            # Designate zone names have trailing dot
            if zone_name in (name, f"{name}."):
                return z
        return None

    def zone_delete(self, zone_id: str) -> bool:
        """Delete a DNS zone."""
        result = self._run("zone", "delete", zone_id, check=False)
        return result.returncode == 0

    def invalidate_cache(self) -> None:
        """Force cache refresh on next server_list() call."""
        self._server_cache = None


def _attached_volume_ids(value: object) -> list[str]:
    """Normalize openstackclient's JSON and repr-style attachment values."""
    found: list[str] = []

    def add(candidate: object) -> None:
        text = str(candidate).strip()
        if text and text not in found:
            found.append(text)

    if isinstance(value, dict):
        if "id" in value:
            add(value["id"])
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, dict) and "id" in item:
                add(item["id"])
            elif isinstance(item, str):
                for match in re.findall(
                    r"[\"']?id[\"']?\s*[:=]\s*[\"']([^\"']+)[\"']", item
                ):
                    add(match)
    elif isinstance(value, str):
        for match in re.findall(
            r"[\"']?id[\"']?\s*[:=]\s*[\"']([^\"']+)[\"']", value
        ):
            add(match)
    return found
