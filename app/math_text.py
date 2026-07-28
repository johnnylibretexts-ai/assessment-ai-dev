from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Any


CANONICAL_MATH_INSTRUCTIONS = r"""
Math notation contract for every human-facing string (including stems, stimuli,
choices, feedback, matching/matrix/highlight text, explanations, misconceptions,
critique, and hints):
- Put inline TeX only inside \( ... \) and display TeX only inside \[ ... \].
- Never use $ or $$ delimiters, naked TeX commands, ASCII pseudo-math such as
  e^(...), sqrt(...), x**2, alpha_1, or k_1^2, HTML, or [SEG1] markers.
- Do not emit \require, links, images, HTML/style/class macros, macro definitions,
  or other executable/dynamic TeX.
- This contract does not apply to numeric/scoring fields, parameter placeholders,
  answer expressions, or WeBWorK/IMathAS machine templates.
Canonical examples: "The roots are \(\alpha_1\) and \(\alpha_2\).";
"\[\alpha^2 + k_1\alpha + k_2 = 0\]".
""".strip()


_SEGMENT_MARKER_RE = re.compile(r"\[/?SEG\d+\]", re.IGNORECASE)
_HTML_RE = re.compile(r"</?[A-Za-z][^>]*>")
_FORBIDDEN_MACRO_RE = re.compile(
    r"\\(?:"
    r"require|href|url|includegraphics|html\w*|class|style|cssId|"
    r"newcommand|renewcommand|providecommand|def|gdef|xdef|edef|let"
    r")\b",
    re.IGNORECASE,
)
_RAW_TEX_RE = re.compile(r"\\[A-Za-z]+")
_ASCII_MATH_PATTERNS = (
    re.compile(r"\*\*"),
    re.compile(r"\b(?:sqrt|exp|sin|cos|tan|log|ln)\s*\(", re.IGNORECASE),
    re.compile(r"\be\s*\^\s*\(", re.IGNORECASE),
    re.compile(r"\b[A-Za-z][A-Za-z0-9]*_\{?[A-Za-z0-9]"),
    re.compile(r"\b[A-Za-z][A-Za-z0-9]*\s*\^\s*\{?[-A-Za-z0-9]"),
)
_DISPLAY_MATH_RE = re.compile(r"\\\[(.*?)\\\]", re.DOTALL)
_LABEL_RE = re.compile(r"\\label\s*\{([^{}]+)\}")
_TAG_RE = re.compile(r"\\tag\*?\s*\{([^{}]+)\}")
_NONUMBER_RE = re.compile(r"\\(?:nonumber|notag)\b")
_REF_RE = re.compile(r"\\(eqref|ref)\s*\{([^{}]+)\}")


def strip_segment_markers(value: str) -> str:
    """Remove legacy model control markers without interpreting HTML."""

    return _SEGMENT_MARKER_RE.sub("", value)


def canonicalize_server_owned_preview(value: str) -> str:
    """Render legacy native-engine preview prose without mutating its evidence.

    Fixed-seed previews are deterministic server output derived from immutable
    WeBWorK/IMathAS templates. Older records predate the human-facing TeX
    contract, so this bounded presentation adapter replaces their known ASCII
    notation while the stored template, seed, answer, and evidence hashes stay
    unchanged.
    """

    text = strip_segment_markers(value)
    protected: dict[str, str] = {}

    def protect(rendered: str) -> str:
        token = f"\x00MATH{len(protected)}\x00"
        protected[token] = rendered
        return token

    text = re.sub(
        r"\\\((?:\\.|[^\\])*?\\\)|\\\[(?:\\.|[^\\])*?\\\]",
        lambda match: protect(match.group(0)),
        text,
    )
    text = re.sub(
        r"\by\(x\)\s*=\s*\(a\s*\+\s*b\s*\*\s*x\)\s*\*\s*"
        r"e\s*\^\s*\(\s*alpha\s*\*\s*x\s*\)",
        lambda _: protect(r"\(y(x) = (a + bx)e^{\alpha x}\)"),
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"\bk1(?:\*\*|\^)\s*2\s*-\s*4\s*\*\s*k2\s*(<=|>=|=|<|>)\s*0",
        lambda match: protect(rf"\(k_1^2 - 4k_2 {match.group(1)} 0\)"),
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"\balpha\s*\+/-\s*i\s*\*\s*beta\b",
        lambda _: protect(r"\(\alpha \pm i\beta\)"),
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"\balpha\s*=\s*-k1\s*/\s*2\b",
        lambda _: protect(r"\(\alpha = -\frac{k_1}{2}\)"),
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"\balpha\s*=\s*(-?\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)\b",
        lambda match: protect(
            rf"\(\alpha = \frac{{{match.group(1)}}}{{{match.group(2)}}}\)"
        ),
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"\bk([12])\s*=\s*(-?\d+(?:\.\d+)?)\b",
        lambda match: protect(rf"\(k_{match.group(1)} = {match.group(2)}\)"),
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"\be\s*\^\s*\(\s*alpha\s*\*\s*x\s*\)",
        lambda _: protect(r"\(e^{\alpha x}\)"),
        text,
        flags=re.IGNORECASE,
    )
    for pattern, rendered in (
        (r"\balpha\b", r"\(\alpha\)"),
        (r"\bbeta\b", r"\(\beta\)"),
        (r"\bk1\b", r"\(k_1\)"),
        (r"\bk2\b", r"\(k_2\)"),
    ):
        text = re.sub(
            pattern,
            lambda _, replacement=rendered: protect(replacement),
            text,
            flags=re.IGNORECASE,
        )
    if re.fullmatch(r"\s*-?\d+(?:\.\d+)?\s*", text):
        text = protect(rf"\({text.strip()}\)")

    for token, rendered in protected.items():
        text = text.replace(token, rendered)
    return text


def validate_generated_math_text(
    value: str, *, field_name: str, check_ascii_math: bool = True
) -> None:
    """Enforce the provider-facing math contract for one human-readable field.

    ``check_ascii_math`` gates only the pseudo-math heuristics. They are
    deliberately aggressive, which is right for text a student will read and
    wrong for internal reviewer prose -- see ``validate_critique_math``.
    """

    if not value:
        return
    value = value.replace("[[computed_task]]", "").replace("[[computed_result]]", "")
    if _SEGMENT_MARKER_RE.search(value):
        raise ValueError(f"{field_name} contains a segment control marker")
    if _HTML_RE.search(value):
        raise ValueError(f"{field_name} contains HTML")
    if "$" in value:
        raise ValueError(f"{field_name} uses unsupported dollar math delimiters")
    forbidden = _FORBIDDEN_MACRO_RE.search(value)
    if forbidden:
        raise ValueError(
            f"{field_name} contains forbidden TeX macro {forbidden.group(0)}"
        )

    outside, error = _outside_math_text(value)
    if error:
        raise ValueError(f"{field_name} {error}")
    raw_tex = _RAW_TEX_RE.search(outside)
    if raw_tex:
        raise ValueError(
            f"{field_name} contains TeX outside canonical delimiters: "
            f"{raw_tex.group(0)}"
        )
    if not check_ascii_math:
        return
    for pattern in _ASCII_MATH_PATTERNS:
        match = pattern.search(outside)
        if match:
            raise ValueError(
                f"{field_name} contains ASCII pseudo-math outside canonical "
                f"delimiters: {match.group(0)}"
            )


def validate_question_math(question: Any) -> None:
    for field_name, value in iter_question_human_text(question):
        validate_generated_math_text(value, field_name=field_name)


def validate_hint_math(ladder: Any) -> None:
    for index, rung in enumerate(ladder.rungs):
        validate_generated_math_text(
            rung.text,
            field_name=f"rungs[{index}].text",
        )


def validate_critique_math(critique: Any) -> None:
    """Validate reviewer prose.

    A critique is internal: it is never shown to a student, never rendered as
    math, and never reaches QTI. Reviewers routinely name answer choices --
    "CHOICE_A is not a plausible distractor" -- and the identifier_subscript
    heuristic reads CHOICE_A as an identifier with a subscript, which failed
    the whole generation job. Every real contract check (HTML, dollar
    delimiters, forbidden macros, delimiter balance, raw TeX) still applies.
    """

    for field in ("issues", "distractor_flags", "revision_instructions"):
        for index, value in enumerate(getattr(critique, field)):
            validate_generated_math_text(
                value,
                field_name=f"{field}[{index}]",
                check_ascii_math=False,
            )


def iter_question_human_text(question: Any) -> Iterator[tuple[str, str]]:
    """Yield display prose only; machine/scoring and parameter fields stay excluded."""

    for field in (
        "stem",
        "stimulus",
        "explanation",
        "targeted_misconception",
    ):
        value = getattr(question, field, None)
        if value:
            yield field, value

    yield from _choice_texts("choices", getattr(question, "choices", ()))
    response = question.response
    for index, pair in enumerate(response.matching_pairs):
        yield f"response.matching_pairs[{index}].prompt", pair.prompt
        yield f"response.matching_pairs[{index}].target", pair.target
    if response.image_alt:
        yield "response.image_alt", response.image_alt
    for index, region in enumerate(response.hotspot_regions):
        yield f"response.hotspot_regions[{index}].label", region.label
    for index, segment in enumerate(response.highlight_segments):
        yield f"response.highlight_segments[{index}].text", segment.text
    yield from _choice_texts("response.matrix_columns", response.matrix_columns)
    for index, row in enumerate(response.matrix_rows):
        yield f"response.matrix_rows[{index}].text", row.text
    for field in (
        "bow_tie_actions",
        "bow_tie_condition",
        "bow_tie_parameters",
    ):
        group = getattr(response, field)
        if group is not None:
            yield from _choice_texts(f"response.{field}.choices", group.choices)


def _choice_texts(prefix: str, choices: Iterable[Any]) -> Iterator[tuple[str, str]]:
    for index, choice in enumerate(choices):
        yield f"{prefix}[{index}].text", choice.text
        if choice.feedback:
            yield f"{prefix}[{index}].feedback", choice.feedback


def _outside_math_text(value: str) -> tuple[str, str | None]:
    outside: list[str] = []
    index = 0
    open_kind: str | None = None
    while index < len(value):
        token = value[index : index + 2]
        if open_kind is None:
            if token in {r"\)", r"\]"}:
                return "", f"contains unmatched closing delimiter {token}"
            if token in {r"\(", r"\["}:
                open_kind = token
                index += 2
                continue
            outside.append(value[index])
            index += 1
            continue
        expected = r"\)" if open_kind == r"\(" else r"\]"
        if token == expected:
            open_kind = None
            index += 2
            continue
        if token in {r"\(", r"\["}:
            return "", f"contains nested math delimiter {token}"
        index += 1
    if open_kind is not None:
        return "", f"contains unclosed math delimiter {open_kind}"
    return "".join(outside), None


@dataclass(frozen=True)
class SourceMathReferences:
    labels: dict[str, str]

    @classmethod
    def from_html(cls, html_body: str) -> "SourceMathReferences":
        labels: dict[str, str] = {}
        equation_number = 0
        for match in _DISPLAY_MATH_RE.finditer(html_body):
            expression = match.group(1)
            label_names = _LABEL_RE.findall(expression)
            if _NONUMBER_RE.search(expression):
                continue
            equation_number += 1
            if not label_names:
                continue
            tag = _TAG_RE.search(expression)
            if tag:
                rendered_number = tag.group(1).strip()
            else:
                rendered_number = str(equation_number)
            for label in label_names:
                labels[label.strip()] = rendered_number
        return cls(labels=labels)

    def present(self, value: str) -> str:
        """Resolve stored source equation controls for partial-page rendering."""

        clean = strip_segment_markers(value)

        def replace_reference(match: re.Match[str]) -> str:
            kind, label = match.groups()
            number = self.labels.get(label.strip())
            if number is None:
                return f'[equation reference "{label.strip()}" unavailable]'
            return f"({number})" if kind == "eqref" else number

        clean = _REF_RE.sub(replace_reference, clean)
        clean = _NONUMBER_RE.sub("", clean)

        def replace_label(match: re.Match[str]) -> str:
            number = self.labels.get(match.group(1).strip())
            return rf"\tag{{{number}}}" if number else ""

        return _LABEL_RE.sub(replace_label, clean)
