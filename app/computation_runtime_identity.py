from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


RUNTIME_MANIFEST_SCHEMA_VERSION = "assessment-computation-runtime-manifest-v0"
RUNTIME_MANIFEST_RESPONSE_HEADER = "x-assessment-computation-runtime-manifest-sha256"
RUNTIME_MANIFEST_PATH = Path("/app/computation-runtime-manifest.json")
UNAVAILABLE_RUNTIME_MANIFEST_SHA256 = "unavailable"

_MAX_MANIFEST_BYTES = 16 * 1024
_HASHED_ROOT_FILES = (
    "Dockerfile.compute",
    "pyproject.toml",
    "uv.lock",
)
_PINNED_DISTRIBUTIONS = {
    "pint": "0.25.3",
    "sympy": "1.14.0",
    "ucumvert": "0.3.2",
}
_MAX_DISTRIBUTIONS = 512
_MAX_DISTRIBUTION_NAME_LENGTH = 128
_MAX_DISTRIBUTION_VERSION_LENGTH = 64
_DISTRIBUTION_SEPARATOR_PATTERN = re.compile(r"[-_.]+")
_NORMALIZED_DISTRIBUTION_PATTERN = re.compile(
    rf"[a-z0-9](?:[a-z0-9-]{{0,{_MAX_DISTRIBUTION_NAME_LENGTH - 2}}}[a-z0-9])?"
)
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")


class RuntimeManifestError(ValueError):
    """Raised when the immutable computation build manifest is unavailable."""


@dataclass(frozen=True)
class ComputationRuntimeIdentity:
    """Verified identity reported by one built computation sidecar."""

    manifest_sha256: str
    source_tree_sha256: str
    dependency_versions: tuple[tuple[str, str], ...]


def build_runtime_manifest(root: Path) -> bytes:
    """Build canonical manifest bytes from the computation runtime contents."""

    root = root.resolve()
    dependency_versions = _installed_dependency_versions()
    payload = {
        "schema_version": RUNTIME_MANIFEST_SCHEMA_VERSION,
        "service": "assessment-computation",
        "protocol_schema_version": "assessment-computation-v0",
        "source_tree_sha256": _source_tree_sha256(root),
        "dependency_versions": dependency_versions,
    }
    body = _canonical_json(payload)
    if len(body) > _MAX_MANIFEST_BYTES:
        raise RuntimeManifestError("runtime manifest exceeds the permitted size")
    return body


def write_runtime_manifest(root: Path, output: Path) -> ComputationRuntimeIdentity:
    """Write a build-time manifest and return its verified identity."""

    body = build_runtime_manifest(root)
    output.write_bytes(body)
    return load_runtime_identity(output, root=root)


def load_runtime_identity(
    manifest_path: Path = RUNTIME_MANIFEST_PATH,
    *,
    root: Path = Path("/app"),
) -> ComputationRuntimeIdentity:
    """Load and verify a build manifest against the current runtime files.

    This detects a stale or accidentally copied manifest. It does not claim to
    prove the OCI image digest; the application binds this handshake to a
    separately reviewed immutable image reference.
    """

    try:
        body = manifest_path.read_bytes()
    except OSError as exc:
        raise RuntimeManifestError("runtime manifest is unavailable") from exc
    if not body or len(body) > _MAX_MANIFEST_BYTES:
        raise RuntimeManifestError("runtime manifest has an invalid size")
    try:
        payload = json.loads(
            body,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_non_finite_json,
        )
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeManifestError("runtime manifest is invalid") from exc
    _validate_manifest_payload(payload)
    if body != _canonical_json(payload):
        raise RuntimeManifestError("runtime manifest is not canonical")

    actual_source_tree_sha256 = _source_tree_sha256(root.resolve())
    if payload["source_tree_sha256"] != actual_source_tree_sha256:
        raise RuntimeManifestError("runtime source tree does not match its manifest")
    dependency_versions = _installed_dependency_versions()
    if payload["dependency_versions"] != dependency_versions:
        raise RuntimeManifestError(
            "runtime dependencies do not match the build manifest"
        )
    return ComputationRuntimeIdentity(
        manifest_sha256=hashlib.sha256(body).hexdigest(),
        source_tree_sha256=actual_source_tree_sha256,
        dependency_versions=tuple(sorted(dependency_versions.items())),
    )


def validate_runtime_manifest_sha256(value: str) -> str:
    normalized = value.strip().casefold()
    if not _SHA256_PATTERN.fullmatch(normalized):
        raise ValueError("runtime manifest identity must be a lowercase SHA-256")
    return normalized


def _source_tree_sha256(root: Path) -> str:
    paths: list[Path] = []
    app_root = root / "app"
    if not app_root.is_dir():
        raise RuntimeManifestError("runtime app source tree is unavailable")
    paths.extend(
        path
        for path in app_root.rglob("*.py")
        if path.is_file() and "__pycache__" not in path.parts
    )
    for relative_path in _HASHED_ROOT_FILES:
        path = root / relative_path
        if not path.is_file():
            raise RuntimeManifestError(
                f"runtime identity input is unavailable: {relative_path}"
            )
        paths.append(path)

    digest = hashlib.sha256()
    for path in sorted(
        paths, key=lambda candidate: candidate.relative_to(root).as_posix()
    ):
        if path.is_symlink():
            raise RuntimeManifestError("runtime identity inputs must not be symlinks")
        relative_path = path.relative_to(root).as_posix().encode("utf-8")
        body = path.read_bytes()
        digest.update(len(relative_path).to_bytes(4, "big"))
        digest.update(relative_path)
        digest.update(len(body).to_bytes(8, "big"))
        digest.update(body)
    return digest.hexdigest()


def _installed_dependency_versions() -> dict[str, str]:
    versions: dict[str, str] = {}
    try:
        distributions = tuple(importlib.metadata.distributions())
    except Exception as exc:
        raise RuntimeManifestError(
            "installed runtime distributions are unavailable"
        ) from exc
    for distribution in distributions:
        try:
            raw_name = distribution.metadata["Name"]
            version = distribution.version
        except Exception as exc:
            raise RuntimeManifestError(
                "installed runtime distribution metadata is invalid"
            ) from exc
        name = _normalize_distribution_name(raw_name)
        _validate_distribution_version(version)
        if name in versions:
            raise RuntimeManifestError(
                f"duplicate normalized runtime distribution: {name}"
            )
        versions[name] = version
        if len(versions) > _MAX_DISTRIBUTIONS:
            raise RuntimeManifestError("too many installed runtime distributions")
    _assert_pinned_dependency_versions(versions)
    return {name: versions[name] for name in sorted(versions)}


def _normalize_distribution_name(value: Any) -> str:
    if not isinstance(value, str):
        raise RuntimeManifestError("runtime distribution name is invalid")
    normalized = _DISTRIBUTION_SEPARATOR_PATTERN.sub("-", value).casefold()
    if not _NORMALIZED_DISTRIBUTION_PATTERN.fullmatch(normalized):
        raise RuntimeManifestError("runtime distribution name is invalid")
    return normalized


def _validate_distribution_version(value: Any) -> None:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > _MAX_DISTRIBUTION_VERSION_LENGTH
        or any(ord(character) < 0x21 or ord(character) > 0x7E for character in value)
    ):
        raise RuntimeManifestError("runtime distribution version is invalid")


def _assert_pinned_dependency_versions(versions: dict[str, str]) -> None:
    for name, expected_version in _PINNED_DISTRIBUTIONS.items():
        if versions.get(name) != expected_version:
            raise RuntimeManifestError(
                f"runtime dependency is not qualified: {name}=={expected_version}"
            )


def _validate_dependency_versions(value: Any) -> None:
    if not isinstance(value, dict) or not value or len(value) > _MAX_DISTRIBUTIONS:
        raise RuntimeManifestError("runtime dependency identity is invalid")
    normalized_names: set[str] = set()
    for name, version in value.items():
        normalized_name = _normalize_distribution_name(name)
        if normalized_name in normalized_names:
            raise RuntimeManifestError(
                f"duplicate normalized runtime distribution: {normalized_name}"
            )
        normalized_names.add(normalized_name)
        if name != normalized_name:
            raise RuntimeManifestError("runtime distribution names must be normalized")
        _validate_distribution_version(version)
    _assert_pinned_dependency_versions(value)


def _validate_manifest_payload(payload: Any) -> None:
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version",
        "service",
        "protocol_schema_version",
        "source_tree_sha256",
        "dependency_versions",
    }:
        raise RuntimeManifestError("runtime manifest shape is invalid")
    if payload["schema_version"] != RUNTIME_MANIFEST_SCHEMA_VERSION:
        raise RuntimeManifestError("runtime manifest schema is unsupported")
    if payload["service"] != "assessment-computation":
        raise RuntimeManifestError("runtime manifest service is invalid")
    if payload["protocol_schema_version"] != "assessment-computation-v0":
        raise RuntimeManifestError("runtime protocol schema is invalid")
    if not isinstance(
        payload["source_tree_sha256"], str
    ) or not _SHA256_PATTERN.fullmatch(payload["source_tree_sha256"]):
        raise RuntimeManifestError("runtime source identity is invalid")
    _validate_dependency_versions(payload["dependency_versions"])


def _canonical_json(payload: dict[str, Any]) -> bytes:
    return (
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("ascii")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_non_finite_json(value: str) -> None:
    raise ValueError(f"non-finite JSON number: {value}")


def _main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("build",))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    write_runtime_manifest(args.root, args.output)


if __name__ == "__main__":
    _main()
