"""Regression tests for the interactive UI (prompt handling, not rendering)."""

import contextlib

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
