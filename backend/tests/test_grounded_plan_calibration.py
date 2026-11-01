"""Verify exact quotation guards and non-forced uncertain component grading."""

from __future__ import annotations

import copy

import pytest

from research.grounded_plan_calibration import (
    COMPONENTS,
    GroundedOutput,
    validate_evidence,
)
from research.independent_plan_review import require_different_models


def _packet():
    return {
        "required_dimensions": [
            {"id": d} for d in ("share", "pricing", "policy", "growth")
        ],
        "required_components": copy.deepcopy(COMPONENTS),
        "tasks": [
            {
                "id": 1,
                "title": "市场",
                "intent": "研究品牌份额和价格竞争",
                "query": "份额 价格",
            },
            {
                "id": 2,
                "title": "政策",
                "intent": "研究政策影响和增长驱动",
                "query": "政策 驱动",
            },
        ],
    }


def _labels():
    sources = {
        "share.brand_share": (1, "品牌份额"),
        "pricing.price_competition": (1, "价格竞争"),
        "policy.policy_impact": (2, "政策影响"),
        "growth.drivers": (2, "增长驱动"),
    }
    return {
        "components": [
            {
                "component_id": c["id"],
                "status": "covered" if c["id"] in sources else "missing",
                "evidence": [
                    {
                        "task_id": sources[c["id"]][0],
                        "field": "intent",
                        "quote": sources[c["id"]][1],
                    }
                ]
                if c["id"] in sources
                else [],
                "reason": "Explicit text or absent goal",
            }
            for c in COMPONENTS
        ]
    }


def test_partial_components_do_not_turn_into_full_dimensions():
    result = validate_evidence(GroundedOutput.model_validate(_labels()), _packet())
    assert result["dimension_coverage"] == 0.5
    assert result["dimension_statuses"] == {
        "share": "covered",
        "pricing": "partial",
        "policy": "covered",
        "growth": "partial",
    }
    assert result["exact_quote_count"] == 4
    assert result["quote_identity_validated"]
    assert not result["semantic_quote_support_verified"]


@pytest.mark.parametrize(
    "defect",
    [
        "fabricated",
        "wrong_task",
        "wrong_field",
        "translation",
        "empty_evidence",
        "missing_component",
        "duplicate_component",
    ],
)
def test_unsupported_quotes_and_incomplete_labels_are_rejected(defect):
    raw = _labels()
    quote = raw["components"][0]["evidence"][0]
    if defect == "fabricated":
        quote["quote"] = "分析盈利压力"
    elif defect == "wrong_task":
        quote["task_id"] = 2
    elif defect == "wrong_field":
        quote["field"] = "title"
    elif defect == "translation":
        quote["quote"] = "brand market share"
    elif defect == "empty_evidence":
        raw["components"][0]["evidence"] = []
    elif defect == "missing_component":
        raw["components"].pop()
    else:
        raw["components"].append(copy.deepcopy(raw["components"][0]))
    with pytest.raises(ValueError):
        validate_evidence(GroundedOutput.model_validate(raw), _packet())


def test_uncertain_is_boundary_and_does_not_become_a_missing_label():
    raw = _labels()
    raw["components"][2]["status"] = "uncertain"
    result = validate_evidence(GroundedOutput.model_validate(raw), _packet())
    assert result["evaluation_set"] == "boundary"
    assert result["dimension_coverage"] is None
    assert result["uncertain_components"] == ["pricing.profit_pressure"]
    assert "pricing.profit_pressure" not in result["missing_components"]


def test_unrepresented_dimension_and_unknown_model_identity_fail():
    packet = _packet()
    packet["required_dimensions"].append({"id": "undefined"})
    with pytest.raises(ValueError, match="Every frozen dimension"):
        validate_evidence(GroundedOutput.model_validate(_labels()), packet)
    with pytest.raises(ValueError, match="identities"):
        require_different_models("unknown", "openai/gpt-oss-120b")


def test_exact_but_irrelevant_quote_is_not_claimed_as_semantically_verified():
    raw = _labels()
    raw["components"][2].update(
        status="covered",
        evidence=[{"task_id": 1, "field": "intent", "quote": "价格竞争"}],
    )
    result = validate_evidence(GroundedOutput.model_validate(raw), _packet())
    assert result["quote_identity_validated"]
    assert result["semantic_quote_support_verified"] is False
