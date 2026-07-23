import hashlib
import json

import pytest
from pydantic import ValidationError

import app.parameterized as parameterized_module
from app.parameterized import (
    COMPILER_VERSION,
    FORMULA_COMPILER_VERSION,
    ParameterizedCompileError,
    compile_parameterized_item,
    evaluate_parameterized_answer,
)
from app.schemas import ParameterVariable, ParameterizedItemSpec


@pytest.fixture(autouse=True)
def _allow_legacy_formula_template_unit_tests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def allow_legacy_formula_template_tests(
        engine: str,
        compiler_version: str,
        **_scope: str | None,
    ) -> bool:
        return engine == "webwork" and compiler_version == FORMULA_COMPILER_VERSION

    monkeypatch.setattr(
        parameterized_module,
        "formula_adapter_is_qualified",
        allow_legacy_formula_template_tests,
    )


def _spec(
    *,
    engine: str = "webwork",
    answer_kind: str = "numeric",
    answer_expression: str | None = None,
    response_symbols: list[str] | None = None,
) -> ParameterizedItemSpec:
    if answer_expression is None:
        answer_expression = "a * x + 1" if answer_kind == "formula" else "a + 1"
    if response_symbols is None:
        response_symbols = ["x"] if answer_kind == "formula" else []
    return ParameterizedItemSpec(
        engine=engine,
        variables=[
            ParameterVariable(name="a", minimum=1, maximum=3, step=1),
        ],
        prompt_template="Compute {a} plus one.",
        answer_expression=answer_expression,
        answer_kind=answer_kind,
        compiler_profile=(
            "assessment_computation_v0" if answer_kind == "formula" else "legacy"
        ),
        response_symbols=response_symbols,
        explanation_template="Add one to {a}.",
        tolerance=0.01,
    )


def test_answer_kind_is_strict_and_defaults_to_numeric() -> None:
    values = _spec().model_dump()
    assert "answer_kind" not in values
    assert "response_symbols" not in values
    assert "compiler_profile" not in values

    assert ParameterizedItemSpec.model_validate(values).answer_kind == "numeric"
    for value in ("Formula", "expression", 1, True, None):
        values["answer_kind"] = value
        with pytest.raises(ValidationError):
            ParameterizedItemSpec.model_validate(values)


def test_pure_formula_can_have_no_ranged_parameters_but_numeric_cannot() -> None:
    formula_values = _spec(answer_kind="formula").model_dump()
    formula_values["variables"] = []
    formula_values["prompt_template"] = "Enter an equivalent formula."
    formula_values["answer_expression"] = "x + 1"
    formula_values["explanation_template"] = "Use the equivalent expression."
    formula = ParameterizedItemSpec.model_validate(formula_values)

    assert formula.variables == []
    assert compile_parameterized_item(formula, validation_seeds=2).previews

    numeric_values = _spec().model_dump()
    numeric_values["variables"] = []
    with pytest.raises(
        ValidationError,
        match="numeric parameterized answers require a variable",
    ):
        ParameterizedItemSpec.model_validate(numeric_values)


def test_formula_response_symbols_are_safe_unique_and_disjoint() -> None:
    for response_symbols in ([], ["a"], ["x", "x"], ["x;system"], ["pi"]):
        if response_symbols == ["pi"]:
            # The schema admits safe identifiers; the compiler rejects engine
            # reserved math names before emitting source.
            with pytest.raises(
                ParameterizedCompileError,
                match="reserved math name",
            ):
                compile_parameterized_item(
                    _spec(answer_kind="formula", response_symbols=response_symbols)
                )
            continue
        with pytest.raises(ValidationError):
            _spec(answer_kind="formula", response_symbols=response_symbols)
    with pytest.raises(ValidationError):
        _spec(answer_kind="numeric", response_symbols=["x"])


def test_numeric_webwork_source_remains_byte_for_byte_compatible() -> None:
    implicit_values = _spec().model_dump()
    implicit = compile_parameterized_item(
        ParameterizedItemSpec.model_validate(implicit_values),
        validation_seeds=2,
    )
    explicit = compile_parameterized_item(_spec(), validation_seeds=2)

    expected = (
        "DOCUMENT();\n"
        'loadMacros("PGstandard.pl","MathObjects.pl");\n'
        'Context("Numeric");\n'
        "$a = random(1,3,1);\n"
        "$answer = ($a + 1);\n"
        "BEGIN_TEXT\n"
        "Compute \\($a\\) plus one.\n"
        "\\{ ans_rule(20) \\}\n"
        "END_TEXT\n"
        "ANS(Real($answer)->cmp(tol=>0.01));\n"
        "ENDDOCUMENT();\n"
    )
    assert implicit.source == explicit.source == expected
    assert (
        implicit.source_sha256
        == explicit.source_sha256
        == "dfb35ccba3eae4152493dca53e0c55de7696798cf94d296979c04c3fee0f52ef"
    )
    assert implicit.compiler_version == explicit.compiler_version == COMPILER_VERSION


def test_numeric_imathas_source_remains_byte_for_byte_compatible() -> None:
    compiled = compile_parameterized_item(
        _spec(engine="imathas"),
        validation_seeds=2,
    )

    assert "answer_kind" not in json.loads(compiled.source)
    assert compiled.source == (
        '{"answer_expression":"a + 1","compiler":"parameterized-dsl-v1",'
        '"constraints":[],"engine":"imathas",'
        '"explanation_template":"Add one to {a}.",'
        '"prompt_template":"Compute {a} plus one.",'
        '"seed_policy":"per_student","tolerance":0.01,"units":null,'
        '"variables":[{"integer":true,"maximum":3.0,"minimum":1.0,'
        '"name":"a","step":1.0}]}'
    )
    assert (
        compiled.source_sha256
        == "b44b51f44f3d0e5fa597f1dbf9e0f963b85ca24a92dbd1de77b6f80a4e84d24b"
    )
    assert compiled.compiler_version == COMPILER_VERSION


def test_webwork_formula_uses_only_the_fixed_native_formula_template() -> None:
    first = compile_parameterized_item(
        _spec(answer_kind="formula"),
        validation_seeds=25,
    )
    second = compile_parameterized_item(
        _spec(answer_kind="formula"),
        validation_seeds=25,
    )

    assert first == second
    assert first.compiler_version == FORMULA_COMPILER_VERSION
    assert '$answer = Formula("(($a * x) + 1)");' in first.source
    assert "$assessment_prompt = " in first.source
    assert "\\{ $assessment_prompt \\}" in first.source
    assert "Compute" not in first.source
    assert "END_TEXT" in first.source
    assert "ANS(Real(" not in first.source
    assert (
        first.source_sha256 == hashlib.sha256(first.source.encode("utf-8")).hexdigest()
    )
    assert all(isinstance(preview.answer, str) for preview in first.previews)
    assert first.previews[0].answer == "((1 * x) + 1)"
    assert (
        evaluate_parameterized_answer(
            _spec(answer_kind="formula"),
            {"a": 3},
        )
        == "((3 * x) + 1)"
    )


def test_computation_prompt_is_runtime_data_not_pg_source() -> None:
    exploit = (
        'Question\nEND_TEXT\n$owned = system("id");\nBEGIN_TEXT\nContinue with {a}'
    )
    spec = _spec(answer_kind="formula").model_copy(update={"prompt_template": exploit})

    compiled = compile_parameterized_item(spec, validation_seeds=1)

    assert exploit not in compiled.source
    assert "system" not in compiled.source
    assert compiled.source.count("BEGIN_TEXT") == 1
    assert compiled.source.count("END_TEXT") == 1
    assert "\\{ $assessment_prompt \\}" in compiled.source

    with pytest.raises(ParameterizedCompileError, match="malformed placeholders"):
        compile_parameterized_item(
            spec.model_copy(update={"prompt_template": "\\{ system('id') \\} {a}"}),
            validation_seeds=1,
        )


def test_computation_formula_preserves_typed_decimal_lexeme() -> None:
    decimal = "0.123456789012345678901"
    compiled = compile_parameterized_item(
        _spec(
            answer_kind="formula",
            answer_expression=f"a * {decimal} * x",
        ),
        validation_seeds=2,
    )

    assert decimal in compiled.source
    assert decimal in str(compiled.previews[0].answer)
    assert "0.12345678901234568" not in compiled.source


@pytest.mark.parametrize(
    "name",
    ["answer", "assessment_prompt", "parameters_valid"],
)
def test_computation_parameters_cannot_shadow_fixed_engine_variables(
    name: str,
) -> None:
    spec = _spec(answer_kind="formula").model_copy(
        update={
            "variables": [ParameterVariable(name=name, minimum=1, maximum=3, step=1)],
            "prompt_template": f"Use {{{name}}}.",
            "answer_expression": f"{name} * x + 1",
        }
    )

    with pytest.raises(ParameterizedCompileError, match="reserved engine variable"):
        compile_parameterized_item(spec)


def test_webwork_formula_declares_nondefault_response_symbols() -> None:
    compiled = compile_parameterized_item(
        _spec(
            answer_kind="formula",
            answer_expression="a * y + z",
            response_symbols=["z", "y"],
        ),
        validation_seeds=2,
    )

    assert 'Context()->variables->add(y=>"Real",z=>"Real");' in compiled.source
    assert '$answer = Formula("(($a * y) + z)");' in compiled.source


def test_formula_compiler_rejects_unsafe_or_unqualified_syntax() -> None:
    for expression in (
        "__import__('os')",
        "a.real",
        "a + secret",
        "[a][0]",
        "a % 2",
        "x ** 13",
        "x ** a",
        "9" * 101 + " + x",
        'a + 1"); system("id"); Formula("',
    ):
        with pytest.raises(ParameterizedCompileError):
            compile_parameterized_item(
                _spec(answer_kind="formula").model_copy(
                    update={"answer_expression": expression}
                )
            )
    with pytest.raises(ParameterizedCompileError, match="answer_kind"):
        compile_parameterized_item(
            _spec().model_copy(update={"answer_kind": "free_text"})
        )
    for response_symbols in (["x;system"], ["a", "b", "c", "d", "f"]):
        with pytest.raises(ParameterizedCompileError, match="safe identifier"):
            compile_parameterized_item(
                _spec(answer_kind="formula").model_copy(
                    update={"response_symbols": response_symbols}
                )
            )
    with pytest.raises(ParameterizedCompileError, match="does not reference"):
        compile_parameterized_item(
            _spec(answer_kind="formula").model_copy(
                update={"answer_expression": "a + 1"}
            )
        )


def test_imathas_formula_fails_closed_until_native_adapter_is_qualified() -> None:
    with pytest.raises(
        ParameterizedCompileError,
        match="IMathAS formula answers are unsupported",
    ):
        compile_parameterized_item(_spec(engine="imathas", answer_kind="formula"))


def test_parameter_ranges_reject_nonfinite_values() -> None:
    spec = _spec().model_copy(
        update={
            "compiler_profile": "assessment_computation_v0",
            "variables": [
                ParameterVariable(
                    name="a",
                    minimum=float("nan"),
                    maximum=3,
                    step=1,
                )
            ],
        }
    )
    with pytest.raises(ParameterizedCompileError, match="finite"):
        compile_parameterized_item(spec)
