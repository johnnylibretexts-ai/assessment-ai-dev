#!/usr/bin/env python3
"""Build and qualify the computation runtime under its deployed isolation flags.

The harness creates only uniquely named, labeled Docker resources and removes
those exact resources in a ``finally`` block.  It never discovers, stops, or
removes existing containers, volumes, or images.
"""

from __future__ import annotations

import argparse
import base64
import errno
import json
import re
import selectors
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
RESOURCE_PREFIX = "lt-assessment-computation-qualification"
SOCKET_PATH = "/run/assessment-computation/compute.sock"
CONTAINER_MEMORY_BYTES = 640 * 1024 * 1024
CHILD_MEMORY_BYTES = 256 * 1024 * 1024
CHILD_FILE_SIZE_BYTES = (256 * 1024) + 4096

_REQUEST_PROBE = r"""
import base64
import socket
import sys

method, path, encoded = sys.argv[1:4]
body = base64.b64decode(encoded)
headers = [
    f"{method} {path} HTTP/1.1",
    "Host: assessment-computation",
    "Accept: application/json",
    "Connection: close",
]
if body:
    headers.extend(
        [
            "Content-Type: application/json",
            f"Content-Length: {len(body)}",
        ]
    )
wire = ("\r\n".join(headers) + "\r\n\r\n").encode("ascii") + body
client = socket.socket(socket.AF_UNIX)
client.settimeout(10)
client.connect("/run/assessment-computation/compute.sock")
client.sendall(wire)
chunks = []
while True:
    chunk = client.recv(65536)
    if not chunk:
        break
    chunks.append(chunk)
client.close()
sys.stdout.write(base64.b64encode(b"".join(chunks)).decode("ascii"))
"""

_SECURITY_PROBE = r"""
import errno
import json
import os
import socket
import stat
from pathlib import Path

def service_pid():
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            parts = (entry / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        if b"app.computation_service:app" in parts:
            return int(entry.name)
    raise RuntimeError("uvicorn service process was not found")

pid = service_pid()
status = {}
for line in Path(f"/proc/{pid}/status").read_text().splitlines():
    if ":" in line:
        key, value = line.split(":", 1)
        status[key] = value.strip()

socket_stat = os.stat("/run/assessment-computation/compute.sock")
network = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
network.settimeout(0.25)
egress_errno = network.connect_ex(("1.1.1.1", 443))
network.close()

try:
    with open("/qualification-nonroot-write-probe", "xb"):
        pass
except OSError as exc:
    nonroot_write_errno = exc.errno
else:
    nonroot_write_errno = 0
    os.unlink("/qualification-nonroot-write-probe")

print(
    json.dumps(
        {
            "service_pid": pid,
            "service_uid": [int(value) for value in status["Uid"].split()],
            "service_gid": [int(value) for value in status["Gid"].split()],
            "cap_ambient": status["CapAmb"],
            "cap_bounding": status["CapBnd"],
            "cap_effective": status["CapEff"],
            "cap_inheritable": status["CapInh"],
            "cap_permitted": status["CapPrm"],
            "no_new_privileges": int(status["NoNewPrivs"]),
            "socket_is_uds": stat.S_ISSOCK(socket_stat.st_mode),
            "socket_mode": stat.S_IMODE(socket_stat.st_mode),
            "interfaces": sorted(os.listdir("/sys/class/net")),
            "egress_errno": egress_errno,
            "nonroot_write_errno": nonroot_write_errno,
        },
        sort_keys=True,
    )
)
"""

_ROOT_WRITE_PROBE = r"""
import errno
import json
import os

path = "/qualification-rootfs-write-probe"
try:
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
except OSError as exc:
    print(json.dumps({"errno": exc.errno}))
    raise SystemExit(0 if exc.errno == errno.EROFS else 2)
else:
    os.close(descriptor)
    os.unlink(path)
    print(json.dumps({"errno": 0}))
    raise SystemExit(3)
"""

_STOP_NEXT_BOUNDED_CHILD = r"""
import json
import os
import signal
import time
from pathlib import Path

def service_pid():
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            parts = (entry / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        if b"app.computation_service:app" in parts:
            return int(entry.name)
    raise RuntimeError("uvicorn service process was not found")

parent = service_pid()

def children():
    result = set()
    for task in Path(f"/proc/{parent}/task").iterdir():
        try:
            result.update((task / "children").read_text().split())
        except OSError:
            continue
    return result

baseline = children()
print("READY", flush=True)
deadline = time.monotonic() + 10
while time.monotonic() < deadline:
    try:
        candidates = children() - baseline
    except OSError:
        candidates = set()
    for candidate in sorted(candidates, key=int):
        limits_path = Path(f"/proc/{candidate}/limits")
        try:
            limits = limits_path.read_text()
        except OSError:
            continue
        address_line = next(
            (
                line
                for line in limits.splitlines()
                if line.startswith("Max address space")
            ),
            "",
        )
        if "268435456" not in address_line:
            continue
        try:
            os.kill(int(candidate), signal.SIGSTOP)
        except ProcessLookupError:
            continue
        print(
            json.dumps(
                {
                    "child_pid": int(candidate),
                    "parent_pid": parent,
                    "state": Path(f"/proc/{candidate}/status")
                    .read_text()
                    .split("State:", 1)[1]
                    .splitlines()[0]
                    .strip(),
                },
                sort_keys=True,
            ),
            flush=True,
        )
        raise SystemExit(0)
    time.sleep(0.001)
raise SystemExit("bounded computation child was not observed")
"""

_SERVICE_CHILDREN = r"""
import json
from pathlib import Path

for entry in Path("/proc").iterdir():
    if not entry.name.isdigit():
        continue
    try:
        parts = (entry / "cmdline").read_bytes().split(b"\0")
    except OSError:
        continue
    if b"app.computation_service:app" in parts:
        children = set()
        for task in (entry / "task").iterdir():
            try:
                children.update((task / "children").read_text().split())
            except OSError:
                continue
        details = []
        for child in sorted(children, key=int):
            try:
                command = [
                    value.decode("utf-8", errors="replace")
                    for value in Path(f"/proc/{child}/cmdline")
                    .read_bytes()
                    .split(b"\0")
                    if value
                ]
                state = next(
                    line.split(":", 1)[1].strip()
                    for line in Path(f"/proc/{child}/status").read_text().splitlines()
                    if line.startswith("State:")
                )
            except OSError:
                continue
            details.append({"pid": int(child), "command": command, "state": state})
        print(json.dumps(details, sort_keys=True))
        raise SystemExit(0)
raise SystemExit("uvicorn service process was not found")
"""


class QualificationFailure(RuntimeError):
    """A fail-closed container qualification failure."""


def _run(
    command: list[str],
    *,
    timeout: float = 120.0,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command,
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if check and completed.returncode != 0:
        raise QualificationFailure(
            f"command failed ({completed.returncode}): {' '.join(command)}\n"
            f"{completed.stderr.strip()}"
        )
    return completed


def _docker_exec(
    container_name: str,
    script: str,
    *arguments: str,
    user: str | None = None,
    timeout: float = 15.0,
) -> subprocess.CompletedProcess[str]:
    command = ["docker", "exec"]
    if user is not None:
        command.extend(["--user", user])
    command.extend([container_name, "python", "-c", script, *arguments])
    return _run(command, timeout=timeout)


def _decode_chunked(body: bytes) -> bytes:
    decoded = bytearray()
    position = 0
    while True:
        line_end = body.find(b"\r\n", position)
        if line_end < 0:
            raise QualificationFailure("malformed chunked HTTP response")
        try:
            size = int(body[position:line_end].split(b";", 1)[0], 16)
        except ValueError as exc:
            raise QualificationFailure("invalid chunk size in HTTP response") from exc
        position = line_end + 2
        if size == 0:
            return bytes(decoded)
        end = position + size
        if end + 2 > len(body) or body[end : end + 2] != b"\r\n":
            raise QualificationFailure("truncated chunked HTTP response")
        decoded.extend(body[position:end])
        position = end + 2


def _parse_http(encoded_response: str) -> tuple[int, dict[str, Any]]:
    try:
        wire = base64.b64decode(encoded_response.strip(), validate=True)
        raw_headers, body = wire.split(b"\r\n\r\n", 1)
        lines = raw_headers.split(b"\r\n")
        status = int(lines[0].split(b" ", 2)[1])
        headers = {
            key.decode("ascii").casefold(): value.decode("ascii").strip()
            for key, value in (line.split(b":", 1) for line in lines[1:])
        }
        if headers.get("transfer-encoding", "").casefold() == "chunked":
            body = _decode_chunked(body)
        payload = json.loads(body)
    except (ValueError, KeyError, json.JSONDecodeError) as exc:
        raise QualificationFailure("invalid HTTP response from Unix socket") from exc
    if not isinstance(payload, dict):
        raise QualificationFailure("Unix-socket response was not a JSON object")
    return status, payload


def _request(
    container_name: str,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    body = (
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        if payload is not None
        else b""
    )
    completed = _docker_exec(
        container_name,
        _REQUEST_PROBE,
        method,
        path,
        base64.b64encode(body).decode("ascii"),
    )
    return _parse_http(completed.stdout)


def _numeric_blueprint(value: int) -> dict[str, Any]:
    return {
        "schema_version": "assessment-computation-v0",
        "profile": {"family": "numeric", "delivery": "numerical"},
        "operation": "evaluate",
        "expression": {"kind": "integer", "integer": value},
    }


def _wait_ready(container_name: str) -> dict[str, Any]:
    deadline = time.monotonic() + 30
    last_detail = ""
    while time.monotonic() < deadline:
        try:
            status, payload = _request(container_name, "GET", "/readyz")
            if status == 200:
                return payload
            last_detail = f"HTTP {status}: {payload}"
        except (QualificationFailure, subprocess.SubprocessError) as exc:
            last_detail = str(exc)
        time.sleep(0.2)
    raise QualificationFailure(f"sidecar did not become ready: {last_detail}")


def _readline(
    process: subprocess.Popen[str],
    *,
    timeout: float,
) -> str:
    if process.stdout is None:
        raise QualificationFailure("observer stdout was not captured")
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    try:
        if not selector.select(timeout):
            raise QualificationFailure("timed out waiting for child observer")
        line = process.stdout.readline()
    finally:
        selector.close()
    if not line:
        stderr = process.stderr.read().strip() if process.stderr is not None else ""
        raise QualificationFailure(f"child observer exited early: {stderr}")
    return line.strip()


def _start_child_observer(container_name: str) -> subprocess.Popen[str]:
    process = subprocess.Popen(
        [
            "docker",
            "exec",
            container_name,
            "python",
            "-c",
            _STOP_NEXT_BOUNDED_CHILD,
        ],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if _readline(process, timeout=10) != "READY":
        process.kill()
        raise QualificationFailure("child observer did not enter ready state")
    return process


def _start_request(
    container_name: str,
    payload: dict[str, Any],
) -> subprocess.Popen[str]:
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return subprocess.Popen(
        [
            "docker",
            "exec",
            container_name,
            "python",
            "-c",
            _REQUEST_PROBE,
            "POST",
            "/v0/compute",
            base64.b64encode(body).decode("ascii"),
        ],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _observe_stopped_child(
    observer: subprocess.Popen[str],
) -> dict[str, Any]:
    try:
        observed = json.loads(_readline(observer, timeout=10))
        return_code = observer.wait(timeout=2)
    except (json.JSONDecodeError, subprocess.TimeoutExpired) as exc:
        observer.kill()
        raise QualificationFailure("invalid child observer result") from exc
    if return_code != 0:
        stderr = observer.stderr.read().strip() if observer.stderr is not None else ""
        raise QualificationFailure(f"child observer failed: {stderr}")
    if not isinstance(observed, dict):
        raise QualificationFailure("child observer result was not an object")
    if not str(observed.get("state", "")).startswith("T"):
        raise QualificationFailure("computation child did not enter stopped state")
    return observed


def _finish_request(
    process: subprocess.Popen[str],
    *,
    timeout: float = 10,
) -> tuple[int, dict[str, Any]]:
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        process.kill()
        process.communicate()
        raise QualificationFailure("Unix-socket request did not complete") from exc
    if process.returncode != 0:
        raise QualificationFailure(
            f"Unix-socket request probe failed: {stderr.strip()}"
        )
    return _parse_http(stdout)


def _parse_child_limits(text: str) -> dict[str, tuple[str, str]]:
    patterns = {
        "address_space": r"^Max address space\s+(\S+)\s+(\S+)",
        "core_file_size": r"^Max core file size\s+(\S+)\s+(\S+)",
        "cpu_time": r"^Max cpu time\s+(\S+)\s+(\S+)",
        "file_size": r"^Max file size\s+(\S+)\s+(\S+)",
        "open_files": r"^Max open files\s+(\S+)\s+(\S+)",
        "processes": r"^Max processes\s+(\S+)\s+(\S+)",
    }
    parsed: dict[str, tuple[str, str]] = {}
    for name, pattern in patterns.items():
        match = re.search(pattern, text, flags=re.MULTILINE)
        if match is None:
            raise QualificationFailure(f"child limit was not reported: {name}")
        parsed[name] = (match.group(1), match.group(2))
    return parsed


def _is_resource_tracker(child: dict[str, Any]) -> bool:
    command = " ".join(str(part) for part in child.get("command", []))
    return "multiprocessing.resource_tracker import main" in command


def _wait_for_no_request_children(
    container_name: str,
) -> list[dict[str, Any]]:
    deadline = time.monotonic() + 5
    children: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        children = json.loads(
            _docker_exec(
                container_name,
                _SERVICE_CHILDREN,
                timeout=5,
            ).stdout
        )
        request_children = [
            child for child in children if not _is_resource_tracker(child)
        ]
        if not request_children:
            return children
        time.sleep(0.05)
    raise QualificationFailure(
        f"a request worker survived its request boundary: {request_children}"
    )


def _assert_equal(actual: Any, expected: Any, description: str) -> None:
    if actual != expected:
        raise QualificationFailure(
            f"{description}: expected {expected!r}, observed {actual!r}"
        )


def _inspect_runtime(container_name: str, volume_name: str) -> dict[str, Any]:
    inspection = json.loads(
        _run(["docker", "inspect", container_name], timeout=15).stdout
    )[0]
    host = inspection["HostConfig"]
    config = inspection["Config"]
    mounts = inspection["Mounts"]

    _assert_equal(host["NetworkMode"], "none", "network mode")
    _assert_equal(host["ReadonlyRootfs"], True, "read-only root")
    _assert_equal({value.casefold() for value in host["CapDrop"]}, {"all"}, "cap drop")
    if not any(
        value.casefold().startswith("no-new-privileges")
        for value in host["SecurityOpt"]
    ):
        raise QualificationFailure("no-new-privileges was not applied")
    _assert_equal(host["PidsLimit"], 64, "container PID limit")
    _assert_equal(host["Memory"], CONTAINER_MEMORY_BYTES, "container memory limit")
    _assert_equal(host["NanoCpus"], 1_000_000_000, "container CPU limit")
    _assert_equal(host["Init"], True, "container init")
    _assert_equal(host["PortBindings"], {}, "host port bindings")
    _assert_equal(host["Binds"], None, "bind mounts")
    _assert_equal(config.get("ExposedPorts"), None, "exposed ports")
    _assert_equal(config["User"], "assessment-compute", "configured runtime user")

    tmpfs = host["Tmpfs"].get("/tmp", "")
    for required in {"rw", "noexec", "nosuid", "nodev", "size=64m"}:
        if required not in set(tmpfs.split(",")):
            raise QualificationFailure(f"/tmp is missing mount option {required}")

    non_tmp_mounts = [mount for mount in mounts if mount["Destination"] != "/tmp"]
    if len(non_tmp_mounts) != 1:
        raise QualificationFailure("runtime has an unexpected mount")
    socket_mount = non_tmp_mounts[0]
    _assert_equal(socket_mount["Type"], "volume", "socket mount type")
    _assert_equal(socket_mount["Name"], volume_name, "socket volume")
    _assert_equal(
        socket_mount["Destination"],
        "/run/assessment-computation",
        "socket mount destination",
    )

    sensitive_names = []
    for assignment in config.get("Env", []):
        name = assignment.split("=", 1)[0].casefold()
        if any(
            marker in name
            for marker in (
                "access_token",
                "api_key",
                "credential",
                "password",
                "private_key",
                "secret",
            )
        ):
            sensitive_names.append(name)
    _assert_equal(sensitive_names, [], "credential-like runtime environment names")

    return {
        "network_mode": host["NetworkMode"],
        "read_only": host["ReadonlyRootfs"],
        "cap_drop": host["CapDrop"],
        "security_opt": host["SecurityOpt"],
        "pids_limit": host["PidsLimit"],
        "memory_bytes": host["Memory"],
        "nano_cpus": host["NanoCpus"],
        "init": host["Init"],
        "host_port_bindings": host["PortBindings"],
        "bind_mounts": host["Binds"],
        "socket_mount_type": socket_mount["Type"],
        "tmpfs": tmpfs,
    }


def _qualify(
    *,
    run_id: str,
    image_tag: str,
    container_name: str,
    volume_name: str,
) -> dict[str, Any]:
    print(f"building unique candidate image {image_tag}", file=sys.stderr)
    _run(
        [
            "docker",
            "build",
            "--target",
            "runtime",
            "--label",
            f"org.libretexts.assessment-computation-qualification={run_id}",
            "--tag",
            image_tag,
            "--file",
            "Dockerfile.compute",
            ".",
        ],
        timeout=600,
    )
    image = json.loads(_run(["docker", "image", "inspect", image_tag]).stdout)[0]

    _run(
        [
            "docker",
            "volume",
            "create",
            "--label",
            f"org.libretexts.assessment-computation-qualification={run_id}",
            volume_name,
        ]
    )
    print(f"starting unique candidate container {container_name}", file=sys.stderr)
    _run(
        [
            "docker",
            "run",
            "--detach",
            "--name",
            container_name,
            "--label",
            f"org.libretexts.assessment-computation-qualification={run_id}",
            "--init",
            "--read-only",
            "--network",
            "none",
            "--mount",
            f"type=volume,source={volume_name},target=/run/assessment-computation",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,nodev,size=64m",
            "--security-opt",
            "no-new-privileges=true",
            "--cap-drop",
            "ALL",
            "--pids-limit",
            "64",
            "--memory",
            "640m",
            "--cpus",
            "1.0",
            image_tag,
        ],
        timeout=30,
    )

    inspect_evidence = _inspect_runtime(container_name, volume_name)
    readiness = _wait_ready(container_name)
    _assert_equal(readiness["status"], "ready", "readiness status")
    _assert_equal(
        readiness["schema_version"],
        "assessment-computation-v0",
        "readiness schema",
    )

    security = json.loads(
        _docker_exec(container_name, _SECURITY_PROBE, timeout=10).stdout
    )
    _assert_equal(security["service_uid"], [10002, 10002, 10002, 10002], "service UID")
    _assert_equal(security["service_gid"], [10002, 10002, 10002, 10002], "service GID")
    for capability_field in (
        "cap_ambient",
        "cap_bounding",
        "cap_effective",
        "cap_inheritable",
        "cap_permitted",
    ):
        _assert_equal(
            int(security[capability_field], 16),
            0,
            f"{capability_field} mask",
        )
    _assert_equal(security["no_new_privileges"], 1, "service no-new-privileges")
    _assert_equal(security["socket_is_uds"], True, "socket type")
    if security["socket_mode"] & 0o666 != 0o666:
        raise QualificationFailure(
            "Unix socket is not readable and writable across container UIDs"
        )
    routed_interfaces = [
        name for name in security["interfaces"] if name.startswith(("en", "eth", "wl"))
    ]
    if routed_interfaces:
        raise QualificationFailure(
            f"unexpected routed network interfaces: {routed_interfaces}"
        )
    if security["egress_errno"] == 0:
        raise QualificationFailure("container established an external TCP connection")
    if security["nonroot_write_errno"] not in {errno.EACCES, errno.EROFS}:
        raise QualificationFailure(
            "non-root service could write outside its socket volume"
        )

    root_write = json.loads(
        _docker_exec(
            container_name,
            _ROOT_WRITE_PROBE,
            user="0",
            timeout=10,
        ).stdout
    )
    _assert_equal(root_write["errno"], errno.EROFS, "root filesystem write errno")

    timeout_observer = _start_child_observer(container_name)
    timeout_request = _start_request(container_name, _numeric_blueprint(2))
    timeout_child = _observe_stopped_child(timeout_observer)
    timeout_limits_raw = _docker_exec(
        container_name,
        f"print(open('/proc/{timeout_child['child_pid']}/limits').read())",
        timeout=5,
    ).stdout
    timeout_limits = _parse_child_limits(timeout_limits_raw)
    _assert_equal(
        timeout_limits["address_space"],
        (str(CHILD_MEMORY_BYTES), str(CHILD_MEMORY_BYTES)),
        "child address-space limit",
    )
    _assert_equal(
        timeout_limits["core_file_size"],
        ("0", "0"),
        "child core-file limit",
    )
    _assert_equal(
        timeout_limits["cpu_time"],
        ("2", "3"),
        "numeric child CPU limit",
    )
    _assert_equal(
        timeout_limits["file_size"],
        (str(CHILD_FILE_SIZE_BYTES), str(CHILD_FILE_SIZE_BYTES)),
        "child file-size limit",
    )
    _assert_equal(timeout_limits["open_files"], ("32", "32"), "child FD limit")
    _assert_equal(timeout_limits["processes"], ("1", "1"), "child process limit")

    timeout_status, timeout_payload = _finish_request(timeout_request)
    _assert_equal(timeout_status, 504, "stopped child timeout status")
    _assert_equal(
        timeout_payload,
        {"detail": "The computation request exceeded its time limit."},
        "stopped child timeout response",
    )
    infrastructure_children = _wait_for_no_request_children(container_name)

    recovery_observer = _start_child_observer(container_name)
    recovery_request = _start_request(container_name, _numeric_blueprint(2))
    recovery_child = _observe_stopped_child(recovery_observer)
    if recovery_child["child_pid"] == timeout_child["child_pid"]:
        raise QualificationFailure("fresh request reused the prior child PID")
    _docker_exec(
        container_name,
        f"import os, signal; os.kill({recovery_child['child_pid']}, signal.SIGCONT)",
        timeout=5,
    )
    recovery_status, recovery_payload = _finish_request(recovery_request)
    _assert_equal(recovery_status, 200, "post-timeout recovery status")
    _assert_equal(recovery_payload["exact_value"], "2", "post-timeout exact value")
    recovered_infrastructure_children = _wait_for_no_request_children(container_name)
    _assert_equal(
        recovered_infrastructure_children,
        infrastructure_children,
        "stable non-request infrastructure processes",
    )

    different_status, different_payload = _request(
        container_name,
        "POST",
        "/v0/compute",
        _numeric_blueprint(7),
    )
    repeat_status, repeat_payload = _request(
        container_name,
        "POST",
        "/v0/compute",
        _numeric_blueprint(2),
    )
    _assert_equal(different_status, 200, "different request status")
    _assert_equal(repeat_status, 200, "repeat request status")
    _assert_equal(different_payload["exact_value"], "7", "different exact value")
    _assert_equal(repeat_payload, recovery_payload, "repeat-request determinism")
    if different_payload["blueprint_hash"] == recovery_payload["blueprint_hash"]:
        raise QualificationFailure("distinct blueprints shared a result hash")
    repeated_infrastructure_children = _wait_for_no_request_children(container_name)
    _assert_equal(
        repeated_infrastructure_children,
        infrastructure_children,
        "cross-request infrastructure process stability",
    )

    return {
        "schema_version": "assessment-computation-container-qualification-v0",
        "run_id": run_id,
        "completed_at": datetime.now(UTC).isoformat(),
        "passed": True,
        "image_id": image["Id"],
        "container_runtime": inspect_evidence,
        "service_security": security,
        "readiness": readiness,
        "child_limits": {
            name: {"soft": values[0], "hard": values[1]}
            for name, values in timeout_limits.items()
        },
        "timeout_recovery": {
            "timeout_status": timeout_status,
            "timeout_child_pid": timeout_child["child_pid"],
            "recovery_status": recovery_status,
            "recovery_child_pid": recovery_child["child_pid"],
        },
        "cross_request_isolation": {
            "first_blueprint_hash": recovery_payload["blueprint_hash"],
            "different_blueprint_hash": different_payload["blueprint_hash"],
            "repeat_blueprint_hash": repeat_payload["blueprint_hash"],
            "no_surviving_request_workers": True,
            "infrastructure_children": infrastructure_children,
        },
    }


def _cleanup(
    *,
    image_tag: str,
    container_name: str,
    volume_name: str,
) -> list[str]:
    cleaned: list[str] = []
    for resource, command in (
        (
            f"container:{container_name}",
            ["docker", "container", "rm", "--force", container_name],
        ),
        (
            f"volume:{volume_name}",
            ["docker", "volume", "rm", volume_name],
        ),
        (
            f"image:{image_tag}",
            ["docker", "image", "rm", image_tag],
        ),
    ):
        completed = _run(command, timeout=30, check=False)
        if completed.returncode == 0:
            cleaned.append(resource)
    return cleaned


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build and qualify a uniquely named Assessment Computation runtime "
            "container, then remove the exact test resources."
        )
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Optional path for the bounded JSON qualification report.",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    run_id = uuid.uuid4().hex[:12]
    image_tag = f"{RESOURCE_PREFIX}:{run_id}"
    container_name = f"{RESOURCE_PREFIX}-{run_id}"
    volume_name = f"{RESOURCE_PREFIX}-socket-{run_id}"
    report: dict[str, Any] | None = None
    failure: BaseException | None = None

    _run(["docker", "version"], timeout=15)
    try:
        report = _qualify(
            run_id=run_id,
            image_tag=image_tag,
            container_name=container_name,
            volume_name=volume_name,
        )
    except BaseException as exc:
        failure = exc
        logs = _run(
            ["docker", "logs", container_name],
            timeout=10,
            check=False,
        )
        if logs.stdout.strip() or logs.stderr.strip():
            print("candidate container logs:", file=sys.stderr)
            print(logs.stdout.strip(), file=sys.stderr)
            print(logs.stderr.strip(), file=sys.stderr)
    finally:
        cleaned = _cleanup(
            image_tag=image_tag,
            container_name=container_name,
            volume_name=volume_name,
        )

    if failure is not None:
        raise failure
    if report is None:
        raise QualificationFailure("qualification produced no report")
    report["cleaned_resources"] = cleaned
    encoded = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    sys.stdout.write(encoded)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (QualificationFailure, subprocess.SubprocessError) as exc:
        print(f"qualification failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
