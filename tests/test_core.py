"""Unit tests for the pure scoring/mapping helpers."""

import re

import pytest

from canvas_split_assignment.api import normalize_base_url
from canvas_split_assignment.errors import CanvasError
from canvas_split_assignment.models import CriterionClo
from canvas_split_assignment.scoring import (
    apply_penalty_evenly,
    build_clo_mapping,
    build_targets,
    clo_assignment_name,
    evaluate_submission,
    extract_clo,
    find_clo_variants,
    format_points,
    score_assessment,
)

PATTERN = re.compile(r"(CLO\s*\d+)")


def mapping() -> dict[str, CriterionClo]:
    return {
        "c1": CriterionClo("c1", "CLO1", 10.0, "CLO1 fundamentals"),
        "c2": CriterionClo("c2", "CLO2", 10.0, "CLO2 analysis"),
    }


def graded(score, rubric, **extra):
    return {"workflow_state": "graded", "score": score, "rubric_assessment": rubric, **extra}


# --------------------------------------------------------------------------- #
# extract_clo
# --------------------------------------------------------------------------- #


def test_extract_clo_with_group():
    assert extract_clo(PATTERN, "CLO3: does things") == "CLO3"


def test_extract_clo_without_group_returns_whole_match():
    assert extract_clo(re.compile(r"CLO\d+"), "see CLO3 here") == "CLO3"


def test_extract_clo_no_match():
    assert extract_clo(PATTERN, "teamwork") is None


def test_extract_clo_uses_first_group():
    assert extract_clo(re.compile(r"(CLO)\s*(\d+)"), "CLO 3") == "CLO"


def test_extract_clo_falls_back_to_whole_match_when_group_fails():
    assert extract_clo(re.compile(r"(CLO\d+)|OTHER"), "OTHER") == "OTHER"


def test_extract_clo_empty_capture_is_not_a_clo():
    assert extract_clo(re.compile(r"CLO(\s*)"), "CLO") is None


def test_extract_clo_handles_empty_text():
    assert extract_clo(PATTERN, "") is None
    assert extract_clo(PATTERN, None) is None


# --------------------------------------------------------------------------- #
# build_clo_mapping / build_targets
# --------------------------------------------------------------------------- #


def test_build_clo_mapping_maps_and_sums_points():
    criteria = [
        {"id": "c1", "points": 10, "description": "CLO1 fundamentals"},
        {"id": "c2", "points": 10, "description": "CLO2 analysis"},
    ]
    mapped, unmatched = build_clo_mapping(criteria, PATTERN)
    assert [c.clo for c in mapped] == ["CLO1", "CLO2"]
    assert mapped[0].points == 10.0
    assert unmatched == []


def test_build_clo_mapping_falls_back_to_long_description():
    criteria = [{"id": "c1", "points": 5, "description": "Teamwork", "long_description": "CLO2: analysis"}]
    mapped, unmatched = build_clo_mapping(criteria, PATTERN)
    assert [c.clo for c in mapped] == ["CLO2"]
    assert unmatched == []


def test_build_clo_mapping_reports_unmatched():
    criteria = [{"id": "c1", "points": 5, "description": "Teamwork"}]
    mapped, unmatched = build_clo_mapping(criteria, PATTERN)
    assert mapped == []
    assert unmatched == ["Teamwork"]


def test_build_targets_groups_by_clo_and_reuses_existing():
    mapped = [
        CriterionClo("c1", "CLO1", 10.0, "a"),
        CriterionClo("c2", "CLO1", 5.0, "b"),
        CriterionClo("c3", "CLO2", 8.0, "c"),
    ]
    targets, warnings = build_targets("Homework 1", mapped, {"Homework 1 - [CLO1]": 99})
    assert warnings == []
    assert [t.clo for t in targets] == ["CLO1", "CLO2"]
    assert targets[0].name == "Homework 1 - [CLO1]"
    assert targets[0].points_possible == 15.0
    assert targets[0].existing_assignment_id == 99
    assert targets[1].existing_assignment_id is None


def test_build_targets_warns_on_overlong_name():
    mapped = [CriterionClo("c1", "CLO1", 1.0, "a")]
    targets, warnings = build_targets("x" * 300, mapped, {})
    assert len(targets[0].name) == 255
    assert warnings and "truncated" in warnings[0]


def test_clo_assignment_name():
    assert clo_assignment_name("Homework 1", "CLO1") == "Homework 1 - [CLO1]"


def test_find_clo_variants():
    assert find_clo_variants(["CLO1", "CLO 1", "CLO2"]) == [["CLO 1", "CLO1"]]
    assert find_clo_variants(["CLO1", "CLO2"]) == []


# --------------------------------------------------------------------------- #
# score_assessment
# --------------------------------------------------------------------------- #


def test_score_assessment_prefers_original_mapping():
    assessment = {"c1": {"points": 7.0}, "c2": {"points": 9.0}}
    totals, warnings, fallback = score_assessment(assessment, mapping(), PATTERN)
    assert totals == {"CLO1": 7.0, "CLO2": 9.0}
    assert warnings == []
    assert fallback == 0


def test_score_assessment_falls_back_to_pattern_on_unknown_criterion():
    assessment = {"zz": {"points": 4.0, "description": "CLO9 extra"}}
    totals, warnings, fallback = score_assessment(assessment, mapping(), PATTERN)
    assert totals == {"CLO9": 4.0}
    assert fallback == 1
    assert warnings == []


def test_score_assessment_warns_about_unmapped_criterion():
    assessment = {"zz": {"points": 4.0}}
    totals, warnings, fallback = score_assessment(assessment, mapping(), PATTERN)
    assert totals == {}
    assert warnings and "matches no CLO" in warnings[0]


def test_score_assessment_missing_points_counts_zero():
    assessment = {"c1": {"points": None}}
    totals, warnings, _ = score_assessment(assessment, mapping(), PATTERN)
    assert totals == {"CLO1": 0.0}
    assert warnings and "no usable points" in warnings[0]


# --------------------------------------------------------------------------- #
# late penalty distribution
# --------------------------------------------------------------------------- #


def test_apply_penalty_evenly_splits():
    adjusted, per = apply_penalty_evenly({"CLO1": 10.0, "CLO2": 10.0}, 5.0)
    assert per == 2.5
    assert adjusted == {"CLO1": 7.5, "CLO2": 7.5}


def test_apply_penalty_evenly_fixes_rounding_on_largest_clo():
    adjusted, per = apply_penalty_evenly({"A": 3.0, "B": 3.0, "C": 3.0}, 5.0)
    assert round(per, 4) == round(5 / 3, 4)
    assert round(sum(adjusted.values()), 2) == 4.0  # 9.0 - 5.0
    assert max(adjusted.values()) == 1.34  # remainder landed on one CLO


def test_apply_penalty_evenly_empty():
    assert apply_penalty_evenly({}, 5.0) == ({}, 0.0)


# --------------------------------------------------------------------------- #
# evaluate_submission
# --------------------------------------------------------------------------- #


def test_evaluate_skips_ungraded():
    ev = evaluate_submission({"workflow_state": "submitted"}, mapping(), PATTERN)
    assert ev.status == "ungraded"


def test_evaluate_skips_excused():
    ev = evaluate_submission({"workflow_state": "graded", "excused": True}, mapping(), PATTERN)
    assert ev.status == "excused"


def test_evaluate_skips_missing_rubric_assessment():
    ev = evaluate_submission(graded(10, {}), mapping(), PATTERN)
    assert ev.status == "no_rubric"
    assert ev.warnings


def test_evaluate_matching_grade_splits_totals():
    ev = evaluate_submission(
        graded(20.0, {"c1": {"points": 10.0}, "c2": {"points": 10.0}}), mapping(), PATTERN
    )
    assert ev.status == "graded"
    assert ev.totals == {"CLO1": 10.0, "CLO2": 10.0}
    assert ev.penalty_applied == 0.0


def test_evaluate_skips_when_grade_does_not_match_rubric_total():
    """The core guard: unexplained adjustments are warned about and skipped."""
    ev = evaluate_submission(
        graded(17.0, {"c1": {"points": 8.0}, "c2": {"points": 10.0}}), mapping(), PATTERN
    )
    assert ev.status == "score_mismatch"
    assert ev.totals == {}
    assert "17" in ev.warnings[0] and "18" in ev.warnings[0]


def test_evaluate_skips_when_manual_reduction_has_no_penalty():
    ev = evaluate_submission(
        graded(15.0, {"c1": {"points": 10.0}, "c2": {"points": 10.0}}), mapping(), PATTERN
    )
    assert ev.status == "score_mismatch"


def test_evaluate_skips_extra_credit_above_rubric_total():
    ev = evaluate_submission(
        graded(22.0, {"c1": {"points": 10.0}, "c2": {"points": 10.0}}), mapping(), PATTERN
    )
    assert ev.status == "score_mismatch"


def test_evaluate_skips_graded_submission_without_score():
    ev = evaluate_submission(
        graded(None, {"c1": {"points": 10.0}}), mapping(), PATTERN
    )
    assert ev.status == "score_mismatch"
    assert "no score" in ev.warnings[0]


def test_evaluate_late_penalty_is_spread_evenly_instead_of_skipped():
    submission = graded(
        15.0,
        {"c1": {"points": 10.0}, "c2": {"points": 10.0}},
        points_deducted=5.0,
        late_policy_status="late",
    )
    ev = evaluate_submission(submission, mapping(), PATTERN)
    assert ev.status == "graded"
    assert ev.penalty_applied == 5.0
    assert ev.penalty_per_clo == 2.5
    assert ev.totals == {"CLO1": 7.5, "CLO2": 7.5}
    assert round(sum(ev.totals.values()), 2) == ev.score


def test_evaluate_penalty_that_does_not_explain_the_gap_is_still_skipped():
    submission = graded(
        13.0,
        {"c1": {"points": 10.0}, "c2": {"points": 10.0}},
        points_deducted=5.0,
        late_policy_status="late",
    )
    ev = evaluate_submission(submission, mapping(), PATTERN)
    assert ev.status == "score_mismatch"
    assert "besides the late penalty" in ev.warnings[0]


def test_evaluate_warns_when_penalty_makes_a_clo_negative():
    submission = graded(
        -4.0,
        {"c1": {"points": 1.0}},
        points_deducted=5.0,
        late_policy_status="late",
    )
    ev = evaluate_submission(submission, mapping(), PATTERN)
    assert ev.status == "graded"
    assert ev.totals == {"CLO1": -4.0}
    assert any("negative CLO total" in w for w in ev.warnings)


def test_evaluate_no_mapped_criteria():
    submission = graded(5.0, {"zz": {"points": 5.0}})
    ev = evaluate_submission(submission, mapping(), PATTERN)
    assert ev.status == "no_mapped_criteria"


def test_evaluate_tolerates_tiny_float_noise():
    submission = graded(20.0000001, {"c1": {"points": 10.0}, "c2": {"points": 10.0}})
    ev = evaluate_submission(submission, mapping(), PATTERN)
    assert ev.status == "graded"


# --------------------------------------------------------------------------- #
# formatting / urls
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("value", "expected"),
    [(12.0, "12"), (12.5, "12.5"), (0, "0"), (-0.0, "0"), (None, "0"), (100.0, "100"), (0.5, "0.5")],
)
def test_format_points(value, expected):
    assert format_points(value) == expected


def test_normalize_base_url_adds_scheme():
    url, notes = normalize_base_url("canvas.school.edu")
    assert url == "https://canvas.school.edu"
    assert notes


def test_normalize_base_url_strips_api_prefix_and_slash():
    url, notes = normalize_base_url("https://canvas.school.edu/api/v1/")
    assert url == "https://canvas.school.edu"
    assert any("api/v1" in n for n in notes)


def test_normalize_base_url_rejects_empty():
    with pytest.raises(CanvasError):
        normalize_base_url("   ")
    with pytest.raises(CanvasError):
        normalize_base_url("https://")
