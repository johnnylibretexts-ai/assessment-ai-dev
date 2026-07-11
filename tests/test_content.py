from __future__ import annotations

import hashlib
import hmac
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from app.config import Settings
from app.content import (
    ContentAuthenticationError,
    ContentConfigurationError,
    ContentLimitError,
    ContentNotFoundError,
    ContentResponseError,
    ContentTransportError,
    CXoneSandboxContentAdapter,
    UnsafeSandboxPathError,
)


ROOT = "Sandboxes/johnnyphung"
PAGE = f"{ROOT}/Demo Book/Page One"


def settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "server_key": "test-key",
        "server_secret": "test-secret",
        "server_user": "test-user",
        "cxone_env_file": tmp_path / "missing.env",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def adapter(
    tmp_path: Path,
    handler: Callable[[httpx.Request], httpx.Response],
    **setting_overrides: object,
) -> CXoneSandboxContentAdapter:
    return CXoneSandboxContentAdapter(
        settings(tmp_path, **setting_overrides),
        transport=httpx.MockTransport(handler),
        clock=lambda: 1_720_000_000.9,
    )


def json_response(payload: object, status_code: int = 200) -> httpx.Response:
    return httpx.Response(status_code, json=payload)


@pytest.mark.asyncio
async def test_fetch_page_is_get_only_double_encoded_and_normalized(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raw_path = request.url.raw_path.split(b"?", 1)[0]
        if raw_path.endswith(b"/contents"):
            return json_response(
                {
                    "body": [
                        "<div><h2> Intro </h2>",
                        123,
                        "<p>Hello <strong>world</strong>.</p>",
                        "<p>Second&nbsp; line<br>continued.</p>",
                        "<script>ignore me</script><img alt='diagram'></div>",
                    ]
                }
            )
        return json_response({"@id": 42, "title": "A Demo Page"})

    async with adapter(tmp_path, handler) as content:
        page = await content.fetch_page(PAGE)

    assert len(requests) == 2
    assert all(request.method == "GET" for request in requests)
    assert all(request.url.scheme == "https" for request in requests)
    assert all(request.url.host == "dev.libretexts.org" for request in requests)
    assert all(
        request.url.path.startswith("/@api/deki/pages/=") for request in requests
    )
    assert all(
        b"Sandboxes%252Fjohnnyphung" in request.url.raw_path for request in requests
    )
    assert all(request.url.params["dream.out.format"] == "json" for request in requests)
    assert requests[1].url.params["mode"] == "view"

    assert page.title == "A Demo Page"
    assert page.html_body.startswith("<div><h2> Intro")
    assert "Hello <strong>world</strong>" in page.html_body
    assert page.plaintext == (
        "Intro\n\nHello world.\n\nSecond line\n\ncontinued.\n\n[Image: diagram]"
    )
    assert [paragraph.index for paragraph in page.paragraphs] == list(
        range(len(page.paragraphs))
    )
    for paragraph in page.paragraphs:
        assert page.plaintext[paragraph.start : paragraph.end] == paragraph.text
        assert paragraph.end - paragraph.start == len(paragraph.text)
    assert page.source.path == PAGE
    assert page.source.page_id == "42"
    assert page.source.canonical_url == (
        "https://dev.libretexts.org/Sandboxes/johnnyphung/Demo%20Book/Page%20One"
    )


@pytest.mark.asyncio
async def test_hmac_token_matches_mirror_client(tmp_path: Path) -> None:
    observed_token = ""

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal observed_token
        observed_token = request.headers["X-Deki-Token"]
        return json_response({"@id": "1", "title": "Root"})

    content = adapter(tmp_path, handler)
    try:
        await content.fetch_toc(ROOT, max_depth=0)
    finally:
        await content.aclose()

    epoch = 1_720_000_000
    digest = hmac.new(
        b"test-secret", f"test-key{epoch}=test-user".encode(), hashlib.sha256
    ).hexdigest()
    assert observed_token == f"test-key_{epoch}_=test-user_{digest}"


@pytest.mark.asyncio
async def test_credentials_load_from_configured_env_file(tmp_path: Path) -> None:
    credential_file = tmp_path / "cxone.env"
    credential_file.write_text(
        "# ignored\n"
        "export SERVER_KEY='file-key'\n"
        'SERVER_SECRET="file-secret"\n'
        "SERVER_USER=file-user\n"
        "UNRELATED=not-loaded\n",
        encoding="utf-8",
    )
    seen = ""

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen
        seen = request.headers["X-Deki-Token"]
        return json_response({"title": "Root"})

    content = CXoneSandboxContentAdapter(
        settings(
            tmp_path,
            server_key=None,
            server_secret=None,
            server_user=None,
            cxone_env_file=credential_file,
        ),
        transport=httpx.MockTransport(handler),
        clock=lambda: 10,
    )
    try:
        await content.fetch_toc(ROOT, max_depth=0)
    finally:
        await content.aclose()

    digest = hmac.new(
        b"file-secret", b"file-key10=file-user", hashlib.sha256
    ).hexdigest()
    assert seen == f"file-key_10_=file-user_{digest}"


@pytest.mark.asyncio
async def test_bare_process_credentials_support_compose_env_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SERVER_KEY", "process-key")
    monkeypatch.setenv("SERVER_SECRET", "process-secret")
    monkeypatch.setenv("SERVER_USER", "process-user")

    def handler(_request: httpx.Request) -> httpx.Response:
        return json_response({"title": "Root"})

    content = CXoneSandboxContentAdapter(
        settings(tmp_path, server_key=None, server_secret=None, server_user=None),
        transport=httpx.MockTransport(handler),
    )
    try:
        toc = await content.fetch_toc(ROOT, max_depth=0)
    finally:
        await content.aclose()
    assert toc.path == ROOT


@pytest.mark.asyncio
async def test_settings_credentials_take_precedence_over_process_and_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SERVER_KEY", "process-key")
    monkeypatch.setenv("SERVER_SECRET", "process-secret")
    monkeypatch.setenv("SERVER_USER", "process-user")
    credential_file = tmp_path / "cxone.env"
    credential_file.write_text(
        "SERVER_KEY=file-key\nSERVER_SECRET=file-secret\nSERVER_USER=file-user\n",
        encoding="utf-8",
    )
    content = CXoneSandboxContentAdapter(
        settings(tmp_path, cxone_env_file=credential_file),
        transport=httpx.MockTransport(lambda _request: json_response({})),
    )
    try:
        assert content._server_key == "test-key"
        assert content._server_secret == "test-secret"
        assert content._server_user == "test-user"
    finally:
        await content.aclose()


def test_missing_credentials_report_names_not_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("SERVER_KEY", "SERVER_SECRET", "SERVER_USER", "CXONE_ENV_FILE"):
        monkeypatch.delenv(name, raising=False)
    partial_secret = "present-but-never-print-me"
    with pytest.raises(ContentConfigurationError) as caught:
        CXoneSandboxContentAdapter(
            settings(
                tmp_path,
                server_key=partial_secret,
                server_secret=None,
                server_user=None,
            )
        )
    assert "SERVER_SECRET" in str(caught.value)
    assert "SERVER_USER" in str(caught.value)
    assert partial_secret not in str(caught.value)


@pytest.mark.asyncio
async def test_explicit_cxone_env_file_override_is_honored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("SERVER_KEY", "SERVER_SECRET", "SERVER_USER"):
        monkeypatch.delenv(name, raising=False)
    override = tmp_path / "override.env"
    override.write_text(
        "SERVER_KEY=override-key\nSERVER_SECRET=override-secret\nSERVER_USER=override-user\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CXONE_ENV_FILE", str(override))
    content = CXoneSandboxContentAdapter(
        Settings(
            _env_file=None,
            server_key=None,
            server_secret=None,
            server_user=None,
            cxone_env_file=tmp_path / "missing.env",
        ),
        transport=httpx.MockTransport(lambda _request: json_response({})),
    )
    try:
        assert content._server_key == "override-key"
        assert content._server_user == "override-user"
    finally:
        await content.aclose()


@pytest.mark.parametrize(
    "unsafe_path",
    [
        "https://dev.libretexts.org/Sandboxes/johnnyphung/Page",
        "//dev.libretexts.org/Sandboxes/johnnyphung/Page",
        "Sandboxes/someone-else/Page",
        "Sandboxes/johnnyphung/../someone-else/Page",
        "Sandboxes/johnnyphung/./Page",
        "Sandboxes/johnnyphung/%2e%2e/someone-else",
        "Sandboxes/johnnyphung/%252e%252e/someone-else",
        r"Sandboxes\johnnyphung\Page",
        "Sandboxes/johnnyphung//Page",
        "Sandboxes/johnnyphung/Page?mode=edit",
        "Sandboxes/johnnyphung/Page#fragment",
        "Sandboxes/johnnyphung/Page\x00suffix",
        "https://[",
    ],
)
@pytest.mark.asyncio
async def test_unsafe_paths_fail_before_transport(
    tmp_path: Path, unsafe_path: str
) -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return json_response({})

    async with adapter(tmp_path, handler) as content:
        with pytest.raises(UnsafeSandboxPathError):
            await content.fetch_page(unsafe_path)
    assert calls == 0


@pytest.mark.asyncio
async def test_case_insensitive_root_is_canonicalized(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.raw_path.split(b"?", 1)[0].endswith(b"/contents"):
            return json_response({"body": "<p>Body</p>"})
        return json_response({"title": "Title"})

    async with adapter(tmp_path, handler) as content:
        page = await content.fetch_page("/sandboxes/JOHNNYPHUNG/Page")
    assert page.source.path == "Sandboxes/johnnyphung/Page"


@pytest.mark.asyncio
async def test_auth_not_found_and_status_errors_are_typed_and_sanitized(
    tmp_path: Path,
) -> None:
    secret_body = "response-must-not-leak"
    cases = [
        (403, ContentAuthenticationError),
        (404, ContentNotFoundError),
        (500, ContentTransportError),
        (302, ContentTransportError),
    ]
    for status, error_type in cases:
        content = adapter(
            tmp_path,
            lambda _request, status=status: httpx.Response(
                status, text=secret_body, headers={"location": "https://example.test"}
            ),
        )
        try:
            with pytest.raises(error_type) as caught:
                await content.fetch_toc(ROOT, max_depth=0)
        finally:
            await content.aclose()
        assert secret_body not in str(caught.value)
        assert "test-secret" not in str(caught.value)


@pytest.mark.asyncio
async def test_transport_and_malformed_json_errors_are_safe(tmp_path: Path) -> None:
    def disconnected(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("credential-shaped-private-detail", request=request)

    async with adapter(tmp_path, disconnected) as content:
        with pytest.raises(ContentTransportError) as caught:
            await content.fetch_toc(ROOT, max_depth=0)
    assert "credential-shaped-private-detail" not in str(caught.value)

    async with adapter(
        tmp_path, lambda _request: httpx.Response(200, text="private invalid body")
    ) as content:
        with pytest.raises(ContentResponseError) as caught:
            await content.fetch_toc(ROOT, max_depth=0)
    assert "private invalid body" not in str(caught.value)


@pytest.mark.asyncio
async def test_empty_unreadable_and_malformed_bodies_are_rejected(
    tmp_path: Path,
) -> None:
    bodies: list[object] = [None, [], {"unexpected": "shape"}, "<script>x</script>"]
    for body in bodies:

        def handler(request: httpx.Request, body: object = body) -> httpx.Response:
            if request.url.raw_path.split(b"?", 1)[0].endswith(b"/contents"):
                return json_response({"body": body})
            return json_response({"title": "Title"})

        async with adapter(tmp_path, handler) as content:
            with pytest.raises(ContentResponseError):
                await content.fetch_page(PAGE)


@pytest.mark.asyncio
async def test_source_character_limit_fails_closed(tmp_path: Path) -> None:
    long_text = "x" * 1_001

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.raw_path.split(b"?", 1)[0].endswith(b"/contents"):
            return json_response({"body": f"<p>{long_text}</p>"})
        return json_response({"title": "Title"})

    async with adapter(tmp_path, handler, max_source_chars=1_000) as content:
        with pytest.raises(ContentLimitError):
            await content.fetch_page(PAGE)


@pytest.mark.asyncio
@pytest.mark.parametrize("malformed", [0, False, "", {}])
async def test_malformed_falsy_subpage_shapes_are_rejected(
    tmp_path: Path, malformed: object
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.raw_path.split(b"?", 1)[0].endswith(b"/subpages"):
            return json_response({"page.subpage": malformed})
        return json_response({"@id": "1", "title": "Root"})

    async with adapter(tmp_path, handler) as content:
        with pytest.raises(ContentResponseError):
            await content.fetch_toc(ROOT)


@pytest.mark.asyncio
async def test_recursive_toc_handles_singletons_and_stops_at_leaves(
    tmp_path: Path,
) -> None:
    requested_paths: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        raw_path = request.url.raw_path.split(b"?", 1)[0]
        requested_paths.append(raw_path)
        if not raw_path.endswith(b"/subpages"):
            return json_response({"@id": "root-id", "title": "Book"})
        if b"Demo%2520Book/subpages" in raw_path:
            return json_response(
                {
                    "page.subpage": {
                        "@id": "chapter-id",
                        "@subpages": "true",
                        "title": "Chapter",
                        "path": {"#text": f"{ROOT}/Demo Book/Chapter"},
                    }
                }
            )
        if b"Chapter/subpages" in raw_path:
            return json_response(
                {
                    "page.subpage": [
                        {
                            "@id": 9,
                            "@subpages": "false",
                            "title": "Lesson",
                            "path": {"#text": f"{ROOT}/Demo Book/Chapter/Lesson"},
                        }
                    ]
                }
            )
        raise AssertionError(f"unexpected request: {raw_path!r}")

    async with adapter(tmp_path, handler) as content:
        toc = await content.fetch_toc(f"{ROOT}/Demo Book")

    assert toc.model_dump() == {
        "title": "Book",
        "path": f"{ROOT}/Demo Book",
        "page_id": "root-id",
        "children": [
            {
                "title": "Chapter",
                "path": f"{ROOT}/Demo Book/Chapter",
                "page_id": "chapter-id",
                "children": [
                    {
                        "title": "Lesson",
                        "path": f"{ROOT}/Demo Book/Chapter/Lesson",
                        "page_id": "9",
                        "children": [],
                    }
                ],
            }
        ],
    }
    assert len(requested_paths) == 3


@pytest.mark.asyncio
async def test_toc_depth_bound_avoids_deeper_requests(tmp_path: Path) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if request.url.raw_path.split(b"?", 1)[0].endswith(b"/subpages"):
            return json_response(
                {
                    "page.subpage": {
                        "@subpages": "true",
                        "title": "Child",
                        "path": {"#text": f"{ROOT}/Child"},
                    }
                }
            )
        return json_response({"title": "Root"})

    async with adapter(tmp_path, handler) as content:
        toc = await content.fetch_toc(ROOT, max_depth=1)
    assert calls == 2
    assert len(toc.children) == 1
    assert toc.children[0].children == []


@pytest.mark.asyncio
async def test_toc_page_bound_cycle_and_out_of_scope_children_fail_closed(
    tmp_path: Path,
) -> None:
    async def run_with_subpages(payload: object, error: type[Exception]) -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            if request.url.raw_path.split(b"?", 1)[0].endswith(b"/subpages"):
                return json_response(payload)
            return json_response({"title": "Root"})

        async with adapter(tmp_path, handler) as content:
            with pytest.raises(error):
                await content.fetch_toc(ROOT, max_pages=1)
        assert calls == 2

    child = {
        "page.subpage": {
            "@subpages": "false",
            "title": "Child",
            "path": {"#text": f"{ROOT}/Child"},
        }
    }
    await run_with_subpages(child, ContentLimitError)

    sibling = {
        "page.subpage": {
            "@subpages": "false",
            "title": "Escape",
            "path": {"#text": "Sandboxes/someone-else/Page"},
        }
    }
    await run_with_subpages(sibling, ContentResponseError)

    cycle = {
        "page.subpage": {
            "@subpages": "true",
            "title": "Root again",
            "path": {"#text": ROOT},
        }
    }
    await run_with_subpages(cycle, ContentResponseError)


@pytest.mark.parametrize(
    ("max_depth", "max_pages"),
    [(-1, 10), (17, 10), (True, 10), (1, 0), (1, 1_001), (1, False)],
)
@pytest.mark.asyncio
async def test_invalid_toc_limits_make_no_request(
    tmp_path: Path, max_depth: object, max_pages: object
) -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return json_response({})

    async with adapter(tmp_path, handler) as content:
        with pytest.raises(ContentLimitError):
            await content.fetch_toc(  # type: ignore[arg-type]
                ROOT, max_depth=max_depth, max_pages=max_pages
            )
    assert calls == 0


@pytest.mark.asyncio
async def test_closed_adapter_fails_without_transport(tmp_path: Path) -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return json_response({})

    content = adapter(tmp_path, handler)
    await content.aclose()
    with pytest.raises(ContentTransportError):
        await content.fetch_toc(ROOT, max_depth=0)
    assert calls == 0


def test_adapter_rechecks_hard_pins_even_for_unvalidated_settings(
    tmp_path: Path,
) -> None:
    invalid_host = Settings.model_construct(
        cxone_host="evil.example",
        sandbox_root=ROOT,
        server_key="key",
        server_secret="secret",
        server_user="user",
        cxone_env_file=tmp_path / "none",
    )
    with pytest.raises(ContentConfigurationError):
        CXoneSandboxContentAdapter(invalid_host)

    invalid_root = Settings.model_construct(
        cxone_host="dev.libretexts.org",
        sandbox_root="Sandboxes/someone-else",
        server_key="key",
        server_secret="secret",
        server_user="user",
        cxone_env_file=tmp_path / "none",
    )
    with pytest.raises(ContentConfigurationError):
        CXoneSandboxContentAdapter(invalid_root)
