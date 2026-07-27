"""Tests for the demo assistant.

The assistant is demo-support code, but it renders model output into a page and
persists what testers type, so the interesting cases here are the boundaries:
the flag, identity, origin, size, rate, provider failure, and reviewer scoping.
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator, Sequence
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.assistant import assistant_status
from app.assistant.corpus import CorpusError, corpus_files, corpus_text
from app.assistant.context import page_context
from app.assistant.llm import (
    AssistantLLMError,
    ChatTurn,
    ChatUsage,
    GeminiChatClient,
    OllamaChatClient,
    build_chat_client,
)
from app.assistant.prompt import (
    build_turn_context,
    compose_question,
    runtime_facts,
    static_system_prompt,
)
from app.assistant.service import AssistantRateLimited, AssistantService, RateLimiter
from app.assistant.store import AssistantStore, create_schema
from app.config import Settings
from app.db import DraftRepository, DraftWrite, init_database
from app.main import create_app
from app.schemas import (
    BloomLevel,
    Choice,
    Concept,
    Critique,
    Difficulty,
    NormalizedPage,
    Paragraph,
    QuestionDraft,
    SourceInfo,
)

REVIEWER = {"X-Reviewer": "tester@libretexts.dev"}
ORIGIN = {"Origin": "http://testserver"}


def settings(tmp_path: Path, **overrides) -> Settings:
    base = {
        "_env_file": None,
        "database_url": f"sqlite:///{tmp_path / 'app.db'}",
        "allowed_origin": "http://testserver",
        "assistant_enabled": True,
        "llm_provider_order": "gemini",
        "gemini_api_key": SecretStr("test-key"),
        "ollama_api_key": None,
    }
    base.update(overrides)
    return Settings(**base)


def page() -> NormalizedPage:
    text = "Energy changes form but is conserved."
    return NormalizedPage(
        title="Energy",
        plaintext=text,
        htmlBody=f"<p>{text}</p>",
        paragraphs=[Paragraph(index=0, text=text, start=0, end=len(text))],
        source=SourceInfo(
            backend="libretexts_public",
            canonical_url="https://chem.libretexts.org/Bookshelves/Test/Page",
            path="chem.libretexts.org/Bookshelves/Test/Page",
            page_id="86187",
        ),
    )


def question() -> QuestionDraft:
    return QuestionDraft(
        concept_label="Energy conservation",
        stem="Which statement about energy is accurate?",
        choices=[
            Choice(id="A", text="It is conserved.", correct=True),
            Choice(id="B", text="It disappears.", correct=False),
            Choice(id="C", text="It is matter.", correct=False),
            Choice(id="D", text="It has no units.", correct=False),
        ],
        explanation="The source says energy changes form while remaining conserved.",
        bloom=BloomLevel.UNDERSTAND,
        difficulty=Difficulty.EASY,
        citation_paragraphs=[0],
    )


def seed(repository: DraftRepository) -> int:
    stored = repository.replace_generated_drafts(
        page=page(),
        pipeline_version="test-v1",
        drafts=[
            DraftWrite(
                position=0,
                concept=Concept(
                    label="Energy conservation",
                    description="Energy changes form without disappearing.",
                    source_paragraphs=[0],
                ),
                raw=question(),
                critique=Critique(
                    issues=["The initial stem was vague."],
                    revision_required=True,
                ),
                revised=question(),
            )
        ],
        llm_calls=[],
    )
    return stored.draft_ids[0]


class FakeChatClient:
    """Emits a fixed set of deltas so stream shape can be asserted exactly."""

    provider_name = "fake"
    model = "fake-model"

    def __init__(
        self, pieces: Sequence[str] = ("Hello", " there"), *, fail: bool = False
    ):
        self.pieces = list(pieces)
        self.fail = fail
        self.closed = False
        self.system = ""
        self.history: list[ChatTurn] = []
        self.question = ""
        self.last_usage: ChatUsage | None = ChatUsage(
            prompt=5_200, cached=5_000, thoughts=0, output=40, total=5_240
        )

    async def stream(self, *, system, history, question) -> AsyncIterator[str]:
        self.system = system
        self.history = list(history)
        self.question = question
        for index, piece in enumerate(self.pieces):
            if self.fail and index == 1:
                raise AssistantLLMError("provider exploded")
            yield piece

    async def aclose(self) -> None:
        self.closed = True


# --------------------------------------------------------------------------
# Corpus
# --------------------------------------------------------------------------


def test_corpus_loads_every_file():
    files = corpus_files()
    assert len(files) >= 5
    text = corpus_text()
    for path in files:
        assert path.name in text


def test_corpus_carries_no_secrets():
    """The corpus is shown verbatim to anyone who can reach the app."""

    text = corpus_text()
    forbidden = [
        r"API_KEY\s*=\s*\S",
        r"SECRET\s*=\s*\S",
        r"PASSWORD\s*=\s*\S",
        r"\$2[aby]\$\d\d\$",  # bcrypt hash
        r"BEGIN [A-Z ]*PRIVATE KEY",
        r"\b(?:10|127)\.\d{1,3}\.\d{1,3}\.\d{1,3}\b",
        r"\b192\.168\.\d{1,3}\.\d{1,3}\b",
        r"\b185\.211\.\d{1,3}\.\d{1,3}\b",
    ]
    for pattern in forbidden:
        assert not re.search(pattern, text), f"corpus matched {pattern}"


def test_corpus_error_for_missing_directory(monkeypatch):
    monkeypatch.setattr(
        "app.assistant.corpus.CORPUS_DIR", Path("/nonexistent-corpus-dir")
    )
    with pytest.raises(CorpusError):
        corpus_files()


# --------------------------------------------------------------------------
# Flag behaviour
# --------------------------------------------------------------------------


def test_flag_off_removes_routes_and_launcher(tmp_path):
    app = create_app(settings(tmp_path, assistant_enabled=False))
    with TestClient(app) as client:
        home = client.get("/")
        assert "assistant-launcher" not in home.text
        assert (
            client.get("/assistant/conversation", headers=REVIEWER).status_code == 404
        )
        assert (
            client.post("/assistant/message", json={"question": "hi"}).status_code
            == 404
        )


def test_flag_on_renders_launcher(tmp_path):
    app = create_app(settings(tmp_path))
    with TestClient(app) as client:
        home = client.get("/")
        assert "assistant-launcher" in home.text
        assert "Demo Assistant" in home.text
        assert "not a source of record" in home.text


def test_healthz_reports_assistant(tmp_path):
    app = create_app(settings(tmp_path))
    with TestClient(app) as client:
        assert client.get("/healthz").json()["assistant"] == "enabled"

    off = create_app(settings(tmp_path, assistant_enabled=False))
    with TestClient(off) as client:
        assert client.get("/healthz").json()["assistant"] == "disabled"


def test_assistant_status_without_a_provider(tmp_path):
    misconfigured = settings(tmp_path, gemini_api_key=None)
    assert assistant_status(misconfigured) == "misconfigured"


# --------------------------------------------------------------------------
# Guards
# --------------------------------------------------------------------------


def _app_with_fake(tmp_path, fake: FakeChatClient, **overrides):
    resolved = settings(tmp_path, **overrides)
    app = create_app(resolved)

    original_lifespan_state: dict = {}

    def install(client: TestClient) -> None:
        service = client.app.state.assistant_service
        service._client_factory = lambda: fake  # noqa: SLF001 - test seam
        original_lifespan_state["service"] = service

    return app, install


def test_missing_reviewer_identity_is_refused(tmp_path):
    app = create_app(settings(tmp_path))
    with TestClient(app) as client:
        assert client.get("/assistant/conversation").status_code == 403
        assert (
            client.post(
                "/assistant/message", json={"question": "hi"}, headers=ORIGIN
            ).status_code
            == 403
        )


def test_cross_origin_is_refused(tmp_path):
    app = create_app(settings(tmp_path))
    with TestClient(app) as client:
        response = client.post(
            "/assistant/message",
            json={"question": "hi"},
            headers={**REVIEWER, "Origin": "https://evil.example"},
        )
        assert response.status_code == 403


def test_oversized_question_is_refused(tmp_path):
    fake = FakeChatClient()
    app, install = _app_with_fake(tmp_path, fake, assistant_max_message_chars=100)
    with TestClient(app) as client:
        install(client)
        response = client.post(
            "/assistant/message",
            json={"question": "x " * 400},
            headers={**REVIEWER, **ORIGIN},
        )
        assert response.status_code == 422


def test_blank_question_is_refused(tmp_path):
    app = create_app(settings(tmp_path))
    with TestClient(app) as client:
        response = client.post(
            "/assistant/message",
            json={"question": "   "},
            headers={**REVIEWER, **ORIGIN},
        )
        assert response.status_code == 422


# --------------------------------------------------------------------------
# Streaming
# --------------------------------------------------------------------------


def _frames(text: str) -> list[tuple[str, str]]:
    frames = []
    for block in text.split("\n\n"):
        if not block.strip():
            continue
        name = ""
        data = ""
        for line in block.split("\n"):
            if line.startswith("event:"):
                name = line[6:].strip()
            elif line.startswith("data:"):
                data += line[5:].strip()
        frames.append((name, json.loads(data) if data else ""))
    return frames


def test_stream_emits_deltas_then_done(tmp_path):
    fake = FakeChatClient(["Bloom ", "level ", "means..."])
    app, install = _app_with_fake(tmp_path, fake)
    with TestClient(app) as client:
        install(client)
        response = client.post(
            "/assistant/message",
            json={"question": "what is a Bloom level?"},
            headers={**REVIEWER, **ORIGIN},
        )
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")

        frames = _frames(response.text)
        assert [name for name, _ in frames] == ["delta", "delta", "delta", "done"]
        assert "".join(payload for name, payload in frames if name == "delta") == (
            "Bloom level means..."
        )
    assert fake.closed


def test_provider_failure_emits_error_and_records_it(tmp_path):
    fake = FakeChatClient(["partial", "boom"], fail=True)
    app, install = _app_with_fake(tmp_path, fake)
    with TestClient(app) as client:
        install(client)
        response = client.post(
            "/assistant/message",
            json={"question": "explain hints"},
            headers={**REVIEWER, **ORIGIN},
        )
        names = [name for name, _ in _frames(response.text)]
        assert "error" in names
        assert "done" not in names

        # The half-written answer must not be stored as though it completed.
        history = client.get("/assistant/conversation", headers=REVIEWER).json()
        roles = [message["role"] for message in history["messages"]]
        assert roles == ["user"]


def test_empty_completion_is_reported(tmp_path):
    fake = FakeChatClient([""])
    app, install = _app_with_fake(tmp_path, fake)
    with TestClient(app) as client:
        install(client)
        response = client.post(
            "/assistant/message",
            json={"question": "say nothing"},
            headers={**REVIEWER, **ORIGIN},
        )
        assert "error" in [name for name, _ in _frames(response.text)]


# --------------------------------------------------------------------------
# Persistence and scoping
# --------------------------------------------------------------------------


def test_conversation_persists_and_is_reviewer_scoped(tmp_path):
    fake = FakeChatClient(["answered"])
    app, install = _app_with_fake(tmp_path, fake)
    with TestClient(app) as client:
        install(client)
        client.post(
            "/assistant/message",
            json={"question": "first question"},
            headers={**REVIEWER, **ORIGIN},
        )

        mine = client.get("/assistant/conversation", headers=REVIEWER).json()
        assert [message["role"] for message in mine["messages"]] == [
            "user",
            "assistant",
        ]
        assert mine["messages"][0]["content"] == "first question"
        assert mine["messages"][1]["content"] == "answered"

        other = client.get(
            "/assistant/conversation", headers={"X-Reviewer": "someone-else"}
        ).json()
        assert other["messages"] == []


def test_reset_starts_a_fresh_conversation(tmp_path):
    fake = FakeChatClient(["answered"])
    app, install = _app_with_fake(tmp_path, fake)
    with TestClient(app) as client:
        install(client)
        client.post(
            "/assistant/message",
            json={"question": "first"},
            headers={**REVIEWER, **ORIGIN},
        )
        assert (
            client.post("/assistant/reset", headers={**REVIEWER, **ORIGIN}).status_code
            == 200
        )
        assert (
            client.get("/assistant/conversation", headers=REVIEWER).json()["messages"]
            == []
        )


def test_history_is_trimmed_to_max_turns(tmp_path):
    database = init_database(f"sqlite:///{tmp_path / 'trim.db'}")
    create_schema(database.engine)
    store = AssistantStore(database.session_factory)
    resolved = settings(tmp_path, assistant_max_turns=4)
    service = AssistantService(resolved, store, None, client_factory=FakeChatClient)

    conversation_id = store.ensure_conversation("someone", title="t")
    for index in range(10):
        store.append(
            reviewer="someone",
            conversation_id=conversation_id,
            role="user",
            content=f"question {index}",
        )

    # 4 turns is 8 messages. This assertion used to read `== 4`, which encoded
    # the bug: the setting was passed straight through as a row limit.
    history = service.history("someone")
    assert len(history) == 8
    assert history[-1]["content"] == "question 9"
    database.dispose()


def test_append_rejects_a_foreign_conversation(tmp_path):
    database = init_database(f"sqlite:///{tmp_path / 'scope.db'}")
    create_schema(database.engine)
    store = AssistantStore(database.session_factory)
    mine = store.ensure_conversation("owner", title="t")
    with pytest.raises(LookupError):
        store.append(
            reviewer="intruder", conversation_id=mine, role="user", content="hello"
        )
    database.dispose()


# --------------------------------------------------------------------------
# Rate limiting
# --------------------------------------------------------------------------


def test_rate_limiter_windows_per_reviewer():
    now = [0.0]
    limiter = RateLimiter(2, clock=lambda: now[0])

    limiter.check("a")
    limiter.check("a")
    with pytest.raises(AssistantRateLimited):
        limiter.check("a")

    # A different reviewer has their own allowance.
    limiter.check("b")

    # The window rolls forward.
    now[0] = 61.0
    limiter.check("a")


def test_rate_limited_request_reports_through_the_stream(tmp_path):
    fake = FakeChatClient(["ok"])
    app, install = _app_with_fake(tmp_path, fake, assistant_rate_limit_per_minute=1)
    with TestClient(app) as client:
        install(client)
        headers = {**REVIEWER, **ORIGIN}
        client.post("/assistant/message", json={"question": "one"}, headers=headers)
        second = client.post(
            "/assistant/message", json={"question": "two"}, headers=headers
        )
        names = [name for name, _ in _frames(second.text)]
        assert names == ["error"]


# --------------------------------------------------------------------------
# Page context
# --------------------------------------------------------------------------


def test_draft_route_context_includes_status_and_critique(tmp_path):
    database = init_database(f"sqlite:///{tmp_path / 'ctx.db'}")
    repository = DraftRepository(database)
    draft_id = seed(repository)

    context = page_context(f"/drafts/{draft_id}", repository)
    assert f"draft {draft_id}" in context
    assert "Status: ready_for_review" in context
    assert "The initial stem was vague." in context
    assert "Bloom level: understand" in context
    assert "Never published." in context
    database.dispose()


def test_home_route_context_summarizes_the_queue(tmp_path):
    database = init_database(f"sqlite:///{tmp_path / 'queue.db'}")
    repository = DraftRepository(database)
    seed(repository)
    context = page_context("/", repository)
    assert "draft queue" in context.casefold()
    assert "1 drafts" in context
    database.dispose()


def test_unknown_and_malformed_routes_yield_no_context(tmp_path):
    database = init_database(f"sqlite:///{tmp_path / 'none.db'}")
    repository = DraftRepository(database)
    for route in ["/nope", "not-a-route", "", "/drafts/999999", "/drafts/abc"]:
        assert page_context(route, repository) == ""
    database.dispose()


def test_draft_context_ignores_query_strings(tmp_path):
    database = init_database(f"sqlite:///{tmp_path / 'qs.db'}")
    repository = DraftRepository(database)
    draft_id = seed(repository)
    assert page_context(f"/drafts/{draft_id}?notice=saved", repository) != ""
    database.dispose()


# --------------------------------------------------------------------------
# Prompt assembly
# --------------------------------------------------------------------------


def test_runtime_facts_report_live_flag_state(tmp_path):
    resolved = settings(tmp_path, hint_generation_enabled=True)
    facts = runtime_facts(resolved, None, provider="gemini", model="gemini-3.6-flash")
    assert "Hint generation: on" in facts
    assert "Advanced item types: off" in facts
    assert "gemini-3.6-flash" in facts


def test_static_prefix_is_byte_identical_across_requests(tmp_path):
    """Prefix caching depends on this. If it drifts, caching silently stops."""

    first = static_system_prompt()
    second = static_system_prompt()
    assert first == second
    assert "Demo Assistant" in first
    assert "REFERENCE MATERIAL" in first
    # The model must be told it cannot act, or it will claim it did.
    assert "no tools" in first
    # Nothing deployment- or request-specific may leak into the cached prefix.
    for volatile in ("RUNTIME FACTS", "Draft queue", "WHAT THE USER IS LOOKING AT"):
        assert volatile not in first, f"{volatile} would break prefix caching"


def test_volatile_content_rides_on_the_question_not_the_prefix(tmp_path):
    resolved = settings(tmp_path)
    turn = build_turn_context(
        resolved,
        None,
        provider="gemini",
        model="gemini-3.6-flash",
        page_context="The reviewer is looking at draft 7.",
    )
    assert "RUNTIME FACTS" in turn
    assert "WHAT THE USER IS LOOKING AT" in turn
    assert "draft 7" in turn

    composed = compose_question(turn, "why is it blocked?")
    assert composed.endswith("why is it blocked?")
    assert "=== QUESTION ===" in composed
    # With no context the question is sent bare, so short exchanges stay cheap.
    assert compose_question("", "hello") == "hello"


def test_turn_context_omits_the_page_section_when_there_is_none(tmp_path):
    turn = build_turn_context(settings(tmp_path), None, provider="g", model="m")
    assert "WHAT THE USER IS LOOKING AT" not in turn


# --------------------------------------------------------------------------
# Provider selection and transport
# --------------------------------------------------------------------------


def test_build_chat_client_follows_provider_order(tmp_path):
    gemini = build_chat_client(settings(tmp_path, llm_provider_order="gemini"))
    assert isinstance(gemini, GeminiChatClient)

    ollama = build_chat_client(
        settings(
            tmp_path,
            llm_provider_order="ollama",
            ollama_base_url="http://localhost:11434",
        )
    )
    assert isinstance(ollama, OllamaChatClient)


def test_build_chat_client_skips_an_unready_provider(tmp_path):
    resolved = settings(
        tmp_path,
        llm_provider_order="gemini,ollama",
        gemini_api_key=None,
        ollama_base_url="http://localhost:11434",
    )
    assert isinstance(build_chat_client(resolved), OllamaChatClient)


def test_build_chat_client_fails_when_nothing_is_ready(tmp_path):
    resolved = settings(tmp_path, llm_provider_order="gemini", gemini_api_key=None)
    with pytest.raises(AssistantLLMError):
        build_chat_client(resolved)


@pytest.mark.anyio
async def test_gemini_client_parses_sse_deltas(tmp_path):
    body = (
        'data: {"candidates":[{"content":{"parts":[{"text":"Hello"}]}}]}\n\n'
        'data: {"candidates":[{"content":{"parts":[{"text":" world"}]}}]}\n\n'
        "data: [DONE]\n\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert "streamGenerateContent" in str(request.url)
        return httpx.Response(200, text=body)

    client = GeminiChatClient(
        settings(tmp_path), transport=httpx.MockTransport(handler)
    )
    pieces = [
        piece async for piece in client.stream(system="s", history=[], question="q")
    ]
    assert pieces == ["Hello", " world"]
    await client.aclose()


@pytest.mark.anyio
async def test_gemini_client_raises_on_http_error(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="unavailable")

    client = GeminiChatClient(
        settings(tmp_path), transport=httpx.MockTransport(handler)
    )
    with pytest.raises(AssistantLLMError):
        async for _ in client.stream(system="s", history=[], question="q"):
            pass
    await client.aclose()


@pytest.mark.anyio
async def test_ollama_client_parses_ndjson_deltas(tmp_path):
    body = (
        '{"message":{"role":"assistant","content":"Self"}}\n'
        '{"message":{"role":"assistant","content":"-hosted"}}\n'
        '{"done":true}\n'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url).endswith("/api/chat")
        return httpx.Response(200, text=body)

    client = OllamaChatClient(
        settings(tmp_path, ollama_base_url="http://localhost:11434"),
        transport=httpx.MockTransport(handler),
    )
    pieces = [
        piece async for piece in client.stream(system="s", history=[], question="q")
    ]
    assert pieces == ["Self", "-hosted"]
    await client.aclose()


@pytest.fixture
def anyio_backend():
    return "asyncio"


# --------------------------------------------------------------------------
# Panel placement and colour
#
# These exist because the panel originally rendered *inside* .site-header,
# which sets `color: #fff`. Assistant replies inherited it and came out white
# on a white panel -- invisible unless you selected the text. Markup-presence
# tests all passed while the feature was unreadable.
# --------------------------------------------------------------------------


def test_panel_is_not_nested_inside_the_dark_site_header(tmp_path):
    app = create_app(settings(tmp_path))
    with TestClient(app) as client:
        html = client.get("/").text

    start = html.index('<header class="site-header">')
    header_block = html[start : html.index("</header>", start)]

    assert "assistant-launcher" in header_block, "the launcher belongs in the header"
    assert 'id="assistant-panel"' not in header_block, (
        "the panel must not sit inside .site-header -- it inherits color:#fff there"
    )
    assert 'id="assistant-panel"' in html


def test_panel_and_reply_text_declare_their_own_colour():
    """Neither may rely on inheriting a readable colour from an ancestor."""

    css = (
        Path(__file__).resolve().parents[1] / "app" / "static" / "styles.css"
    ).read_text()

    for selector in (".assistant-panel {", ".assistant-text {"):
        start = css.index(selector)
        rule = css[start : css.index("}", start)]
        assert "color:" in rule, f"{selector} must state an explicit color"


def test_a_failed_queue_lookup_omits_the_line_rather_than_claiming_empty(tmp_path):
    """A broken lookup and an empty queue are different facts.

    These lines are handed to the model as ground truth, so collapsing the two
    made the assistant say "the queue is empty" with confidence when it simply
    could not read the queue.
    """

    class BrokenRepository:
        def list_drafts(self, *args, **kwargs):
            raise RuntimeError("database is unreachable")

    facts = runtime_facts(
        settings(tmp_path), BrokenRepository(), provider="gemini", model="m"
    )
    assert "Draft queue" not in facts
    # The rest of the facts must survive the failure.
    assert "Hint generation:" in facts


# --------------------------------------------------------------------------
# Concurrency: one active conversation per reviewer
# --------------------------------------------------------------------------


def test_only_one_active_conversation_survives_a_concurrent_first_turn(tmp_path):
    """The check-then-insert race must be settled by the database, not by luck.

    Two first-turn requests that both observe "no conversation yet" used to both
    insert, silently splitting one tester's transcript across two rows.
    """

    database = init_database(f"sqlite:///{tmp_path / 'race.db'}")
    create_schema(database.engine)
    store = AssistantStore(database.session_factory)

    from sqlalchemy.exc import IntegrityError

    from app.assistant.store import AssistantConversation

    # Simulate the loser of the race: a competing row is already committed by
    # the time our insert reaches the database.
    with database.session_factory() as rival:
        rival.add(AssistantConversation(reviewer="racer", active=True, title="rival"))
        rival.commit()
        winner_id = rival.query(AssistantConversation).one().id

    adopted = store.ensure_conversation("racer", title="mine")
    assert adopted == winner_id, "the loser must adopt the winner's conversation"

    # And the index genuinely forbids a second active row.
    with pytest.raises(IntegrityError):
        with database.session_factory() as session:
            session.add(
                AssistantConversation(reviewer="racer", active=True, title="second")
            )
            session.commit()
    database.dispose()


def test_reset_retires_the_old_conversation_and_keeps_it(tmp_path):
    database = init_database(f"sqlite:///{tmp_path / 'reset.db'}")
    create_schema(database.engine)
    store = AssistantStore(database.session_factory)

    from app.assistant.store import AssistantConversation

    first = store.ensure_conversation("someone", title="first")
    store.append(
        reviewer="someone", conversation_id=first, role="user", content="hello"
    )
    second = store.start_conversation("someone")

    assert second != first
    assert store.active_conversation_id("someone") == second
    assert store.messages("someone") == [], "the new conversation starts empty"

    # The retired conversation is kept as a record, not deleted.
    with database.session_factory() as session:
        rows = (
            session.query(AssistantConversation)
            .order_by(AssistantConversation.id)
            .all()
        )
        assert [row.active for row in rows] == [False, True]
    database.dispose()


def test_migration_adds_active_to_a_pre_existing_table(tmp_path):
    """A database written by the first release must upgrade in place."""

    from sqlalchemy import inspect as sa_inspect, text as sa_text

    database = init_database(f"sqlite:///{tmp_path / 'old.db'}")
    with database.engine.begin() as connection:
        connection.execute(
            sa_text(
                "CREATE TABLE assistant_conversations ("
                " id INTEGER PRIMARY KEY, reviewer VARCHAR(255) NOT NULL,"
                " title VARCHAR(120) NOT NULL DEFAULT '',"
                " created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)"
            )
        )
        # Two rows that would violate the new index unless the migration
        # retires the older one first.
        for name in ("a", "b"):
            connection.execute(
                sa_text(
                    "INSERT INTO assistant_conversations "
                    "(reviewer, title, created_at, updated_at) "
                    f"VALUES ('dup', '{name}', '2026-01-01', '2026-01-01')"
                )
            )

    create_schema(database.engine)

    columns = {
        column["name"]
        for column in sa_inspect(database.engine).get_columns("assistant_conversations")
    }
    assert "active" in columns

    store = AssistantStore(database.session_factory)
    assert store.active_conversation_id("dup") is not None
    with database.engine.connect() as connection:
        active = connection.execute(
            sa_text(
                "SELECT COUNT(*) FROM assistant_conversations "
                "WHERE reviewer='dup' AND active=1"
            )
        ).scalar()
    assert active == 1, "duplicates must be retired down to one active row"

    # Idempotent: running it again changes nothing and does not error.
    create_schema(database.engine)
    database.dispose()


# --------------------------------------------------------------------------
# Turn semantics, template limit, and the reset guard
# --------------------------------------------------------------------------


def test_max_turns_counts_exchanges_not_rows(tmp_path):
    """ "Turns" must mean question-and-answer pairs, not stored messages."""

    database = init_database(f"sqlite:///{tmp_path / 'turns.db'}")
    create_schema(database.engine)
    store = AssistantStore(database.session_factory)
    resolved = settings(tmp_path, assistant_max_turns=3)
    service = AssistantService(resolved, store, None, client_factory=FakeChatClient)

    conversation_id = store.ensure_conversation("someone", title="t")
    for index in range(10):
        store.append(
            reviewer="someone",
            conversation_id=conversation_id,
            role="user" if index % 2 == 0 else "assistant",
            content=f"message {index}",
        )

    history = service.history("someone")
    assert len(history) == 6, "3 turns is 6 messages"
    assert history[-1]["content"] == "message 9"
    database.dispose()


def test_textarea_limit_follows_the_configured_maximum(tmp_path):
    app = create_app(settings(tmp_path, assistant_max_message_chars=1234))
    with TestClient(app) as client:
        assert 'maxlength="1234"' in client.get("/").text


def test_reset_button_checks_the_response_before_clearing():
    """A refused reset must not blank the panel the server still holds."""

    script = (
        Path(__file__).resolve().parents[1] / "app" / "static" / "assistant.js"
    ).read_text()
    reset_block = script[
        script.index("resetButton.addEventListener") : script.index(
            "const consumeFrames"
        )
    ]
    assert "response.ok" in reset_block, (
        "fetch resolves on 403/503, so the reset handler must inspect the status"
    )


# --------------------------------------------------------------------------
# Cost control
#
# Three things drive the bill: thinking tokens (billed as output), runaway
# answers, and resending the same prefix uncached on every turn. The first two
# were unbounded until these tests existed.
# --------------------------------------------------------------------------


@pytest.mark.anyio
async def test_gemini_request_caps_thinking_and_output(tmp_path):
    """Left unset these inherit the model default, which is the expensive path."""

    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(
            200,
            text='data: {"candidates":[{"content":{"parts":[{"text":"hi"}]}}]}\n\n',
        )

    client = GeminiChatClient(
        settings(tmp_path, assistant_max_output_tokens=512),
        transport=httpx.MockTransport(handler),
    )
    async for _ in client.stream(system="s", history=[], question="q"):
        pass
    await client.aclose()

    config = seen["generationConfig"]
    assert config["maxOutputTokens"] == 512
    assert config["thinkingConfig"] == {"thinkingLevel": "minimal"}


@pytest.mark.anyio
async def test_gemini_usage_is_captured_including_cache_hits(tmp_path):
    body = (
        'data: {"candidates":[{"content":{"parts":[{"text":"hi"}]}}]}\n\n'
        'data: {"usageMetadata":{"promptTokenCount":5300,'
        '"cachedContentTokenCount":5215,"thoughtsTokenCount":0,'
        '"candidatesTokenCount":42,"totalTokenCount":5342}}\n\n'
    )

    client = GeminiChatClient(
        settings(tmp_path),
        transport=httpx.MockTransport(lambda r: httpx.Response(200, text=body)),
    )
    async for _ in client.stream(system="s", history=[], question="q"):
        pass
    usage = client.last_usage
    await client.aclose()

    assert usage is not None
    assert usage.prompt == 5300
    assert usage.cached == 5215
    assert usage.output == 42
    # What actually gets charged at full prompt rate.
    assert usage.billed_prompt == 85


@pytest.mark.anyio
async def test_ollama_request_caps_output(tmp_path):
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(
            200,
            text='{"message":{"content":"hi"}}\n'
            '{"done":true,"prompt_eval_count":100,"eval_count":7}\n',
        )

    client = OllamaChatClient(
        settings(
            tmp_path,
            ollama_base_url="http://localhost:11434",
            assistant_max_output_tokens=333,
        ),
        transport=httpx.MockTransport(handler),
    )
    async for _ in client.stream(system="s", history=[], question="q"):
        pass
    usage = client.last_usage
    await client.aclose()

    assert seen["options"]["num_predict"] == 333
    assert usage is not None and usage.prompt == 100 and usage.output == 7


def test_usage_is_persisted_with_the_answer(tmp_path):
    fake = FakeChatClient(["answered"])
    app, install = _app_with_fake(tmp_path, fake)
    with TestClient(app) as client:
        install(client)
        client.post(
            "/assistant/message",
            json={"question": "what does bloom mean?"},
            headers={**REVIEWER, **ORIGIN},
        )
        store = client.app.state.assistant_service._store
        totals = store.usage_totals()

    assert totals["answers"] == 1
    assert totals["prompt_tokens"] == 5_200
    assert totals["cached_tokens"] == 5_000
    # The number that actually costs money.
    assert totals["billed_prompt_tokens"] == 200


def test_the_cached_prefix_does_not_change_between_turns(tmp_path):
    """Two questions must send a byte-identical system prompt.

    If the prefix drifts, the provider cannot serve it from cache and every
    turn pays full price for the whole corpus.
    """

    fake = FakeChatClient(["ok"])
    app, install = _app_with_fake(tmp_path, fake)
    with TestClient(app) as client:
        install(client)
        headers = {**REVIEWER, **ORIGIN}
        client.post(
            "/assistant/message",
            json={"question": "first", "route": "/"},
            headers=headers,
        )
        first_prefix = fake.system
        client.post(
            "/assistant/message",
            json={"question": "second", "route": "/drafts/1"},
            headers=headers,
        )
        second_prefix = fake.system

    assert first_prefix == second_prefix, "the cached prefix must not vary by route"
    assert "REFERENCE MATERIAL" in first_prefix
    # Volatile content must be on the question instead.
    assert "RUNTIME FACTS" in fake.question


def test_replayed_history_stays_bare_so_the_prefix_keeps_growing(tmp_path):
    """Stored turns must not carry the volatile block they were sent with.

    Otherwise turn N's replay differs from what was sent, the prefix diverges,
    and caching collapses back to the system prompt alone.
    """

    fake = FakeChatClient(["ok"])
    app, install = _app_with_fake(tmp_path, fake)
    with TestClient(app) as client:
        install(client)
        headers = {**REVIEWER, **ORIGIN}
        client.post(
            "/assistant/message",
            json={"question": "first", "route": "/drafts/1"},
            headers=headers,
        )
        client.post("/assistant/message", json={"question": "second"}, headers=headers)

    replayed = [turn.content for turn in fake.history]
    assert "first" in replayed
    for content in replayed:
        assert "RUNTIME FACTS" not in content
        assert "=== QUESTION ===" not in content


def test_queue_counts_are_not_paid_for_twice_on_the_home_page(tmp_path):
    """The home page context already prints the tally; facts must not repeat it."""

    database = init_database(f"sqlite:///{tmp_path / 'dupe.db'}")
    repository = DraftRepository(database)
    seed(repository)
    resolved = settings(tmp_path)

    home = build_turn_context(
        resolved,
        repository,
        provider="g",
        model="m",
        page_context=page_context("/", repository),
        queue_described_elsewhere=True,
    )
    assert home.count("Draft queue") + home.count("Queue:") == 1

    # On a draft page the queue is not in the context, so the facts carry it.
    draft = build_turn_context(
        resolved,
        repository,
        provider="g",
        model="m",
        page_context="",
        queue_described_elsewhere=False,
    )
    assert "Draft queue" in draft
    database.dispose()


def test_counting_drafts_does_not_load_them(tmp_path):
    """A tally must not drag five eager relationships per row along with it."""

    database = init_database(f"sqlite:///{tmp_path / 'count.db'}")
    repository = DraftRepository(database)
    seed(repository)

    assert repository.count_drafts_by_status() == {"ready_for_review": 1}

    calls: list[str] = []
    original = repository.list_drafts
    repository.list_drafts = lambda *a, **k: (  # type: ignore[method-assign]
        calls.append("list_drafts"),
        original(*a, **k),
    )[1]

    from app.assistant.prompt import queue_summary

    assert "1 ready_for_review" in queue_summary(repository)
    assert page_context("/", repository) != ""
    assert calls == [], "neither path may fall back to loading every draft"
    database.dispose()


def _stylesheet_without_comments():
    """The stylesheet with `/* ... */` removed.

    These tests locate rules by searching the raw text, and the stylesheet's
    comments cross-reference the very selectors being searched for — they
    document this `[hidden]` trap by name. Left in, a comment mentioning a
    selector is found ahead of the real rule, so a perfectly correct
    stylesheet fails. Search the declarations only.
    """

    return re.sub(
        r"/\*.*?\*/",
        "",
        (
            Path(__file__).resolve().parents[1] / "app" / "static" / "styles.css"
        ).read_text(),
        flags=re.DOTALL,
    )


def test_the_panel_can_actually_be_hidden():
    """The hidden attribute must beat the panel's own display declaration.

    The UA stylesheet's `[hidden] { display: none }` loses to any author
    `display` rule. `.assistant-panel` sets `display: flex`, so without a more
    specific override the attribute did nothing: the panel sat over every page
    permanently and the close button looked broken. The codebase already had the
    right pattern in `.generation-status[hidden]`.
    """

    css = _stylesheet_without_comments()

    base = css.index(".assistant-panel { position: fixed")
    override = css.index(".assistant-panel[hidden]")
    rule = css[override : css.index("}", override)]

    assert "display: none" in rule
    # Specificity of .assistant-panel[hidden] (0,2,0) already wins, but keeping
    # it after the base rule means it wins on source order too.
    assert override > base, "the override must follow the rule it overrides"


def test_every_toggled_assistant_element_has_a_hidden_override():
    """Any assistant element the script toggles via `hidden` needs the override.

    Generalised so a future panel, drawer, or tooltip cannot reintroduce the
    same bug by declaring `display` without a matching `[hidden]` rule.
    """

    root = Path(__file__).resolve().parents[1] / "app"
    css = _stylesheet_without_comments()
    script = (root / "static" / "assistant.js").read_text()

    # Elements the script hides by attribute, mapped to their CSS class.
    toggled = {"#assistant-panel": ".assistant-panel"}

    for selector, css_class in toggled.items():
        assert f'querySelector("{selector}")' in script
        base_rule_start = css.index(f"{css_class} {{")
        base_rule = css[base_rule_start : css.index("}", base_rule_start)]
        if "display:" in base_rule:
            assert f"{css_class}[hidden]" in css, (
                f"{css_class} declares display, so it needs a "
                f"{css_class}[hidden] override or `hidden` will not work"
            )
