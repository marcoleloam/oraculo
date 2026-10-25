"""Finite, condition-based control of one explicitly identified user LaunchAgent."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import re
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

from oraculo.protocol import MAX_RESPONSE_BYTES
from oraculo.runtime import (
    RuntimeError,
    atomic_write,
    common_options,
    compatible,
    config_hash,
    current_release,
    load_config_verified,
    match_guard,
    receipt,
    recovery_stop,
    require_migration_complete,
    runtime_lock,
    verify,
    write_json,
)


class ServiceController:
    def __init__(
        self,
        root,
        config,
        *,
        label="local.oraculo",
        plist_dir=None,
        log_dir=None,
        timeout=20,
        runner=subprocess.run,
        platform=None,
    ):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,150}", label):
            raise ValueError("invalid_service_label")
        if not 0 < timeout <= 300:
            raise ValueError("timeout_must_be_between_0_and_300")
        self.root, self.config = Path(root).absolute(), Path(config).resolve()
        self.label = label
        self.plist = (plist_dir or Path.home() / "Library/LaunchAgents") / (
            label + ".plist"
        )
        self.logs = log_dir or Path.home() / "Library/Logs/Oraculo"
        self.timeout, self.runner = timeout, runner
        self.supported = (platform or sys.platform) == "darwin"
        self.domain = f"gui/{os.getuid()}"
        self.target = self.domain + "/" + label

    @classmethod
    def from_args(cls, args):
        return cls(
            args.root.expanduser(),
            args.config.expanduser(),
            label=args.label,
            plist_dir=args.plist_dir.expanduser(),
            log_dir=args.log_dir.expanduser(),
            timeout=args.timeout,
        )

    def command(self, *args):
        return self.runner(
            ["/bin/launchctl", *args],
            text=True,
            capture_output=True,
            timeout=self.timeout,
        )

    def require_supported(self):
        if not self.supported:
            raise RuntimeError(
                "service_unsupported",
                "LaunchAgent service control requires macOS. Linux supports package and CLI only.",
            )

    def request(self, kind, payload):
        from oraculo.config import Config

        config = Config.load(self.config)
        request_id = str(uuid.uuid4())
        envelope = dict(
            schema_version=1,
            request_id=request_id,
            kind=kind,
            project_id="*",
            origin={},
            payload=payload,
        )
        deadline = time.monotonic() + min(self.timeout, 5)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(max(0.001, deadline - time.monotonic()))
            client.connect(str(config.socket_path))
            client.sendall(json.dumps(envelope).encode() + b"\n")
            response = bytearray()
            while b"\n" not in response:
                client.settimeout(max(0.001, deadline - time.monotonic()))
                part = client.recv(min(65536, MAX_RESPONSE_BYTES + 1 - len(response)))
                if not part or len(response) + len(part) > MAX_RESPONSE_BYTES:
                    raise RuntimeError("invalid_socket_response")
                response.extend(part)
            value = json.loads(response)
        if value.get("request_id") != request_id:
            raise RuntimeError("socket_correlation")
        if value.get("ok") is not True or not isinstance(value.get("data"), dict):
            raise RuntimeError(
                (value.get("error") or {}).get("code", "service_request_rejected")
            )
        return value["data"]

    def plist_bytes(self):
        if self.plist.is_symlink():
            raise RuntimeError("plist_conflict")
        return self.plist.read_bytes() if self.plist.exists() else None

    def owned_plist(self):
        raw = self.plist_bytes()
        if raw is None:
            return None
        try:
            value = plistlib.loads(raw)
            if value.get("Label") != self.label or value.get(
                "OraculoManagedRoot"
            ) != str(self.root):
                raise RuntimeError(
                    "plist_conflict",
                    "Preserve the foreign plist; choose an isolated --label and --plist-dir.",
                )
            return value
        except (ValueError, plistlib.InvalidFileException):
            raise RuntimeError("plist_conflict") from None

    def plist_value(self, release_id):
        """Deterministic managed identity, also used by offline recovery."""
        release = self.root / "releases" / release_id
        return dict(
            Label=self.label,
            OraculoManagedRoot=str(self.root),
            ProgramArguments=[
                str(release / "venv/bin/python"),
                "-B",
                "-m",
                "oraculo",
                "--config",
                str(self.config),
                "serve",
            ],
            WorkingDirectory=str(release),
            RunAtLoad=True,
            KeepAlive=True,
            ThrottleInterval=30,
            Umask=0o077,
            StandardOutPath=str(self.logs / (self.label + ".stdout.log")),
            StandardErrorPath=str(self.logs / (self.label + ".stderr.log")),
            EnvironmentVariables=dict(
                ORACULO_RELEASE_ID=release_id,
                ORACULO_CONFIG_PATH=str(self.config),
                ORACULO_EXPECTED_CONFIG_SHA256=config_hash(self.config) or "",
                PYTHONDONTWRITEBYTECODE="1",
                PYTHONUNBUFFERED="1",
                PATH=os.pathsep.join(
                    [
                        str(release / "venv/bin"),
                        str(Path.home() / ".local/bin"),
                        "/opt/homebrew/bin",
                        "/usr/local/bin",
                        "/usr/bin",
                        "/bin",
                        "/usr/sbin",
                        "/sbin",
                    ]
                ),
            ),
        )

    def install(self, release_id):
        self.require_supported()
        self.owned_plist()
        verify(self.root / "releases" / release_id, installed=True)
        self.logs.mkdir(parents=True, exist_ok=True)
        atomic_write(self.plist, plistlib.dumps(self.plist_value(release_id)))

    def recovery_identity(self, release_id):
        value = self.owned_plist()
        if value != self.plist_value(release_id):
            raise RuntimeError("plist_runtime_mismatch")
        return dict(
            target=self.target,
            root=str(self.root),
            plist=str(self.plist),
            plist_sha256=hashlib.sha256(self.plist_bytes()).hexdigest(),
            release_id=release_id,
            config_path=str(self.config),
            config_sha256=config_hash(self.config),
        )

    def inspect_launchd(self):
        self.require_supported()
        result = self.command("print", self.target)
        if result.returncode:
            text = result.stdout + result.stderr
            if (
                "Could not find service" in text
                or "Could not find specified service" in text
            ):
                return {"loaded": False, "pid": None}
            return {"loaded": None, "pid": None, "error_code": "launchd_unknown"}
        match = re.search(r"^\s*pid = (\d+)\s*$", result.stdout, re.M)
        state = re.search(r"^\s*state = ([^\n]+)", result.stdout, re.M)

        def scalar(key):
            found = re.search(
                r"^\t" + re.escape(key) + r" = ([^\n]+)$", result.stdout, re.M
            )
            return found[1].strip() if found else None

        arguments = re.search(
            r"^\targuments = \{\n(.*?)^\t\}", result.stdout, re.M | re.S
        )
        environment = re.search(
            r"^\tenvironment = \{\n(.*?)^\t\}", result.stdout, re.M | re.S
        )
        return {
            "loaded": True,
            "pid": int(match[1]) if match else None,
            "launchd_state": state[1].strip() if state else "unknown",
            "path": scalar("path"),
            "program": scalar("program"),
            "arguments": [line.strip() for line in arguments[1].splitlines()]
            if arguments
            else None,
            "environment": dict(
                line.strip().split(" => ", 1)
                for line in environment[1].splitlines()
                if " => " in line
            )
            if environment
            else None,
            "working_directory": scalar("working directory"),
        }

    def loaded_matches(self, loaded, release_id):
        expected = self.plist_value(release_id)
        return (
            loaded.get("path") is not None
            and Path(loaded["path"]).resolve() == self.plist.resolve()
            and loaded.get("program") == expected["ProgramArguments"][0]
            and loaded.get("arguments") == expected["ProgramArguments"]
            and loaded.get("working_directory") == expected["WorkingDirectory"]
            and all(
                (loaded.get("environment") or {}).get(k) == v
                for k, v in expected["EnvironmentVariables"].items()
            )
        )

    def recovery_stop(self, journal, *, journal_name="operation.json"):
        """Stop only the exact replacement recorded before its failed bootstrap.

        The caller holds the runtime lock and subsequently reacquires the official
        offline guard with the same operation/token. No socket reply is needed to
        prove this launchd job's identity, but conflicting socket identity blocks.
        """
        self.require_supported()
        if journal_name not in {"operation.json", "service-operation.json"}:
            raise ValueError("invalid_recovery_journal")
        binding = journal.get("replacement")
        if (
            not journal.get("operation_id")
            or not journal.get("token")
            or journal.get("state") == "complete"
            or not isinstance(binding, dict)
            or journal.get("replacement_maintenance")
            != dict(
                operation_id=journal.get("operation_id"), token=journal.get("token")
            )
        ):
            raise RuntimeError("recovery_identity_unproven")
        release_id = binding.get("release_id")
        if release_id not in {journal.get("target"), journal.get("recovery_target")}:
            raise RuntimeError("recovery_identity_unproven")
        if current_release(
            self.root
        ) != release_id or binding != self.recovery_identity(release_id):
            raise RuntimeError("recovery_identity_mismatch")
        if journal.get("config_sha256") != config_hash(self.config):
            raise RuntimeError("config_changed")
        if journal.get("maintenance_end_attempted"):
            # An end ACK can be lost after admission reopened. A stale journal
            # then proves process ownership, but no longer proves quiescence.
            try:
                guard = self.request("maintenance.status", {})
            except (OSError, ValueError, TimeoutError) as exc:
                raise RuntimeError("maintenance_unproven") from exc
            match_guard(journal, guard)
            if not guard.get("admission_closed") or not guard.get("quiescent"):
                raise RuntimeError("maintenance_unproven")
        observed = self.status()
        if observed["state"] not in {"loaded", "starting", "failed", "unloaded"}:
            raise RuntimeError("service_state_unproven")
        loaded = self.inspect_launchd()
        if loaded["loaded"] is None or (
            loaded["loaded"] and not self.loaded_matches(loaded, release_id)
        ):
            raise RuntimeError("loaded_identity_unproven")
        # Persist the PID before bootout: a crash must not forget a still-live PID.
        pids = set(journal.get("recovery_pids", []))
        pids.update(
            pid for pid in (loaded.get("pid"), journal.get("previous_pid")) if pid
        )
        journal["recovery_pids"] = sorted(pids)
        journal["state"] = "recovery_stopping"
        write_json(self.root / journal_name, journal)
        result = self.stop(recovery_release=release_id)
        self.wait(lambda: not any(self.alive(pid) for pid in pids), "stop_timeout")
        journal["state"] = "recovery_stopped"
        write_json(self.root / journal_name, journal)
        return result

    @staticmethod
    def alive(pid):
        if not pid:
            return False
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    def socket_present(self):
        from oraculo.config import Config

        if not self.config.exists():
            return False
        return Config.load(self.config).socket_path.exists()

    def status(self):
        if not self.supported:
            return receipt(
                "unsupported", error_code="service_unsupported", supported=False
            )
        launchd = self.inspect_launchd()
        state = "unknown"
        if launchd["loaded"] is False:
            state = "unknown" if self.socket_present() else "unloaded"
        elif launchd["loaded"]:
            state = "starting" if launchd["pid"] else "loaded"
            if launchd.get("launchd_state") in {"exiting", "stopping"}:
                state = "stopping"
        result = receipt(state, **launchd)
        if launchd["loaded"] and launchd["pid"]:
            try:
                data = self.request("status.get", {})
                identity = data.get("runtime", {})
                result["runtime"] = identity
                result["health"] = data.get("health")
                expected = current_release(self.root)
                if (
                    identity.get("pid") == launchd["pid"]
                    and identity.get("release_id") == expected
                    and identity.get("config_path") == str(self.config)
                    and identity.get("config_sha256") == config_hash(self.config)
                    and identity.get("protocol_version") == 1
                ):
                    result["state"] = (
                        "online" if data.get("health") == "online" else "failed"
                    )
                    if result["state"] == "failed":
                        result["error_code"] = "service_unhealthy"
                else:
                    result.update(
                        state="unknown", error_code="service_identity_mismatch"
                    )
            except (OSError, ValueError, RuntimeError):
                result["socket"] = "unavailable"
        return result

    def wait(self, predicate, error):
        deadline = time.monotonic() + self.timeout
        while True:
            original_timeout = self.timeout
            self.timeout = max(
                0.001, min(original_timeout, deadline - time.monotonic())
            )
            try:
                matched = predicate()
            except subprocess.TimeoutExpired:
                matched = False
            finally:
                self.timeout = original_timeout
            if matched:
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError(
                    error,
                    "Service state remains uncertain. Inspect status and recover before another start.",
                )
            # Polling delay is bounded by the deadline and never serves as proof.
            time.sleep(min(0.05, remaining))

    def stop(self, *, recovery_release=None):
        self.require_supported()
        before = self.inspect_launchd()
        if before["loaded"] is None:
            raise RuntimeError("launchd_unknown")
        old_pid = before["pid"]
        if before["loaded"]:
            if recovery_release is not None and not self.loaded_matches(
                before, recovery_release
            ):
                raise RuntimeError("loaded_identity_unproven")
            if self.owned_plist() is None:
                raise RuntimeError("plist_missing")
            result = self.command("bootout", self.target)
            if result.returncode and self.inspect_launchd()["loaded"] is not False:
                raise RuntimeError("bootout_failed")
        self.wait(
            lambda: (
                self.inspect_launchd()["loaded"] is False
                and not self.alive(old_pid)
                and not self.socket_present()
            ),
            "stop_timeout",
        )
        return receipt(
            "unloaded", previous_pid=old_pid, source_detail="launchd_pid_socket"
        )

    def start(self, release_id=None, previous_pid=None):
        self.require_supported()
        release_id = release_id or current_release(self.root)
        if release_id is None:
            raise RuntimeError("release_missing")
        verify(self.root / "releases" / release_id, installed=True)
        from oraculo.config import Config

        config = Config.load(self.config)
        value = self.owned_plist()
        expected = [
            str(self.root / "releases" / release_id / "venv/bin/python"),
            "-B",
            "-m",
            "oraculo",
            "--config",
            str(self.config),
            "serve",
        ]
        expected_hash = (
            (value or {})
            .get("EnvironmentVariables", {})
            .get("ORACULO_EXPECTED_CONFIG_SHA256")
        )
        if not value or value.get("ProgramArguments") != expected:
            raise RuntimeError("plist_runtime_mismatch")
        if not expected_hash or config._loaded_sha256 != expected_hash:
            raise RuntimeError("config_changed")
        before = self.status()
        if before["state"] == "online":
            return before
        if before["state"] != "unloaded" or self.alive(previous_pid):
            raise RuntimeError("stop_unconfirmed")
        if self.command("enable", self.target).returncode:
            raise RuntimeError("enable_failed")
        if config_hash(self.config) != expected_hash:
            raise RuntimeError("config_changed")
        if self.command("bootstrap", self.domain, str(self.plist)).returncode:
            raise RuntimeError("bootstrap_failed")
        last = {}

        def online():
            last.update(self.status())
            return (
                last["state"] == "online"
                and last.get("pid") != previous_pid
                and last.get("runtime", {}).get("release_id") == release_id
            )

        self.wait(online, "start_timeout")
        return last


def diagnose(config, *, root=None, **kwargs):
    from oraculo.runtime import DEFAULT_ROOT

    try:
        return ServiceController(root or DEFAULT_ROOT, config, **kwargs).status()
    except (OSError, ValueError, RuntimeError) as exc:
        return receipt(
            "unknown", error_code=getattr(exc, "code", "service_probe_failed")
        )


def register_parsers(subparsers):
    parser = subparsers.add_parser("service")
    actions = parser.add_subparsers(dest="service_action", required=True)
    for action in ("start", "stop", "restart", "status", "uninstall"):
        common_options(actions.add_parser(action))


def start_operation(controller, operation, journal_path, *, previous_pid=None):
    """One guarded bootstrap path for start and restart, including recovery."""
    runtime_journal = controller.root / "operation.json"
    if (
        runtime_journal.exists()
        and json.loads(runtime_journal.read_text()).get("state") != "complete"
    ):
        raise RuntimeError(
            "recovery_required",
            "Resume runtime activate or runtime rollback before service start.",
        )
    from oraculo.maintenance import maintenance_guard

    observed = controller.status()
    pending = operation and operation.get("state") != "complete"
    if not pending and observed["state"] == "online":
        return controller.start(previous_pid=previous_pid)
    if observed["state"] not in {"unloaded", "online"}:
        raise RuntimeError("service_state_unproven")
    selected = current_release(controller.root)
    manifest = verify(controller.root / "releases" / selected, installed=True)
    expected_config = (
        operation.get("config_sha256") if pending else config_hash(controller.config)
    )
    if not expected_config or expected_config != config_hash(controller.config):
        raise RuntimeError("config_changed")
    config = load_config_verified(controller.config, expected_config)
    if not pending:
        operation = dict(
            schema_version=1,
            operation_id=str(uuid.uuid4()),
            token=None,
            state="acquiring_maintenance",
            config_sha256=expected_config,
        )
        write_json(journal_path, operation)

    def prepare(guard):
        match_guard(operation, guard)
        compatible(manifest, guard)
        if operation.get("capacity") not in (None, guard.get("capacity")):
            raise RuntimeError("capacity_changed")
        if observed["state"] == "unloaded":
            # Re-render only under the official guard. The child independently
            # checks this pinned hash before opening State, closing the final
            # config-file race after launchctl bootstrap.
            if config_hash(controller.config) != expected_config:
                raise RuntimeError("config_changed")
            controller.install(selected)
        operation.update(
            token=guard["token"],
            capacity=guard.get("capacity"),
            state="starting",
            maintenance_end_attempted=False,
            target=selected,
            config_sha256=expected_config,
            replacement=controller.recovery_identity(selected),
            replacement_maintenance=dict(
                operation_id=operation["operation_id"], token=guard["token"]
            ),
        )
        if config_hash(controller.config) != expected_config:
            raise RuntimeError("config_changed")
        write_json(journal_path, operation)
        if config_hash(controller.config) != expected_config:
            raise RuntimeError("config_changed")

    if observed["state"] == "unloaded":
        with maintenance_guard(config, operation["operation_id"]) as guard:
            prepare(guard)
    else:
        prepare(
            controller.request(
                "maintenance.begin", {"operation_id": operation["operation_id"]}
            )
        )
    result = controller.start(previous_pid=previous_pid)
    if operation and operation["state"] != "complete":
        guard = controller.request(
            "maintenance.begin",
            {"operation_id": operation["operation_id"]},
        )
        match_guard(operation, guard)
        compatible(manifest, guard)
        if guard.get("capacity") != operation.get("capacity"):
            raise RuntimeError("capacity_changed")
        if config_hash(controller.config) != expected_config:
            raise RuntimeError("config_changed")
        if not operation.get("token"):
            operation["token"] = guard.get("token")
            write_json(journal_path, operation)
        match_guard(operation, controller.request("maintenance.status", {}))
        operation["maintenance_end_attempted"] = True
        write_json(journal_path, operation)
        controller.request(
            "maintenance.end",
            {
                "operation_id": operation["operation_id"],
                "token": operation["token"],
            },
        )
        operation["state"] = "complete"
        write_json(journal_path, operation)
    return result


def dispatch(args):
    controller = ServiceController.from_args(args)
    try:
        if args.service_action == "status":
            return controller.status()
        controller.require_supported()
        if args.dry_run:
            return receipt(
                "planned",
                action=args.service_action,
                label=controller.label,
                plist=str(controller.plist),
                config=str(controller.config),
            )
        with runtime_lock(controller.root):
            require_migration_complete(controller.root)
            journal_path = controller.root / "service-operation.json"
            operation = (
                json.loads(journal_path.read_text()) if journal_path.exists() else None
            )
            if args.service_action == "start":
                return start_operation(controller, operation, journal_path)
            before = controller.status()
            if before["state"] == "online":
                operation_id = (
                    str(uuid.uuid4())
                    if not operation or operation["state"] == "complete"
                    else operation["operation_id"]
                )
                operation = dict(
                    operation_id=operation_id, token=None, state="acquiring_maintenance"
                )
                write_json(journal_path, operation)
                guard = controller.request(
                    "maintenance.begin", {"operation_id": operation_id}
                )
                if (
                    not guard.get("token")
                    or not guard.get("quiescent")
                    or not guard.get("admission_closed")
                ):
                    raise RuntimeError("maintenance_unproven")
                operation = dict(
                    operation_id=operation_id,
                    token=guard["token"],
                    state="stopping",
                    config_sha256=config_hash(controller.config),
                    target=current_release(controller.root),
                    previous_pid=before.get("pid"),
                    replacement=controller.recovery_identity(
                        current_release(controller.root)
                    ),
                    replacement_maintenance=dict(
                        operation_id=operation_id, token=guard["token"]
                    ),
                )
                write_json(journal_path, operation)
            elif before["state"] != "unloaded":
                if args.service_action == "stop" and before["state"] in {
                    "loaded",
                    "starting",
                    "failed",
                }:
                    runtime_path = controller.root / "operation.json"
                    runtime_pending = (
                        runtime_path.exists()
                        and json.loads(runtime_path.read_text()).get("state")
                        != "complete"
                    )
                    return recovery_stop(
                        controller.root,
                        controller.config,
                        controller,
                        journal_name="operation.json"
                        if runtime_pending
                        else "service-operation.json",
                    )
                raise RuntimeError("service_state_unproven")
            result = controller.stop()
            if operation and operation.get("state") != "complete":
                operation["state"] = "stopped"
                write_json(journal_path, operation)
            if args.service_action == "restart":
                return start_operation(
                    controller, operation, journal_path, previous_pid=before.get("pid")
                )
            elif args.service_action == "uninstall":
                controller.owned_plist()
                controller.plist.unlink(missing_ok=True)
                result = receipt(
                    "uninstalled",
                    preserved=[
                        "config",
                        "state",
                        "memory",
                        "credentials",
                        "logs",
                        "releases",
                    ],
                )
            return result
    except RuntimeError as exc:
        result = receipt("blocked", error_code=exc.code)
        result["recovery_action"] = exc.recovery
        return result
    except ValueError as exc:
        if type(exc).__name__ != "StateError":
            raise
        result = receipt("blocked", error_code=str(exc))
        result["recovery_action"] = (
            "Reconcile maintenance through the official interface before retrying."
        )
        return result
    except (OSError, subprocess.SubprocessError) as exc:
        return receipt(
            "failed", error_code="service_io_error", detail=type(exc).__name__
        )


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--config", type=Path, default=Path.home() / ".config/oraculo/config.toml"
    )
    register_parsers(parser.add_subparsers(dest="command", required=True))
    args = parser.parse_args(argv)
    try:
        result = dispatch(args)
        print(json.dumps(result))
        return int(result["state"] in {"failed", "blocked", "unknown", "unsupported"})
    except ValueError as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
