"""Regression tests for the interactive UI (prompt handling, not rendering)."""

import contextlib
import io

import pytest
from rich.console import Console

import canvas_split_assignment.ui as ui_module
from canvas_split_assignment.ui import FILTER_THRESHOLD, Ui


class FakeQuestion:
    """Mimics a questionary question: returns queued answers."""

    def __init__(self, answers: list, asked: list, kind: str, message: str):
        self._answers = answers
        self._asked = asked
        self._kind = kind
        self._message = message

    def unsafe_ask(self):
        self._asked.append((self._kind, self._message))
        return self._answers.pop(0)


@pytest.fixture
def scripted_ui(monkeypatch):
    """A real Ui whose questionary prompts draw from scripted answer queues."""
    answers: dict[str, list] = {"text": [], "checkbox": [], "confirm": []}
    asked: list[tuple[str, str]] = []

    def fake_prompt(kind):
        def factory(message, *args, **kwargs):
            assert answers[kind], f"unexpected {kind} prompt: {message}"
            return FakeQuestion(answers[kind], asked, kind, message)

        return factory

    monkeypatch.setattr(ui_module.questionary, "text", fake_prompt("text"))
    monkeypatch.setattr(ui_module.questionary, "checkbox", fake_prompt("checkbox"))
    monkeypatch.setattr(ui_module.questionary, "confirm", fake_prompt("confirm"))

    ui = Ui(Console(force_terminal=False, width=200))
    return ui, answers, asked


def many_choices(count=FILTER_THRESHOLD + 5):
    return [(f"Course {i}", i) for i in range(1, count + 1)]


def test_empty_filter_shows_every_choice(scripted_ui):
    """Regression: pressing Enter at the filter prompt must show the full list."""
    ui, answers, asked = scripted_ui
    choices = many_choices()
    answers["text"].append("")  # user presses Enter -> show all
    answers["checkbox"].append([v for _t, v in choices])

    picked = ui.checkbox("Which course(s)?", choices)

    assert picked == [v for _t, v in choices]
    assert [k for k, _m in asked] == ["text", "checkbox"]
    assert "press Enter to show all" in asked[0][1]


def test_filter_narrows_the_choices(scripted_ui):
    ui, answers, asked = scripted_ui
    choices = many_choices()
    answers["text"].append("course 1")  # matches Course 1, Course 10..19
    answers["checkbox"].append([1, 10])

    picked = ui.checkbox("Which course(s)?", choices)

    assert picked == [1, 10]
    _, message = asked[1]
    assert message == "Which course(s)?"


def test_no_filter_prompt_for_short_lists(scripted_ui):
    ui, answers, asked = scripted_ui
    choices = [("A", "a"), ("B", "b")]
    answers["checkbox"].append(["a"])

    picked = ui.checkbox("Pick", choices)

    assert picked == ["a"]
    assert asked == [("checkbox", "Pick")]


def test_empty_selection_reprompts_until_confirmed_skip(scripted_ui):
    ui, answers, asked = scripted_ui
    choices = [("A", "a")]
    answers["checkbox"].append([])  # nothing selected
    answers["confirm"].append(True)  # user confirms skipping

    assert ui.checkbox("Pick", choices) == []


def test_text_rejects_empty_by_default(scripted_ui):
    ui, answers, asked = scripted_ui
    answers["text"].extend(["", "value"])

    assert ui.text("Prompt") == "value"
    assert len([k for k, _m in asked if k == "text"]) == 2


def test_text_allows_empty_when_asked(scripted_ui):
    ui, answers, asked = scripted_ui
    answers["text"].append("")

    assert ui.text("Prompt", allow_empty=True) == ""
    assert len(asked) == 1


# --------------------------------------------------------------------------- #
# summary rendering
# --------------------------------------------------------------------------- #


def test_summary_shows_average_and_total_separately():
    """Regression: never pair the class total with the per-student max in one cell."""
    from canvas_split_assignment.models import (
        AssignmentPlan,
        CloTarget,
        CourseInfo,
        RunReport,
    )
    from canvas_split_assignment.ui import render_summary

    console = Console(file=io.StringIO(), width=250, force_terminal=False)
    ui = Ui(console)
    plan = AssignmentPlan(
        course=CourseInfo(id=1, name="Lab", code="CMPE 270L-1"),
        assignment_id=10,
        assignment_name="Assignment 1 - Submission",
        targets=[
            CloTarget(
                clo="B1",
                name="Assignment 1 - Submission - [B1]",
                points_possible=15.0,
                assignment_id=501,
                action="created",
                students_graded=20,
                points_posted=296.26,
            )
        ],
    )
    report = RunReport(plans=[plan])
    render_summary(ui, report)
    out = console.file.getvalue()

    assert "Avg / max" in out
    assert "Total posted" in out
    # per-student average vs per-student max
    assert "14.81 / 15" in out
    # the class-wide total has its own column
    assert "296.26" in out
    assert "296.26 / 15" not in out


def test_summary_labels_dry_run_columns_as_projected():
    from canvas_split_assignment.models import (
        AssignmentPlan,
        CloTarget,
        CourseInfo,
        RunReport,
    )
    from canvas_split_assignment.ui import render_summary

    console = Console(file=io.StringIO(), width=250, force_terminal=False)
    ui = Ui(console)
    plan = AssignmentPlan(
        course=CourseInfo(id=1, name="Lab", code="X"),
        assignment_id=10,
        assignment_name="HW",
        targets=[
            CloTarget(
                clo="B1",
                name="HW - [B1]",
                points_possible=15.0,
                action="would create",
                students_graded=2,
                points_posted=17.5,
            )
        ],
    )
    report = RunReport(dry_run=True, plans=[plan])
    render_summary(ui, report)
    out = console.file.getvalue()

    assert "Avg / max (projected)" in out
    assert "Total (projected)" in out
    assert "8.75 / 15" in out
