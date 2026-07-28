from __future__ import annotations

import json
import hashlib
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.computation import ExpressionNode
from app.computation_workflow import render_expression_tex
from app.math_text import (
    SourceMathReferences,
    canonicalize_server_owned_preview,
    strip_segment_markers,
    validate_generated_math_text,
)
from app.schemas import (
    GeneratedCritique,
    AssessmentItemType,
    BloomLevel,
    Choice,
    Difficulty,
    GeneratedQuestionDraft,
    ItemResponse,
    ParameterVariable,
    ParameterizedItemSpec,
)


def _generated(**updates: object) -> GeneratedQuestionDraft:
    values: dict[str, object] = {
        "item_type": AssessmentItemType.MULTIPLE_CHOICE,
        "concept_label": "Characteristic roots",
        "stem": r"Which expression is \(\alpha_1\)?",
        "choices": [
            Choice(id="A", text=r"\(\alpha_1\)", correct=True),
            Choice(id="B", text=r"\(\alpha_2\)", correct=False),
            Choice(id="C", text=r"\(k_1\)", correct=False),
            Choice(id="D", text=r"\(k_2\)", correct=False),
        ],
        "response": ItemResponse(),
        "explanation": r"The first root is \(\alpha_1\).",
        "bloom": BloomLevel.APPLY,
        "difficulty": Difficulty.MEDIUM,
        "citation_paragraphs": [1],
    }
    values.update(updates)
    return GeneratedQuestionDraft(**values)


@pytest.mark.parametrize(
    "bad",
    [
        r"Use \alpha to label the root.",
        "Use e^(alpha*x) as the trial solution.",
        "Compare k_1^2 with the discriminant.",
        "Compute sqrt(k_1).",
        "Choose [SEG1]the equation[/SEG1].",
        r"Use \(\require{html}x\).",
        "<strong>Choose this</strong>",
    ],
)
def test_generated_question_rejects_malformed_math_and_markup(bad: str) -> None:
    with pytest.raises(ValidationError):
        _generated(stem=bad)


@pytest.mark.parametrize(
    "value",
    [
        r"\(\alpha_1\)",
        r"\[\alpha^2 + k_1\alpha + k_2 = 0\]",
        r"Compare \(\sqrt{k_1}\) with \(e^{\alpha x}\).",
    ],
)
def test_canonical_delimiters_are_accepted(value: str) -> None:
    validate_generated_math_text(value, field_name="example")


@pytest.mark.parametrize(
    "value",
    [r"\(\alpha_1", r"\alpha_1\)", r"\(\alpha_1\[x\]\)"],
)
def test_delimiter_balance_is_enforced(value: str) -> None:
    with pytest.raises(ValueError):
        validate_generated_math_text(value, field_name="example")


def test_parameter_machine_fields_are_excluded_from_human_math_lint() -> None:
    spec = ParameterizedItemSpec(
        engine="webwork",
        variables=[ParameterVariable(name="x", minimum=1, maximum=3, step=1)],
        prompt_template="Evaluate {x}.",
        answer_expression="x ** 2",
        explanation_template="Square {x}.",
    )
    draft = GeneratedQuestionDraft(
        item_type=AssessmentItemType.WEBWORK,
        concept_label="Powers",
        stem=r"Evaluate the displayed quantity \(x^2\).",
        response=ItemResponse(parameterized=spec),
        explanation=r"Use the exponent in \(x^2\).",
        bloom=BloomLevel.APPLY,
        difficulty=Difficulty.MEDIUM,
        citation_paragraphs=[1],
    )
    assert draft.response.parameterized.answer_expression == "x ** 2"


def test_source_equation_references_resolve_without_exposing_controls() -> None:
    html = (
        r"<p>\[\alpha^2+k_1\alpha+k_2=0\label{eq:aux}\]</p>"
        r"<p>Equation \eqref{eq:aux} has two roots.</p>"
    )
    references = SourceMathReferences.from_html(html)
    equation = references.present(r"\[\alpha^2+k_1\alpha+k_2=0\label{eq:aux}\]")
    prose = references.present(r"Equation \eqref{eq:aux} has two roots.")
    unresolved = references.present(r"See \ref{eq:missing}.")
    assert r"\tag{1}" in equation
    assert r"\label" not in equation
    assert "Equation (1)" in prose
    assert "unavailable" in unresolved
    assert r"\ref" not in unresolved


def test_segment_markers_are_removed_only_from_presentation() -> None:
    stored = r"[SEG1]\(\alpha^2\)[/SEG1]"
    assert strip_segment_markers(stored) == r"\(\alpha^2\)"
    assert "[SEG1]" in stored


def test_typed_computation_ast_has_deterministic_tex() -> None:
    node = ExpressionNode(
        kind="div",
        args=[
            ExpressionNode(kind="symbol", symbol="alpha_1"),
            ExpressionNode(
                kind="pow",
                args=[
                    ExpressionNode(kind="symbol", symbol="x"),
                    ExpressionNode(kind="integer", integer=2),
                ],
            ),
        ],
    )
    assert render_expression_tex(node) == (
        r"\frac{\alpha_{\mathrm{1}}}{"
        r"\left(\mathrm{x}\right)^{2}}"
    )


def test_mathjax_integrity_manifest_covers_every_vendored_file() -> None:
    root = Path("app/static/vendor/mathjax")
    manifest = json.loads((root / "integrity.json").read_text())
    listed = {entry["path"] for entry in manifest["files"]}
    actual = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and path.name != "integrity.json"
    }
    assert manifest["mathjax_version"] == "4.1.0"
    assert manifest["font_version"] == "4.1.0"
    assert manifest["mhchem_font_version"] == "4.1.0"
    assert manifest["license"] == "Apache-2.0"
    assert listed == actual
    for entry in manifest["files"]:
        body = (root / entry["path"]).read_bytes()
        assert len(body) == entry["bytes"]
        assert hashlib.sha256(body).hexdigest() == entry["sha256"]
    assert "runtime/tex-chtml.js" in listed
    assert "runtime/a11y/assistive-mml.js" in listed
    assert "runtime/input/tex/extensions/empheq.js" in listed
    assert "runtime/input/tex/extensions/enclose.js" in listed
    assert "mathjax-mhchem-font-extension/chtml/woff2/mjx-mhc-m.woff2" in listed
    assert any(path.endswith(".woff2") for path in listed)


def test_mathjax_packages_are_exactly_pinned() -> None:
    lock = json.loads(Path("package-lock.json").read_text())
    packages = lock["packages"]
    assert packages["node_modules/mathjax"]["version"] == "4.1.0"
    assert packages["node_modules/@mathjax/mathjax-newcm-font"]["version"] == "4.1.0"
    assert (
        packages["node_modules/@mathjax/mathjax-mhchem-font-extension"]["version"]
        == "4.1.0"
    )
    assert packages["node_modules/mathjax"]["license"] == "Apache-2.0"
    assert (
        packages["node_modules/mathjax"]["integrity"]
        == "sha512-53eDXzxk40pS2sdI6KDCPoreY95ADaGygbi41ExKmn3FYQ+QIdpquIU90eppecelzQjf74kpScyeplVPccnIJw=="
    )
    assert packages["node_modules/@mathjax/mathjax-newcm-font"]["integrity"] == (
        "sha512-n10AwYubUa2hyOzxSRzkwRrgCVns083zkentryXICMPKaWT/"
        "watfvK2sUk5D9Bow9mpDfoqb5EWApuUvqnlzaw=="
    )
    assert packages["node_modules/@mathjax/mathjax-mhchem-font-extension"][
        "integrity"
    ] == (
        "sha512-1FWrDbsHmcp3AoGl3lD/7Q9ZeIss6DzU/owcSScCmjFLp8twC1horSB"
        "HiVhbHrAF+ewP2pMXkK3N4nWqEjK01A=="
    )


def test_renderer_is_same_origin_safe_and_csp_allows_only_required_styles() -> None:
    config = Path("app/static/mathjax-config.js").read_text()
    base = Path("app/templates/base.html").read_text()
    caddy = Path("deploy/Caddyfile.assess-ai").read_text()
    assert "https://" not in config
    assert 'inlineMath: [["\\\\(", "\\\\)"]]' in config
    assert 'displayMath: [["\\\\[", "\\\\]"]]' in config
    assert '"[-]": ["autoload", "configmacros", "newcommand", "require"]' in config
    assert 'URLs: "none"' in config
    assert 'font: "mathjax-newcm"' in config
    assert 'fontPath: "/static/vendor/mathjax/mathjax-newcm-font"' in config
    assert '"/static/vendor/mathjax/mathjax-mhchem-font-extension"' in config
    assert "enableMenu: false" in config
    assert "enableSpeech: false" in config
    assert "enableBraille: false" in config
    assert "enableAssistiveMml: true" in config
    assert "/static/vendor/mathjax/runtime/tex-chtml.js" in base
    assert "script-src 'self'" in caddy
    assert "font-src 'self'" in caddy
    assert "connect-src 'self'" in caddy
    assert "style-src 'self' 'unsafe-inline'" in caddy
    assert "https:" not in caddy


def test_corpus_renderer_is_display_only_and_excludes_machine_fields() -> None:
    dockerfile = Path("Dockerfile.corpus").read_text()
    bootstrap = Path("app/static/corpus-math-bootstrap.js").read_text()
    base = Path("deploy/corpus/base.html").read_text()

    assert "FROM ${CORPUS_BASE_IMAGE}" in dockerfile
    assert "app/main.py" not in dockerfile
    assert "app/db.py" not in dockerfile
    assert "corpus-math-bootstrap.js" in base
    assert 'element.querySelector("pre, code")' in bootstrap
    assert 'element.matches("pre, code, select, textarea")' in bootstrap
    assert "innerHTML" not in bootstrap
    assert "textContent" in bootstrap
    assert "SEGMENT_MARKER" in bootstrap
    assert "canonicalizeServerPreview" in bootstrap
    assert ".parameter-preview-table tbody td:not(:first-child)" in bootstrap


def test_server_owned_engine_preview_gets_display_only_canonical_tex() -> None:
    prompt = (
        "Consider k1 = 6 where k1^2 - 4*k2 = 0. The solution is "
        "y(x) = (a + b*x)*e^(alpha*x). Calculate alpha."
    )
    explanation = (
        "The roots are alpha +/- i * beta and alpha = -k1 / 2. "
        "Substituting k1 = 6 yields alpha = -6 / 2."
    )

    rendered_prompt = canonicalize_server_owned_preview(prompt)
    rendered_explanation = canonicalize_server_owned_preview(explanation)
    rendered_answer = canonicalize_server_owned_preview("-3.0")

    for rendered in (rendered_prompt, rendered_explanation, rendered_answer):
        validate_generated_math_text(rendered, field_name="engine_preview")
    assert "e^(" not in rendered_prompt
    assert "k1^2" not in rendered_prompt
    assert r"\(y(x) = (a + bx)e^{\alpha x}\)" in rendered_prompt
    assert r"\(k_1^2 - 4k_2 = 0\)" in rendered_prompt
    assert r"\(\alpha \pm i\beta\)" in rendered_explanation
    assert r"\(\alpha = -\frac{k_1}{2}\)" in rendered_explanation
    assert rendered_answer == r"\(-3.0\)"


def test_critique_prose_may_name_answer_choices() -> None:
    """Reviewer prose is internal and must not be held to the math contract.

    A critique naming a choice -- "CHOICE_A is not a plausible distractor" --
    trips the identifier_subscript heuristic, which reads CHOICE_A as an
    identifier with a subscript. That failed entire generation jobs: the
    critique stage retried three times and the job died, with an error that
    blamed pseudo-math for what was ordinary prose.
    """

    critique = GeneratedCritique(
        issues=["CHOICE_A is not a plausible distractor"],
        distractor_flags=["CHOICE_1 restates the stem"],
        revision_instructions=["Replace CHOICE_B with a common misconception"],
        revision_required=True,
    )
    assert critique.issues == ["CHOICE_A is not a plausible distractor"]


def test_critique_still_rejects_real_contract_violations() -> None:
    """Relaxing the heuristics must not disable the checks that matter."""

    for kwargs in (
        {"issues": ["Use $x$ instead"]},  # dollar delimiters
        {"issues": ["<b>bold</b> is not allowed"]},  # raw HTML
        {"distractor_flags": ["\\frac{1}{2} is unbalanced"]},  # TeX outside delimiters
    ):
        payload = {
            "issues": [],
            "distractor_flags": [],
            "revision_instructions": [],
            "revision_required": True,
        }
        payload.update(kwargs)
        with pytest.raises(ValidationError):
            GeneratedCritique(**payload)


def test_student_facing_text_still_rejects_ascii_pseudo_math() -> None:
    """The heuristics stay on for anything a student reads."""

    with pytest.raises(ValueError, match="ASCII pseudo-math"):
        validate_generated_math_text("k_1 doubles the rate", field_name="stem")
