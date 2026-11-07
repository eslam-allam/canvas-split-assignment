"""Pure scoring and CLO-mapping logic (no I/O, fully unit-testable)."""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from .models import CloTarget, CriterionClo, SubmissionEvaluation

#: |score - rubric_total| above this is treated as "grade adjusted outside the rubric".
SCORE_MATCH_TOLERANCE = 0.01

#: Canvas' default maximum assignment name length.
MAX_NAME_LENGTH = 255


# --------------------------------------------------------------------------- #
# CLO extraction
# --------------------------------------------------------------------------- #


def extract_clo(pattern: re.Pattern[str], text: str | None) -> str | None:
    """Return the CLO captured by *pattern* inside *text*, if any."""
    if not text:
        return None
    match = pattern.search(text)
    if match is None:
        return None
    if match.groups() and match.group(1) is not None:
        value = match.group(1)
    else:
        value = match.group(0)
    value = (value or "").strip()
    return value or None


def _criterion_texts(criterion: dict[str, Any]) -> list[str]:
    """Description-ish texts of a rubric criterion, most specific first."""
    texts: list[str] = []
    for key in ("description", "long_description"):
        value = criterion.get(key)
        if isinstance(value, str) and value.strip():
            texts.append(value.strip())
    return texts


def _assessment_texts(entry: dict[str, Any]) -> list[str]:
    """Description-ish texts of a graded rubric assessment entry."""
    texts = _criterion_texts(entry)
    nested = entry.get("criterion")
    if isinstance(nested, dict):
        texts.extend(_criterion_texts(nested))
    return texts


def build_clo_mapping(
    criteria: Sequence[dict[str, Any]], pattern: re.Pattern[str]
) -> tuple[list[CriterionClo], list[str]]:
    """Map every rubric criterion to a CLO.

    Returns the mapped criteria and the descriptions of criteria the pattern
    could not match (those are excluded from the CLO totals).
    """
    mapped: list[CriterionClo] = []
    unmatched: list[str] = []
    for criterion in criteria or []:
        raw_id = criterion.get("id")
        criterion_id = "" if raw_id is None else str(raw_id)
        texts = _criterion_texts(criterion)
        display = re.sub(r"\s+", " ", texts[0] if texts else criterion_id) or criterion_id
        clo = None
        for text in texts:
            clo = extract_clo(pattern, text)
            if clo:
                break
        if not clo:
            unmatched.append(display)
            continue
        try:
            points = float(criterion.get("points") or 0.0)
        except (TypeError, ValueError):
            points = 0.0
        mapped.append(CriterionClo(criterion_id, clo, points, display))
    return mapped, unmatched


# --------------------------------------------------------------------------- #
# CLO targets
# --------------------------------------------------------------------------- #


def clo_assignment_name(source_name: str, clo: str) -> str:
    return f"{source_name} - [{clo}]"


def build_targets(
    source_name: str,
    mapped: Sequence[CriterionClo],
    existing_by_name: dict[str, int],
    existing_published: dict[str, bool] | None = None,
) -> tuple[list[CloTarget], list[str]]:
    """Group mapped criteria into one CloTarget per unique CLO."""
    warnings: list[str] = []
    existing_published = existing_published or {}
    ordered: dict[str, CloTarget] = {}
    for criterion in mapped:
        target = ordered.get(criterion.clo)
        if target is None:
            name = clo_assignment_name(source_name, criterion.clo)
            if len(name) > MAX_NAME_LENGTH:
                warnings.append(
                    f"assignment name '{name[:60]}...' exceeds {MAX_NAME_LENGTH} characters "
                    "and will be truncated"
                )
                name = name[:MAX_NAME_LENGTH]
            existing_id = existing_by_name.get(name)
            target = CloTarget(
                clo=criterion.clo,
                name=name,
                existing_assignment_id=existing_id,
                published=bool(existing_published.get(name)) if existing_id is not None else False,
            )
            ordered[criterion.clo] = target
        target.points_possible += criterion.points
        target.criteria_ids.append(criterion.criterion_id)
    return list(ordered.values()), warnings


def find_clo_variants(clos: Sequence[str]) -> list[list[str]]:
    """Groups of CLO labels that only differ in spacing/case (likely duplicates)."""
    groups: dict[str, list[str]] = {}
    for clo in clos:
        key = re.sub(r"\s+", "", clo).lower()
        groups.setdefault(key, [])
        if clo not in groups[key]:
            groups[key].append(clo)
    return [sorted(values) for values in groups.values() if len(values) > 1]


# --------------------------------------------------------------------------- #
# Per-submission scoring
# --------------------------------------------------------------------------- #


def score_assessment(
    assessment: dict[str, Any],
    mapping: dict[str, CriterionClo],
    pattern: re.Pattern[str],
) -> tuple[dict[str, float], list[str], int]:
    """Sum awarded rubric points per CLO.

    Criterion ids are resolved through the assignment's rubric mapping first;
    unknown ids fall back to running the CLO pattern over the graded rubric's
    own descriptions.  Returns (totals, warnings, fallback_count).
    """
    totals: dict[str, float] = {}
    warnings: list[str] = []
    fallback = 0
    for raw_id, entry in (assessment or {}).items():
        criterion_id = "" if raw_id is None else str(raw_id)
        if not isinstance(entry, dict):
            warnings.append(f"criterion '{criterion_id}' has a malformed rubric assessment")
            continue
        criterion = mapping.get(criterion_id)
        if criterion is not None:
            clo = criterion.clo
        else:
            clo = None
            for text in _assessment_texts(entry):
                clo = extract_clo(pattern, text)
                if clo:
                    break
            if clo:
                fallback += 1
        if not clo:
            warnings.append(
                f"criterion '{criterion_id}' matches no CLO (neither the rubric mapping "
                "nor the pattern) — its points are left out"
            )
            continue
        raw_points = entry.get("points")
        try:
            points = float(raw_points)
        except (TypeError, ValueError):
            points = 0.0
            warnings.append(
                f"criterion '{criterion_id}' ({clo}) has no usable points "
                f"({raw_points!r}) — counted as 0"
            )
        totals[clo] = totals.get(clo, 0.0) + points
    return totals, warnings, fallback


def _late_penalty(submission: dict[str, Any]) -> float:
    """Automatic late/missing-policy deduction applied to the score, if any."""
    try:
        value = float(submission.get("points_deducted") or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return value if value > 0.0 else 0.0


def apply_penalty_evenly(totals: dict[str, float], penalty: float) -> tuple[dict[str, float], float]:
    """Spread a late penalty evenly across the CLO totals.

    Returns ``(adjusted totals, per-CLO share)``.  A rounding remainder is put on
    the largest CLO so the adjusted totals always add up to the recorded grade.
    """
    if not totals:
        return {}, 0.0
    per_clo = penalty / len(totals)
    adjusted = {clo: round(points - per_clo, 2) for clo, points in totals.items()}
    target_sum = round(sum(totals.values()) - penalty, 2)
    residual = round(target_sum - sum(adjusted.values()), 2)
    if residual:
        biggest = max(adjusted, key=lambda clo: adjusted[clo])
        adjusted[biggest] = round(adjusted[biggest] + residual, 2)
    return adjusted, per_clo


def evaluate_submission(
    submission: dict[str, Any],
    mapping: dict[str, CriterionClo],
    pattern: re.Pattern[str],
    *,
    tolerance: float = SCORE_MATCH_TOLERANCE,
) -> SubmissionEvaluation:
    """Decide whether a submission can be split per CLO, and compute its totals.

    The recorded grade must equal the total of the rubric assessment (within
    *tolerance*); otherwise the CLO split would not reconcile with the gradebook
    and the submission is warned about and skipped.  One exception: when the
    grade is lower than the rubric total by exactly the automatic late/missing
    penalty, the penalty is spread evenly across the CLOs instead of skipping.
    """
    if submission.get("excused"):
        return SubmissionEvaluation(status="excused")
    if submission.get("workflow_state") != "graded":
        return SubmissionEvaluation(status="ungraded")

    assessment = submission.get("rubric_assessment")
    if not isinstance(assessment, dict) or not assessment:
        return SubmissionEvaluation(
            status="no_rubric",
            warnings=["graded without a rubric assessment — cannot split per CLO"],
        )

    rubric_total = 0.0
    for entry in assessment.values():
        if isinstance(entry, dict):
            try:
                rubric_total += float(entry.get("points") or 0.0)
            except (TypeError, ValueError):
                continue

    raw_score = submission.get("score")
    try:
        score = float(raw_score) if raw_score is not None else None
    except (TypeError, ValueError):
        score = None
    if score is None:
        return SubmissionEvaluation(
            status="score_mismatch",
            rubric_total=rubric_total,
            warnings=[
                f"is graded but has no score while the rubric totals "
                f"{format_points(rubric_total)} — the two cannot be reconciled"
            ],
        )

    totals, warnings, _fallback = score_assessment(assessment, mapping, pattern)
    if not totals:
        return SubmissionEvaluation(
            status="no_mapped_criteria",
            rubric_total=rubric_total,
            score=score,
            warnings=warnings or ["no rubric criterion maps to a CLO"],
        )

    gap = rubric_total - score
    if abs(gap) <= tolerance:
        return SubmissionEvaluation(
            status="graded",
            totals=totals,
            rubric_total=rubric_total,
            score=score,
            warnings=warnings,
        )

    penalty = _late_penalty(submission)
    if gap > tolerance and penalty > 0.0 and abs(gap - penalty) <= tolerance:
        # The grade is the rubric total minus the automatic late penalty:
        # keep the submission and spread the penalty evenly across the CLOs.
        adjusted, per_clo = apply_penalty_evenly(totals, gap)
        negative = [
            f"{clo} {format_points(value)}" for clo, value in sorted(adjusted.items()) if value < 0
        ]
        if negative:
            warnings = warnings + [
                f"late penalty of {format_points(gap)} leaves a negative CLO total "
                f"({', '.join(negative)})"
            ]
        return SubmissionEvaluation(
            status="graded",
            totals=adjusted,
            rubric_total=rubric_total,
            score=score,
            warnings=warnings,
            penalty_applied=gap,
            penalty_per_clo=per_clo,
        )

    if penalty > 0.0:
        reason = (
            f"grade {format_points(score)} does not match the rubric total "
            f"{format_points(rubric_total)}: the late penalty is {format_points(penalty)} but "
            f"the gap is {format_points(gap)}, so something besides the late penalty "
            "changed the grade"
        )
    else:
        reason = (
            f"grade {format_points(score)} does not match the rubric total "
            f"{format_points(rubric_total)} — the grade was adjusted outside the rubric "
            "(extra credit, manual override, ...)"
        )
    return SubmissionEvaluation(
        status="score_mismatch",
        rubric_total=rubric_total,
        score=score,
        warnings=[reason],
    )


# --------------------------------------------------------------------------- #
# Formatting
# --------------------------------------------------------------------------- #


def format_points(value: float | int | None) -> str:
    """Format a score for humans and for Canvas (``13``, ``13.5``, ``0``)."""
    if value is None:
        return "0"
    try:
        rounded = round(float(value), 2)
    except (TypeError, ValueError):
        return "0"
    if rounded == 0:
        return "0"
    return f"{rounded:.2f}".rstrip("0").rstrip(".")
