"""End-to-end tests: run() against a fake Canvas API served over MockTransport."""

import argparse
import contextlib
import io
import re
from urllib.parse import parse_qs

import httpx
from rich.console import Console

from canvas_split_assignment.api import CanvasClient
from canvas_split_assignment.cli import run
from canvas_split_assignment.models import RunReport
from canvas_split_assignment.ui import Ui


class FakeUi(Ui):
    """Scripted UI: canned answers, silent output."""

    def __init__(self, *, checkbox_answers=None):
        super().__init__(console=Console(file=io.StringIO(), width=120))
        self.checkbox_answers = [list(answer) for answer in (checkbox_answers or [])]
        self.seen_choices: list[list] = []

    def text(self, message, **kwargs):
        raise AssertionError(f"unexpected text prompt: {message}")

    def secret(self, message):
        raise AssertionError(f"unexpected secret prompt: {message}")

    def confirm(self, message, **kwargs):
        raise AssertionError(f"unexpected confirm prompt: {message}")

    def checkbox(self, message, choices):
        self.seen_choices.append([value for _title, value in choices])
        answer = self.checkbox_answers.pop(0)
        values = {value for _title, value in choices}
        assert set(answer) <= values, f"queued {answer} are not all in {sorted(values)}"
        return list(answer)

    @contextlib.contextmanager
    def status(self, message):
        yield

    @contextlib.contextmanager
    def progress(self, total, description):
        yield lambda: None


class FakeCanvas:
    """In-memory Canvas API."""

    def __init__(self, *, course_pages, assignments, details, submissions):
        self.course_pages = course_pages
        self.assignments = assignments
        self.details = details
        self.submissions = submissions
        self.created: list[dict] = []
        self.graded: list[tuple[int, int, str]] = []
        self.transport = httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method.upper()

        if path == "/api/v1/users/self":
            return httpx.Response(200, json={"id": 1, "name": "Teacher T"})

        if path == "/api/v1/courses":
            page = int(request.url.params.get("page", "1"))
            headers = {}
            if page < len(self.course_pages):
                headers["Link"] = f'<https://canvas.test/api/v1/courses?page={page + 1}>; rel="next"'
            return httpx.Response(200, json=self.course_pages[page - 1], headers=headers)

        match = re.fullmatch(r"/api/v1/courses/(\d+)/assignments", path)
        if match and method == "GET":
            return httpx.Response(200, json=self.assignments.get(int(match.group(1)), []))
        if match and method == "POST":
            self.created.append(parse_qs(request.content.decode()))
            return httpx.Response(200, json={"id": 500 + len(self.created)})

        match = re.fullmatch(r"/api/v1/courses/(\d+)/assignments/(\d+)", path)
        if match and method == "GET":
            detail = self.details.get(int(match.group(2)))
            if detail is None:
                return httpx.Response(404, json={"errors": [{"message": "not found"}]})
            return httpx.Response(200, json=detail)

        match = re.fullmatch(r"/api/v1/courses/(\d+)/assignments/(\d+)/submissions", path)
        if match and method == "GET":
            return httpx.Response(200, json=self.submissions.get(int(match.group(2)), []))

        match = re.fullmatch(r"/api/v1/courses/(\d+)/assignments/(\d+)/submissions/(\d+)", path)
        if match and method == "PUT":
            form = parse_qs(request.content.decode())
            grade = form["submission[posted_grade]"][0]
            self.graded.append((int(match.group(2)), int(match.group(3)), grade))
            return httpx.Response(200, json={"id": 1, "posted_at": "2026-01-01"})

        return httpx.Response(404, json={"errors": [{"message": f"unhandled {method} {path}"}]})


def make_args(**overrides):
    defaults = dict(
        url="https://canvas.test",
        token="token",
        pattern=r"(CLO\s*\d+)",
        dry_run=False,
        yes=True,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


RUBRIC = [
    {"id": "c1", "points": 10, "description": "CLO1: fundamentals"},
    {"id": "c2", "points": 10, "description": "CLO2: analysis"},
]

DETAIL = {
    "id": 10,
    "name": "Homework 1",
    "points_possible": 20.0,
    "use_rubric_for_grading": True,
    "rubric": RUBRIC,
}

SUBMISSIONS = [
    {
        "user_id": 7,
        "workflow_state": "graded",
        "score": 20.0,
        "user": {"name": "Jane"},
        "rubric_assessment": {"c1": {"points": 10.0}, "c2": {"points": 10.0}},
    },
    {
        "user_id": 8,
        "workflow_state": "graded",
        "score": 15.0,
        "points_deducted": 5.0,
        "late_policy_status": "late",
        "user": {"name": "Sam"},
        "rubric_assessment": {"c1": {"points": 10.0}, "c2": {"points": 10.0}},
    },
    {
        "user_id": 9,
        "workflow_state": "submitted",
        "score": None,
        "user": {"name": "Pat"},
        "rubric_assessment": {},
    },
    {
        "user_id": 10,
        "workflow_state": "graded",
        "score": 17.0,
        "user": {"name": "Ann"},
        "rubric_assessment": {"c1": {"points": 8.0}, "c2": {"points": 10.0}},
    },
]


def make_canvas(extra_assignments=None):
    assignments = [
        {"id": 10, "name": "Homework 1", "points_possible": 20.0, "rubric": RUBRIC},
    ] + list(extra_assignments or [])
    return FakeCanvas(
        course_pages=[
            [{"id": 1, "name": "Thermo", "course_code": "ENGR101", "workflow_state": "available",
              "term": {"name": "Fall 2026"}}],
            [{"id": 2, "name": "Fluids", "course_code": "ENGR102", "workflow_state": "available"}],
        ],
        assignments={1: assignments, 2: []},
        details={10: DETAIL},
        submissions={10: SUBMISSIONS},
    )


def patch_transport(monkeypatch, fake):
    """Route every CanvasClient through the fake transport."""
    real_init = CanvasClient.__init__

    def patched(self, base_url, token, **kwargs):
        kwargs["transport"] = fake.transport
        real_init(self, base_url, token, **kwargs)

    monkeypatch.setattr(CanvasClient, "__init__", patched)


# --------------------------------------------------------------------------- #
# tests
# --------------------------------------------------------------------------- #


def test_full_run_creates_assignments_and_posts_grades(monkeypatch):
    fake = make_canvas()
    patch_transport(monkeypatch, fake)
    ui = FakeUi(checkbox_answers=[[1], [10]])  # course 1, assignment 10
    report = RunReport()

    code = run(ui, make_args(), report)

    assert code == 0
    # both course pages were served -> both courses were offered
    assert ui.seen_choices[0] == [1, 2]

    # two CLO assignments created as No Submission, unpublished, counted in final grade
    assert len(fake.created) == 2
    by_name = {form["assignment[name]"][0]: form for form in fake.created}
    assert set(by_name) == {"Homework 1 - [CLO1]", "Homework 1 - [CLO2]"}
    for form in by_name.values():
        assert form["assignment[submission_types][]"] == ["none"]
        assert form["assignment[grading_type]"] == ["points"]
        assert form["assignment[published]"] == ["false"]
        assert form["assignment[omit_from_final_grade]"] == ["false"]
        assert form["assignment[points_possible]"] == ["10"]

    # per-student grades: Jane full marks, Sam minus half the penalty per CLO,
    # nothing for Pat (ungraded) or Ann (grade/rubric mismatch)
    clo1_id, clo2_id = 501, 502
    assert sorted(fake.graded) == [
        (clo1_id, 7, "10"),
        (clo1_id, 8, "7.5"),
        (clo2_id, 7, "10"),
        (clo2_id, 8, "7.5"),
    ]

    stats = report.plans[0].stats
    assert stats.graded_submissions == 3
    assert stats.skipped_ungraded == 1
    assert stats.skipped_score_mismatch == 1
    assert stats.late_penalties == 1
    assert stats.late_penalty_points == 5.0
    assert stats.grades_posted == 4
    assert stats.grades_failed == 0
    # Ann's mismatch is called out by name
    assert any("Ann" in w and "17" in w and "18" in w for w in report.plans[0].warnings)


def test_reuses_existing_assignment_with_the_same_name(monkeypatch):
    fake = make_canvas(
        extra_assignments=[{"id": 99, "name": "Homework 1 - [CLO1]", "points_possible": 10.0}]
    )
    patch_transport(monkeypatch, fake)
    ui = FakeUi(checkbox_answers=[[1], [10]])

    code = run(ui, make_args(), RunReport())

    assert code == 0
    # only CLO2 is created (id 501); CLO1 grades go to the pre-existing #99
    assert [form["assignment[name]"][0] for form in fake.created] == ["Homework 1 - [CLO2]"]
    assert sorted(fake.graded) == [
        (99, 7, "10"),
        (99, 8, "7.5"),
        (501, 7, "10"),
        (501, 8, "7.5"),
    ]


def test_dry_run_changes_nothing(monkeypatch):
    fake = make_canvas()
    patch_transport(monkeypatch, fake)
    ui = FakeUi(checkbox_answers=[[1], [10]])
    report = RunReport()

    code = run(ui, make_args(dry_run=True), report)

    assert code == 0
    assert fake.created == []
    assert fake.graded == []
    assert all(t.action == "would create" for t in report.plans[0].targets)


def test_assignment_without_rubric_is_skipped_with_warning(monkeypatch):
    fake = make_canvas()
    fake.details[10] = {"id": 10, "name": "Homework 1", "points_possible": 20.0}
    patch_transport(monkeypatch, fake)
    ui = FakeUi(checkbox_answers=[[1], [10]])
    report = RunReport()

    code = run(ui, make_args(), report)

    assert code == 2
    assert fake.created == []
    assert report.plans[0].skipped_reason == "no rubric"
    assert any("no rubric" in w for w in report.plans[0].warnings)


def test_criteria_without_clo_are_excluded_and_warned(monkeypatch):
    fake = make_canvas()
    fake.details[10] = {
        "id": 10,
        "name": "Homework 1",
        "points_possible": 25.0,
        "use_rubric_for_grading": True,
        "rubric": RUBRIC + [{"id": "c3", "points": 5, "description": "Teamwork"}],
    }
    fake.submissions[10] = [
        {
            "user_id": 7,
            "workflow_state": "graded",
            "score": 25.0,
            "user": {"name": "Jane"},
            "rubric_assessment": {
                "c1": {"points": 10.0},
                "c2": {"points": 10.0},
                "c3": {"points": 5.0},
            },
        }
    ]
    patch_transport(monkeypatch, fake)
    ui = FakeUi(checkbox_answers=[[1], [10]])
    report = RunReport()

    code = run(ui, make_args(), report)

    assert code == 0
    assert any("Teamwork" in w and "matches no CLO" in w for w in report.plans[0].warnings)
    # only the mapped criteria are posted
    assert sorted(fake.graded) == [(501, 7, "10"), (502, 7, "10")]
