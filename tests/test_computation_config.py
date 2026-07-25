from pathlib import Path

import pytest
from pydantic import ValidationError

from app.computation import ComputationProfile
from app.config import (
    DEFAULT_COMPUTATION_SPECIALIST_SUBJECT_HEADER,
    Settings,
)
from app.jobs import validate_computation_profile_settings


PROXY_TOKEN = "proxy_token_0123456789abcdef0123456789abcdef"


def test_computation_defaults_are_disabled_and_empty() -> None:
    settings = Settings(_env_file=None)

    assert settings.computation_mode == "off"
    assert settings.computation_families == ()
    assert settings.computation_specialist_subjects == ()
    assert (
        settings.computation_specialist_subject_header
        == DEFAULT_COMPUTATION_SPECIALIST_SUBJECT_HEADER
    )
    assert settings.computation_trusted_proxy_token is None
    assert settings.computation_specialist_proxy_ready is False
    assert settings.computation_image_reference == "unavailable"
    assert settings.computation_container_digest == "unavailable"
    assert settings.computation_image_is_immutable is False
    assert settings.computation_socket_path == Path(
        "/run/assessment-computation/compute.sock"
    )
    assert settings.computation_numeric_timeout_seconds == 2.0
    assert settings.computation_unit_timeout_seconds == 2.0
    assert settings.computation_algebraic_timeout_seconds == 5.0


def test_computation_environment_settings_are_strict_and_normalized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ASSESSMENT_AI_COMPUTATION_MODE", "assist")
    monkeypatch.setenv(
        "ASSESSMENT_AI_COMPUTATION_FAMILY_ALLOWLIST",
        " Unit, NUMERIC, algebraic ",
    )
    monkeypatch.setenv(
        "ASSESSMENT_AI_COMPUTATION_SPECIALIST_SUBJECT_ALLOWLIST",
        "reviewer-123,instructor@libretexts.dev",
    )
    monkeypatch.setenv(
        "ASSESSMENT_AI_COMPUTATION_SPECIALIST_SUBJECT_HEADER",
        "X-Assessment-AI-SSO-Subject",
    )
    monkeypatch.setenv(
        "ASSESSMENT_AI_COMPUTATION_TRUSTED_PROXY_TOKEN",
        PROXY_TOKEN,
    )
    monkeypatch.setenv(
        "ASSESSMENT_AI_COMPUTATION_SOCKET_PATH",
        "/tmp/assessment-computation-test.sock",
    )
    image_digest = "a" * 64
    monkeypatch.setenv(
        "ASSESSMENT_AI_COMPUTATION_IMAGE_REFERENCE",
        f"registry.example/assessment-computation@sha256:{image_digest}",
    )

    settings = Settings(_env_file=None)

    assert settings.computation_mode == "assist"
    assert settings.computation_families == ("unit", "numeric", "algebraic")
    assert settings.computation_specialist_subjects == (
        "reviewer-123",
        "instructor@libretexts.dev",
    )
    assert (
        settings.computation_specialist_subject_header == "x-assessment-ai-sso-subject"
    )
    assert settings.computation_specialist_proxy_ready is True
    assert settings.computation_socket_path == Path(
        "/tmp/assessment-computation-test.sock"
    )
    assert settings.computation_numeric_timeout_seconds == 2.0
    assert settings.computation_unit_timeout_seconds == 2.0
    assert settings.computation_algebraic_timeout_seconds == 5.0
    assert settings.computation_container_digest == f"sha256:{image_digest}"
    assert settings.computation_image_is_immutable is True


@pytest.mark.parametrize(
    "image_reference",
    [
        "https://registry.example/assessment-computation:latest",
        "Registry.example/assessment-computation@sha256:" + ("a" * 64),
        "registry.example/assessment-computation@@sha256:" + ("a" * 64),
        "registry.example/assessment-computation@sha256:" + ("A" * 64),
        "registry.example/assessment computation:latest",
        "registry.example/assessment-computation@sha512:" + ("a" * 64),
    ],
)
def test_invalid_computation_image_references_are_rejected(
    image_reference: str,
) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, computation_image_reference=image_reference)


@pytest.mark.parametrize("mode", ["", "OFF", "observe", "enabled"])
def test_unknown_computation_modes_fail_settings_construction(mode: str) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, computation_mode=mode)


@pytest.mark.parametrize(
    "families",
    [
        "calculus",
        "numeric,calculus",
        "numeric,,unit",
        ",numeric",
        "unit,",
        "numeric,numeric",
    ],
)
def test_invalid_computation_family_allowlists_are_rejected(
    families: str,
) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, computation_family_allowlist=families)


@pytest.mark.parametrize(
    "subjects",
    [
        "reviewer,,instructor",
        ",reviewer",
        "reviewer,",
        "reviewer,reviewer",
        "reviewer one",
        "reviewer\none",
    ],
)
def test_invalid_specialist_subject_allowlists_are_rejected(
    subjects: str,
) -> None:
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            computation_specialist_subject_allowlist=subjects,
        )


@pytest.mark.parametrize(
    "header",
    [
        "",
        "X-Reviewer",
        "Authorization",
        "X Assessment AI Subject",
        "X-Assessment-AI-Proxy-Token",
        "X-Assessment-AI-Subject\nInjected",
    ],
)
def test_specialist_subject_header_is_dedicated_and_safe(header: str) -> None:
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            computation_specialist_subject_header=header,
        )


@pytest.mark.parametrize(
    "token",
    [
        "short",
        "x" * 31,
        "x" * 257,
        "x" * 31 + " ",
        "x" * 31 + "/",
        "x" * 31 + "\n",
    ],
)
def test_trusted_proxy_token_requires_bounded_base64url_secret(token: str) -> None:
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            computation_trusted_proxy_token=token,
        )


def test_blank_trusted_proxy_token_is_normalized_to_unavailable() -> None:
    settings = Settings(
        _env_file=None,
        computation_mode="assist",
        computation_specialist_subject_allowlist="specialist@example.edu",
        computation_trusted_proxy_token="",
    )

    assert settings.computation_trusted_proxy_token is None
    assert settings.computation_specialist_proxy_ready is False


@pytest.mark.parametrize("mode", ["assist", "enforce"])
def test_allowlist_without_proxy_token_leaves_attestation_fail_closed(
    mode: str,
) -> None:
    settings = Settings(
        _env_file=None,
        computation_mode=mode,
        computation_specialist_subject_allowlist="specialist@example.edu",
    )

    assert settings.computation_specialist_subjects == ("specialist@example.edu",)
    assert settings.computation_specialist_proxy_ready is False


@pytest.mark.parametrize(
    "socket_path",
    [
        "relative/compute.sock",
        "/run/assessment-computation/compute.socket",
        "/run/assessment-computation/../compute.sock",
    ],
)
def test_computation_socket_must_be_an_absolute_socket_path(
    socket_path: str,
) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, computation_socket_path=socket_path)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("computation_numeric_timeout_seconds", 1.5),
        ("computation_unit_timeout_seconds", 2.5),
        ("computation_algebraic_timeout_seconds", 4.5),
    ],
)
def test_computation_timeouts_are_fixed_to_qualified_v0(
    field: str,
    value: float,
) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: value})


def test_computation_health_config_exposes_only_non_secret_status() -> None:
    subject = "private-reviewer-subject"
    proxy_token = PROXY_TOKEN
    settings = Settings(
        _env_file=None,
        computation_mode="enforce",
        computation_family_allowlist="numeric,unit",
        computation_specialist_subject_allowlist=subject,
        computation_trusted_proxy_token=proxy_token,
    )

    assert settings.computation_health_config == {
        "mode": "enforce",
        "enabled": True,
        "families": ["numeric", "unit"],
        "transport": "unix",
        "socket_path": "/run/assessment-computation/compute.sock",
        "image_reference": "unavailable",
        "timeouts_seconds": {
            "numeric": 2.0,
            "unit": 2.0,
            "algebraic": 5.0,
        },
        "specialist_subject_count": 1,
    }
    assert subject not in repr(settings.computation_health_config)
    assert proxy_token not in repr(settings)
    assert proxy_token not in repr(settings.computation_health_config)
    assert settings.computation_specialist_subject_header not in repr(
        settings.computation_health_config
    )


def test_external_computation_profile_uses_engine_gates_on_every_path() -> None:
    profile = ComputationProfile(family="numeric", delivery="webwork")

    with pytest.raises(ValueError, match="Parameterized item generation"):
        validate_computation_profile_settings(
            Settings(
                _env_file=None,
                computation_mode="assist",
                computation_family_allowlist="numeric",
            ),
            profile,
        )
    with pytest.raises(ValueError, match="WeBWorK engine"):
        validate_computation_profile_settings(
            Settings(
                _env_file=None,
                computation_mode="assist",
                computation_family_allowlist="numeric",
                parameterized_items_enabled=True,
            ),
            profile,
        )

    validate_computation_profile_settings(
        Settings(
            _env_file=None,
            computation_mode="assist",
            computation_family_allowlist="numeric",
            parameterized_items_enabled=True,
            webwork_enabled=True,
        ),
        profile,
    )
