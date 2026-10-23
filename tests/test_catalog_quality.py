"""Offline quality evidence never starts installed apps or changes host settings."""

import itertools
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from universal_tools.catalog_quality import (
    COHERENCE,
    MAX_BYTES,
    WEIGHTS,
    analyze_catalog,
    check_choices,
    compare_catalogs,
    evaluate_catalog,
    evaluate_choices,
    prepare_choices,
    prepare_review,
    score_review,
    snapshot,
)
from universal_tools.quality_cli import _read, main

PROFILE = "Synthetic offline supplied-content analysis; no native/file effects."
EVALUATOR = "fixture-reviewer/1; deterministic-test-settings/1"


@pytest.fixture
def catalog():
    return {
        "tools": [
            {
                "name": "inspect_text",
                "title": "Inspect supplied text",
                "description": "Inspect supplied text for line counts. Use for offline text only; "
                "returns a count without files, native software or network access.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string", "description": "Literal content to inspect."}
                    },
                    "required": ["text"],
                    "additionalProperties": False,
                },
                "outputSchema": {
                    "type": "object",
                    "properties": {
                        "lines": {
                            "type": "integer",
                            "description": "Count including the final line.",
                        }
                    },
                },
                "annotations": {"readOnlyHint": True, "openWorldHint": False},
            }
        ]
    }


def response(request, score=4):
    rating = {"score": score, "justification": "Fixture rating; not an actual model result."}
    return {
        "request_hash": request["request_hash"],
        "tools": [
            {
                "name": t["name"],
                "dimensions": {key: dict(rating) for key in WEIGHTS},
                "annotation_contradiction": False,
                "summary": "Synthetic response for validation tests only.",
            }
            for t in request["tools"]
        ],
        "coherence": {key: dict(rating) for key in COHERENCE},
    }


def test_detached_complete_profile_and_sibling_binding(catalog):
    report = analyze_catalog(catalog, profile=PROFILE)
    assert report["ok"] and report["counts"] == {"errors": 0, "warnings": 0}
    report["tools"][0]["description"] = "mutated returned report"
    assert "mutated" not in catalog["tools"][0]["description"]
    request = prepare_review(catalog, profile=PROFILE, evaluator=EVALUATOR)
    other = deepcopy(catalog["tools"][0])
    other["name"] = "count_text"
    catalog["tools"].append(other)
    assert (
        prepare_review(catalog, profile=PROFILE, evaluator=EVALUATOR)["request_hash"]
        != request["request_hash"]
    )
    forward = analyze_catalog(catalog, profile=PROFILE)
    catalog["tools"].reverse()
    assert analyze_catalog(catalog, profile=PROFILE)["catalog_hash"] == forward["catalog_hash"]


@pytest.mark.parametrize(
    "mutation,code",
    [
        (lambda t: t.pop("description"), "missing_description"),
        (lambda t: t.update(description="  "), "missing_description"),
        (lambda t: t.update(name="bad name"), "invalid_name"),
        (lambda t: t.update(name=["unhashable"]), "invalid_name"),
        (lambda t: t.update(description=123), "invalid_metadata"),
        (lambda t: t.update(inputSchema={"type": "array"}), "invalid_schema"),
        (lambda t: t.update(inputSchema={"type": "object", "required": "text"}), "invalid_schema"),
        (lambda t: t.update(annotations={"readOnlyHint": "true"}), "invalid_annotation"),
        (lambda t: t.update(annotations=[]), "invalid_annotations"),
        (
            lambda t: t.update(outputSchema={"type": "object", "$ref": "https://invalid/"}),
            "unsupported_reference",
        ),
        (
            lambda t: t.update(inputSchema={"type": "object", "$ref": "#/missing"}),
            "unsupported_reference",
        ),
    ],
)
def test_deterministic_errors(catalog, mutation, code):
    mutation(catalog["tools"][0])
    report = analyze_catalog(catalog, profile=PROFILE)
    assert not report["ok"]
    assert code in {f["code"] for f in report["findings"]}
    with pytest.raises(ValueError, match="deterministic"):
        prepare_review(catalog, profile=PROFILE, evaluator=EVALUATOR)


def test_local_recursive_refs_nested_docs_and_instance_examples(catalog):
    catalog["tools"][0]["inputSchema"] = {
        "type": "object",
        "$defs": {"node": {"type": "object", "properties": {"child": {"$ref": "#/$defs/node"}}}},
        "properties": {"node": {"$ref": "#/$defs/node"}},
        "examples": [{"$ref": "https://example.invalid/instance-not-schema"}],
    }
    report = analyze_catalog(catalog, profile=PROFILE)
    assert report["ok"]
    assert sum(f["code"] == "undocumented_field" for f in report["findings"]) == 2
    assert "score" not in report  # no fabricated static numeric grade


def test_optional_metadata_is_advisory_and_hint_defaults_are_not_effect_proof(catalog):
    tool = catalog["tools"][0]
    tool.pop("outputSchema")
    tool.pop("annotations")
    assert analyze_catalog(catalog, profile=PROFILE)["ok"]
    tool["annotations"] = {"readOnlyHint": True, "destructiveHint": True}
    assert analyze_catalog(catalog, profile=PROFILE)["ok"]  # ignored by MCP for readOnly


def test_duplicate_empty_and_paginated_catalogs(catalog):
    catalog["tools"] *= 2
    assert not analyze_catalog(catalog, profile=PROFILE)["ok"]
    assert analyze_catalog([], profile=PROFILE)["ok"]
    with pytest.raises(ValueError):
        prepare_review([], profile=PROFILE, evaluator=EVALUATOR)
    with pytest.raises(ValueError, match="pages"):
        analyze_catalog({"tools": [], "nextCursor": "more"}, profile=PROFILE)


@pytest.mark.parametrize(
    "value",
    [float("nan"), {1: "bad key"}, (1, 2), "x" * (MAX_BYTES + 1)],
    ids=["nonfinite", "key-type", "tuple", "oversize"],
)
def test_json_input_refusals(value):
    with pytest.raises(ValueError):
        snapshot(value)


def test_structure_budgets_before_serialization():
    cyclic = []
    cyclic.append(cyclic)
    with pytest.raises(ValueError, match="structure"):
        snapshot(cyclic)
    with pytest.raises(ValueError, match="structure"):
        snapshot([0] * 100_001)


@pytest.mark.parametrize("change", ["profile", "model", "sibling", "rubric"])
def test_review_invalidation(catalog, monkeypatch, change):
    request = prepare_review(catalog, profile=PROFILE, evaluator=EVALUATOR)
    answer = response(request)
    profile, evaluator = PROFILE, EVALUATOR
    if change == "profile":
        profile = "Different selected profile"
    elif change == "model":
        evaluator += "; changed decoding"
    elif change == "sibling":
        sibling = deepcopy(catalog["tools"][0])
        sibling["name"] = "other"
        catalog["tools"].append(sibling)
    else:
        monkeypatch.setattr("universal_tools.catalog_quality.RUBRIC", "fixture-new-rubric")
    with pytest.raises(ValueError, match="binding"):
        score_review(catalog, answer, profile=profile, evaluator=evaluator)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda r: r["tools"].clear(),
        lambda r: r["tools"].append(deepcopy(r["tools"][0])),
        lambda r: r["tools"][0].update(name="unknown"),
        lambda r: r["tools"][0]["dimensions"]["purpose_clarity"].update(score=True),
        lambda r: r["tools"][0]["dimensions"]["purpose_clarity"].update(score=5.0),
        lambda r: r["tools"][0]["dimensions"]["purpose_clarity"].update(score=6),
        lambda r: r["tools"][0]["dimensions"]["purpose_clarity"].update(justification=" "),
        lambda r: r.update(extra="ignored data must fail"),
    ],
)
def test_review_output_is_strict_and_complete(catalog, mutation):
    answer = response(prepare_review(catalog, profile=PROFILE, evaluator=EVALUATOR))
    mutation(answer)
    with pytest.raises(ValueError):
        score_review(catalog, answer, profile=PROFILE, evaluator=EVALUATOR)


def test_caps_integer_rounding_and_profile_specific_coherence(catalog):
    catalog["tools"][0]["description"] = "inspect_text"
    answer = response(prepare_review(catalog, profile=PROFILE, evaluator=EVALUATOR), score=5)
    answer["tools"][0]["annotation_contradiction"] = True
    scored = score_review(catalog, answer, profile=PROFILE, evaluator=EVALUATOR)
    tool = scored["tools"][0]
    assert tool["score"] == 3.5  # 345 hundredths rounds up, not bankers' 3.4
    assert tool["dimensions"]["purpose_clarity"]["score"] == 2
    assert tool["dimensions"]["behavioral_transparency"]["score"] == 1
    assert tool["flags"] == ["tautology", "annotation_contradiction"]
    assert scored["advisory_only"] and scored["coverage"] == 1
    assert "scope_fit" in scored["coherence"]


def test_all_score_vectors_round_half_up():
    from universal_tools.catalog_quality import _tenths

    for vector in itertools.product(range(1, 6), repeat=6):
        total = sum(a * b for a, b in zip(vector, WEIGHTS.values(), strict=True))
        assert _tenths(total, 100) == (total + 5) // 10


def test_evaluator_gets_detached_request_and_is_not_automatically_retried(catalog):
    calls = []

    def review(request):
        calls.append(request["request_hash"])
        answer = response(request)
        request["tools"].clear()
        return answer

    result = evaluate_catalog(catalog, review, profile=PROFILE, evaluator=EVALUATOR)
    assert result["overall_score"] == 4 and len(calls) == 1 and catalog["tools"]
    with pytest.raises(ValueError):
        evaluate_catalog(catalog, lambda _: {}, profile=PROFILE, evaluator=EVALUATOR)


def test_catalog_comparison(catalog):
    after = deepcopy(catalog)
    after["tools"][0]["description"] += " Extra usage context."
    report = compare_catalogs(catalog, after, profile=PROFILE)
    assert report["changed"] == ["inspect_text"]
    assert report["before_hash"] != report["after_hash"]
    assert not report["added"] and not report["removed"]


def test_selection_expectations_withheld_and_no_tool_case(catalog):
    cases = [
        {"id": "offline", "request": "Count these lines.", "acceptable_tools": ["inspect_text"]},
        {"id": "native", "request": "Start a controller.", "acceptable_tools": [None]},
    ]
    request = prepare_choices(catalog, cases, profile=PROFILE, evaluator=EVALUATOR)
    assert all("acceptable_tools" not in c for c in request["cases"])
    answer = {
        "request_hash": request["request_hash"],
        "choices": [{"id": "offline", "tool": "inspect_text"}, {"id": "native", "tool": None}],
    }
    result = evaluate_choices(
        catalog, cases, lambda _: answer, profile=PROFILE, evaluator=EVALUATOR
    )
    assert result["passed"] and result["correct"] == 2
    assert result["tool_execution_performed"] is False
    wrong = deepcopy(answer)
    wrong["choices"][1]["tool"] = "inspect_text"
    assert not check_choices(catalog, cases, wrong, profile=PROFILE, evaluator=EVALUATOR)["passed"]
    wrong["choices"][1]["tool"] = "fabricated"
    with pytest.raises(ValueError, match="unknown"):
        check_choices(catalog, cases, wrong, profile=PROFILE, evaluator=EVALUATOR)
    answer["choices"].append(answer["choices"][0])
    with pytest.raises(ValueError, match="coverage"):
        check_choices(catalog, cases, answer, profile=PROFILE, evaluator=EVALUATOR)


def test_cli_file_publication_is_caller_controlled(catalog, tmp_path, capsys):
    path = tmp_path / "catalog.json"
    path.write_bytes(json.dumps(catalog).encode())
    before = set(tmp_path.iterdir())
    assert main(["lint", str(path), "--profile", PROFILE]) == 0
    assert json.loads(capsys.readouterr().out)["ok"]
    assert set(tmp_path.iterdir()) == before
    path.write_bytes(b'{"tools":[],"tools":[]}')
    with pytest.raises(ValueError, match="duplicate"):
        _read(path)


async def test_real_sdk_definitions_without_serving_or_app_connections():
    from universal_tools.surface import create_surface

    class NeverInvoke:
        settings = SimpleNamespace(max_request_body_bytes=1024 * 1024)

        async def invoke(self, *args, **kwargs):
            pytest.fail("quality check must not invoke a tool")

    server = create_surface(NeverInvoke())
    listing = await server.list_tools()
    tools = [tool.model_dump(mode="json", by_alias=True, exclude_none=True) for tool in listing]
    report = analyze_catalog(tools, profile="Internal fixed discovery fixture; not a plugin.")
    assert len(report["tools"]) == 14 and report["ok"]
