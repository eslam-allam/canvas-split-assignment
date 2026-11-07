"""Data structures shared across the tool."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class CourseInfo:
    id: int
    name: str
    code: str
    workflow_state: str = ""
    term: str | None = None
    concluded: bool = False

    @property
    def archived(self) -> bool:
        """A course the user would call archived: concluded or completed/deleted."""
        return self.concluded or self.workflow_state in ("completed", "deleted")

    @property
    def label(self) -> str:
        bits = [self.name]
        if self.code:
            bits.append(f"({self.code})")
        bits.append(f"#{self.id}")
        return " ".join(bits)


@dataclass(frozen=True)
class CriterionClo:
    """A rubric criterion bound to the CLO extracted from its description."""

    criterion_id: str
    clo: str
    points: float
    description: str


@dataclass
class CloTarget:
    """One CLO assignment that will back a source assignment."""

    clo: str
    name: str
    points_possible: float = 0.0
    criteria_ids: list[str] = field(default_factory=list)
    existing_assignment_id: int | None = None
    #: Canvas published state (new targets start as drafts, reused ones come from the listing)
    published: bool = False
    # filled during execution
    assignment_id: int | None = None
    action: str = "planned"  # planned | created | reused | failed
    published_by_tool: bool = False
    students_graded: int = 0
    points_posted: float = 0.0


@dataclass
class AssignmentRunStats:
    graded_submissions: int = 0
    skipped_ungraded: int = 0
    skipped_excused: int = 0
    skipped_no_rubric: int = 0
    skipped_score_mismatch: int = 0
    skipped_no_clo: int = 0
    late_penalties: int = 0
    late_penalty_points: float = 0.0
    grades_posted: int = 0
    grades_failed: int = 0


@dataclass
class AssignmentPlan:
    course: CourseInfo
    assignment_id: int
    assignment_name: str
    targets: list[CloTarget] = field(default_factory=list)
    mapping: dict[str, CriterionClo] = field(default_factory=dict)
    unmatched: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    skipped_reason: str | None = None
    stats: AssignmentRunStats = field(default_factory=AssignmentRunStats)

    @property
    def is_processable(self) -> bool:
        return self.skipped_reason is None and bool(self.targets)


@dataclass
class RunReport:
    dry_run: bool = False
    user: str = ""
    base_url: str = ""
    pattern: str = ""
    plans: list[AssignmentPlan] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass
class SubmissionEvaluation:
    """Result of checking one submission against the rubric."""

    status: str  # graded | excused | ungraded | no_rubric | score_mismatch | no_mapped_criteria
    totals: dict[str, float] = field(default_factory=dict)
    rubric_total: float = 0.0
    score: float | None = None
    warnings: list[str] = field(default_factory=list)
    #: late-penalty points that were spread evenly across `totals`
    penalty_applied: float = 0.0
    #: the per-CLO share of the penalty
    penalty_per_clo: float = 0.0
