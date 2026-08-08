"""Contract tests for the edge deployment config.

These assert properties of ``deploy/Caddyfile.assess-ai`` and
``deploy/oauth2-proxy.cfg`` as text, because the security properties they encode
are properties of the *configuration*, not of any Python we ship. See
``docs/adr/0001-forward-auth-identity-binding.md`` for why each one matters --
several of these lines look redundant and must not be "cleaned up".
"""

from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).parents[1]

SUBJECT_HEADER = "X-Assessment-AI-Authenticated-Subject"
TOKEN_HEADER = "X-Assessment-AI-Proxy-Token"


def _caddyfile() -> str:
    """The Caddyfile with comment lines removed.

    Every assertion below is about what Caddy will *execute*, so comments are
    stripped first. Otherwise prose explaining why ``import gate`` is absent
    would fail the very test asserting its absence, and -- worse -- a commented
    -out directive could satisfy a positive assertion.
    """

    raw = (ROOT / "deploy" / "Caddyfile.assess-ai").read_text()
    return "\n".join(
        line for line in raw.splitlines() if not line.lstrip().startswith("#")
    )


def _reverse_proxy_blocks(caddy: str) -> list[tuple[str, str]]:
    """Yield ``(header_line, body)`` for every ``reverse_proxy … { … }`` block.

    Brace-counting rather than regex: ``header_up`` values legitimately contain
    braces (``{remote_host}``, ``{$ENV_VAR}``), so a non-greedy match to the
    first ``}`` would truncate a block and silently weaken every assertion built
    on it.
    """

    blocks: list[tuple[str, str]] = []
    for match in re.finditer(r"^[ \t]*(reverse_proxy\b[^\n{]*)\{", caddy, re.MULTILINE):
        depth = 1
        index = match.end()
        while index < len(caddy) and depth:
            if caddy[index] == "{":
                depth += 1
            elif caddy[index] == "}":
                depth -= 1
            index += 1
        blocks.append((match.group(1).strip(), caddy[match.end() : index - 1]))
    return blocks


def _header_up_fields(body: str) -> tuple[set[str], set[str]]:
    """Return ``(fields_set, fields_deleted)`` for one block, casefolded."""

    fields_set: set[str] = set()
    fields_deleted: set[str] = set()
    for line in body.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 2 or parts[0] != "header_up":
            continue
        field = parts[1]
        if field.startswith("-"):
            fields_deleted.add(field[1:].casefold())
        else:
            fields_set.add(field.casefold())
    return fields_set, fields_deleted


def test_oidc_proxy_is_pinned_and_does_not_forward_tokens() -> None:
    compose = (ROOT / "docker-compose.yml").read_text()
    proxy = (ROOT / "deploy" / "oauth2-proxy.cfg").read_text()
    assert (
        "quay.io/oauth2-proxy/oauth2-proxy@"
        "sha256:10a1165743a192e1940b4708fb9647027185ce11a681a1c5519b442ff7f1f561"
    ) in compose
    assert "./deploy/oauth2-proxy.cfg:/etc/oauth2-proxy.cfg:ro" in compose
    assert 'client_id = "assessment-ai-dev"' in proxy
    assert 'user_id_claim = "sub"' in proxy
    assert 'code_challenge_method = "S256"' in proxy
    assert "insecure_oidc_skip_nonce = false" in proxy
    assert 'oidc_enabled_signing_algs = ["RS256"]' in proxy
    assert "pass_access_token = false" in proxy
    assert "pass_authorization_header = false" in proxy
    assert "pass_basic_auth = false" in proxy
    assert "pass_user_headers = false" in proxy
    assert 'cookie_name = "__Host-assessment-ai"' in proxy
    # reverse_proxy without trusted_proxy_ips lets any connecting IP set X-Forwarded-*,
    # which oauth2-proxy uses to build the post-login return target. Assert the setting
    # exists rather than its exact CIDR: Docker can renumber the bridge, and pinning the
    # value here would turn a recoverable config drift into a red test.
    assert "reverse_proxy = true" in proxy
    assert "trusted_proxy_ips = [" in proxy
    assert 'cookie_expire = "2h"' in proxy
    # cookie_expire is the whole session bound only while these two hold: no
    # refresh, and a minimal session that cannot carry a refresh token. Pin them
    # so raising the expiry again cannot quietly become "unbounded session".
    assert 'cookie_refresh = "0"' in proxy
    assert "session_cookie_minimal = true" in proxy
    assert "127.0.0.1:8194:4180" in compose


def test_caddy_replaces_basic_auth_with_fail_closed_oidc_identity() -> None:
    caddy = _caddyfile()
    assert "import gate" not in caddy
    assert "forward_auth 127.0.0.1:8194" in caddy
    assert "X-Auth-Request-User>X-Reviewer" in caddy
    assert "header_up -Authorization" in caddy
    assert "header_up -Cookie" in caddy
    assert "header_up -X-Auth-Request-Access-Token" in caddy
    assert "header_up -X-Forwarded-Access-Token" in caddy
    assert "/oauth2/sign_in?rd={scheme}://{host}{uri}" in caddy


def test_basic_auth_placeholder_is_gone() -> None:
    """``{http.auth.user.id}`` is only ever set by ``basic_auth``.

    Under ``forward_auth`` nothing sets it, and Caddy's ``ReplaceKnown`` copies
    unrecognised placeholders through *verbatim* rather than emptying them. The
    backend's ``reviewer_identity()`` fails closed on empty only, so the literal
    string would authenticate every request as one shared fictional reviewer.
    This is the fail-open ADR-0001 exists to prevent.
    """

    assert "{http.auth.user.id}" not in _caddyfile()


def test_identity_strips_precede_the_auth_subrequest() -> None:
    """Version-independent defence against CVE-2026-30851.

    Redundant on Caddy >= 2.11.2, where ``copy_headers`` emits an unconditional
    ``Delete`` ahead of its guarded ``Set``. On 2.10.0-2.11.1 these lines are the
    *only* thing stopping a client-supplied ``X-Reviewer`` from reaching the
    backend when the auth response omits the identity header. Do not delete them.
    """

    caddy = _caddyfile()
    for header in ("X-Reviewer", "X-Reviewer-Email", TOKEN_HEADER):
        assert f"request_header -{header}\n" in caddy, header
    assert (
        f"request_header -{{$ASSESSMENT_AI_COMPUTATION_SPECIALIST_SUBJECT_HEADER:{SUBJECT_HEADER}}}"
        in caddy
    )

    strip_at = caddy.index("request_header -X-Reviewer")
    auth_at = caddy.index("forward_auth 127.0.0.1:8194")
    assert strip_at < auth_at, "strips must be written before forward_auth"


def test_app_block_is_wrapped_in_route_so_written_order_is_execution_order() -> None:
    """``route { }`` is load-bearing, not stylistic.

    The Caddyfile adapter sorts ``request_header`` *after* ``forward_auth`` in
    ``defaultDirectiveOrder``. Outside a ``route``, the strips above would run
    after the copy and delete the very identity they exist to protect.
    """

    caddy = _caddyfile()
    route_at = caddy.index("route {")
    assert route_at < caddy.index("request_header -X-Reviewer")
    assert route_at < caddy.index("forward_auth 127.0.0.1:8194")
    assert route_at < caddy.index("reverse_proxy 127.0.0.1:8093")


def test_no_reverse_proxy_block_both_sets_and_deletes_the_same_field() -> None:
    """``HeaderOps.ApplyTo`` runs Delete *after* Set within a single block.

    Caddyfile order inside the block is irrelevant -- every ``header_up`` line
    collapses into one ``HeaderOps``. So ``header_up -X-Foo`` beside
    ``header_up X-Foo "value"`` silently drops the header. That is the bug
    ``main`` shipped: both computation headers were set and then deleted, so the
    specialist path never received either one.
    """

    blocks = _reverse_proxy_blocks(_caddyfile())
    assert blocks, "no reverse_proxy blocks found -- parser is broken"
    for header_line, body in blocks:
        fields_set, fields_deleted = _header_up_fields(body)
        shadowed = fields_set & fields_deleted
        assert not shadowed, (
            f"{header_line}: {sorted(shadowed)} is both Set and Deleted in one "
            "block; Delete runs last, so the header never reaches the backend. "
            "Move the delete to a request_header line inside the route."
        )


def test_specialist_subject_derives_from_the_copied_reviewer_identity() -> None:
    """ADR-0001: Caddy owns the binding, deriving it from the copied value.

    ``copy_headers`` compiles to a ``map[from]to``, so one source header cannot
    feed two destinations -- the specialist subject needs its own derivation.
    """

    caddy = _caddyfile()
    assert (
        f"header_up {{$ASSESSMENT_AI_COMPUTATION_SPECIALIST_SUBJECT_HEADER:{SUBJECT_HEADER}}} "
        "{http.request.header.X-Reviewer}"
    ) in caddy


def test_reviewer_email_is_not_copied_downstream() -> None:
    """Nothing in the backend reads it, and it does not contain an email.

    ``user_id_claim = "sub"`` trips oauth2-proxy's backwards-compatibility shim,
    which rewrites ``EmailClaim`` to ``sub`` -- so this header carried an opaque
    subject under a name that says otherwise. It was also the ``copy_headers``
    entry most likely to trip CVE-2026-30851, since oauth2-proxy omits a header
    entirely for an empty claim. Tracked:
    ``.scratch/assessment-ai-sso/issues/02-oauth2-proxy-user-id-claim.md``.
    """

    assert "X-Auth-Request-Email>X-Reviewer-Email" not in _caddyfile()


def test_main_side_hardening_survived_the_sso_merge() -> None:
    """The reconciliation is bidirectional -- these came from ``main``."""

    caddy = _caddyfile()
    assert "encode zstd gzip" in caddy
    assert "script-src 'self'" in caddy
    assert "object-src 'none'" in caddy


def test_csp_allows_form_posts_to_reach_cas() -> None:
    """Without the CAS origin, every form action fails *silently* on expiry.

    ``form-action`` is enforced across the redirect chain, and an expired session
    turns a form POST into a cross-origin redirect to LibreOne. Applied live on
    2026-08-01; this file must carry it or a deploy would revert the fix.
    """

    assert "form-action 'self' https://one.libretexts.dev;" in _caddyfile()
