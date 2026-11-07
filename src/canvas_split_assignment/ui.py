"""Interactive UI: rich rendering plus questionary prompts."""

from __future__ import annotations

import contextlib
import re
from collections.abc import Callable, Iterator, Sequence
from typing import Any

import questionary
from questionary import Choice
from rich import box
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table

from .models import AssignmentPlan, AssignmentRunStats, RunReport
from .scoring import format_points

FILTER_THRESHOLD = 40  # offer a filter prompt above this many choices


class Ui:
    """Interactive UI: rich rendering plus questionary prompts."""

    def __init__(self, console: Console | None = None) -> None:
        self.console = console or Console()

    # -- rendering --------------------------------------------------------- #

    def banner(self) -> None:
        text = (
            "[bold]Canvas CLO Split[/]\n"
            "Split rubric-graded assignments into one 'No Submission' assignment per CLO "
            "and post each student's rubric points per CLO."
        )
        self.console.print(Panel(text, box=box.ROUNDED, border_style="cyan", padding=(0, 2)))

    def step(self, index: int, total: int, text: str) -> None:
        self.console.print(f"\n[bold cyan]Step {index}/{total}[/] [bold]{escape(text)}[/]")

    def ok(self, message: str) -> None:
        self.console.print(f"  [green]✔[/] {escape(message)}")

    def info(self, message: str) -> None:
        self.console.print(f"  [dim]•[/] {escape(message)}")

    def warn(self, message: str) -> None:
        self.console.print(f"  [yellow]⚠[/] [yellow]{escape(message)}[/]")

    def error(self, message: str) -> None:
        self.console.print(f"  [red]✗[/] [red]{escape(message)}[/]")

    def heading(self, text: str) -> None:
        self.console.print(f"\n[bold]{escape(text)}[/]")

    # -- prompts ----------------------------------------------------------- #

    def text(self, message: str, *, default: str = "", instruction: str | None = None) -> str:
        while True:
            answer = questionary.text(
                message, default=default, instruction=instruction
            ).unsafe_ask()
            answer = (answer or "").strip()
            if answer:
                return answer
            self.warn("Please enter a value.")

    def secret(self, message: str) -> str:
        while True:
            answer = questionary.password(message).unsafe_ask()
            answer = (answer or "").strip()
            if answer:
                return answer
            self.warn("Please enter a value.")

    def confirm(self, message: str, *, default: bool = True) -> bool:
        return bool(questionary.confirm(message, default=default).unsafe_ask())

    def checkbox(self, message: str, choices: Sequence[tuple[str, Any]]) -> list[Any]:
        """Multi-select with an optional filter for long lists."""
        pool = list(choices)
        while True:
            shown = pool
            if len(shown) > FILTER_THRESHOLD:
                needle = self.text(
                    f"{message} [{len(shown)} items] — type text to filter, "
                    "or press Enter to show all",
                    default="",
                )
                if needle:
                    lowered = needle.lower()
                    shown = [(t, v) for t, v in pool if lowered in t.lower()]
                    if not shown:
                        self.warn("Nothing matches that filter — showing everything.")
                        shown = pool
            picked = questionary.checkbox(
                message,
                choices=[Choice(title=title, value=value) for title, value in shown],
            ).unsafe_ask()
            if picked:
                return list(picked)
            if self.confirm("Nothing selected — skip this step?", default=True):
                return []

    # -- decorations ------------------------------------------------------- #

    @contextlib.contextmanager
    def status(self, message: str) -> Iterator[None]:
        with self.console.status(message, spinner="dots"):
            yield

    @contextlib.contextmanager
    def progress(self, total: int, description: str) -> Iterator[Callable[[], None]]:
        columns = (
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            TimeElapsedColumn(),
        )
        with Progress(*columns, console=self.console, transient=True) as bar:
            task = bar.add_task(description, total=total)
            yield lambda: bar.advance(task)


def describe_pattern(ui: Ui, pattern: re.Pattern[str]) -> None:
    if pattern.groups == 0:
        ui.info("no capture group — the whole match will be used as the CLO")
    elif pattern.groups == 1:
        ui.info("capture group 1 will be used as the CLO")
    else:
        ui.info(f"{pattern.groups} capture groups found — only group 1 will be used as the CLO")


# --------------------------------------------------------------------------- #
# Report rendering
# --------------------------------------------------------------------------- #


def render_plan(ui: Ui, plans: Sequence[AssignmentPlan]) -> None:
    table = Table(
        box=box.SIMPLE_HEAVY,
        header_style="bold cyan",
        title="Plan",
        title_style="bold",
    )
    table.add_column("Course")
    table.add_column("Source assignment")
    table.add_column("CLO", style="bold")
    table.add_column("New assignment")
    table.add_column("Pts", justify="right")
    table.add_column("Action")
    for plan in plans:
        if plan.skipped_reason:
            table.add_row(
                escape(plan.course.label),
                escape(plan.assignment_name),
                "-",
                f"[yellow]skipped: {escape(plan.skipped_reason)}[/]",
                "-",
                "-",
            )
            continue
        for target in plan.targets:
            action = (
                f"reuse #{target.existing_assignment_id}"
                if target.existing_assignment_id is not None
                else "create"
            )
            table.add_row(
                escape(plan.course.label),
                escape(plan.assignment_name),
                escape(target.clo),
                escape(target.name),
                format_points(target.points_possible),
                action,
            )
    ui.console.print(table)


def render_summary(ui: Ui, report: RunReport) -> None:
    ui.console.print()
    title = "Summary (dry run — nothing was changed)" if report.dry_run else "Summary"
    table = Table(box=box.SIMPLE_HEAVY, header_style="bold cyan", title=title, title_style="bold")
    table.add_column("Course")
    table.add_column("Source assignment")
    table.add_column("CLO", style="bold")
    table.add_column("CLO assignment")
    table.add_column("Action")
    table.add_column("Students", justify="right")
    table.add_column("Points posted", justify="right")

    totals = AssignmentRunStats()
    created = reused = failed = 0
    for plan in report.plans:
        stats = plan.stats
        for key in (
            "graded_submissions",
            "skipped_ungraded",
            "skipped_excused",
            "skipped_no_rubric",
            "skipped_score_mismatch",
            "skipped_no_clo",
            "late_penalties",
            "grades_posted",
            "grades_failed",
        ):
            setattr(totals, key, getattr(totals, key) + getattr(stats, key))
        totals.late_penalty_points += stats.late_penalty_points
        if plan.skipped_reason:
            table.add_row(
                escape(plan.course.label),
                escape(plan.assignment_name),
                "-",
                f"[yellow]skipped: {escape(plan.skipped_reason)}[/]",
                "-",
                "-",
                "-",
            )
            continue
        for target in plan.targets:
            if target.action == "created":
                created += 1
            elif target.action == "reused":
                reused += 1
            elif target.action == "failed":
                failed += 1
            suffix = f" (#{target.assignment_id})" if target.assignment_id else ""
            if report.dry_run:
                points = "—"
            else:
                points = (
                    f"{format_points(target.points_posted)} / "
                    f"{format_points(target.points_possible)}"
                )
            table.add_row(
                escape(plan.course.label),
                escape(plan.assignment_name),
                escape(target.clo),
                escape(target.name + suffix),
                escape(target.action),
                str(target.students_graded),
                points,
            )
    ui.console.print(table)

    # per-assignment notes: skipped submissions and applied late penalties
    for plan in report.plans:
        stats = plan.stats
        notes: list[str] = []
        if stats.late_penalties:
            notes.append(
                f"late penalty of {format_points(stats.late_penalty_points)} pt(s) spread evenly "
                f"across CLOs for {stats.late_penalties} submission(s)"
            )
        skipped = []
        if stats.skipped_ungraded:
            skipped.append(f"{stats.skipped_ungraded} ungraded")
        if stats.skipped_excused:
            skipped.append(f"{stats.skipped_excused} excused")
        if stats.skipped_no_rubric:
            skipped.append(f"{stats.skipped_no_rubric} graded without a rubric assessment")
        if stats.skipped_score_mismatch:
            skipped.append(f"{stats.skipped_score_mismatch} grade/rubric mismatch")
        if stats.skipped_no_clo:
            skipped.append(f"{stats.skipped_no_clo} with no mapped CLO points")
        if skipped:
            notes.append("skipped submission(s): " + ", ".join(skipped))
        if notes:
            ui.info(f"{plan.course.label} / {plan.assignment_name}: " + " · ".join(notes))

    mode = "would be created" if report.dry_run else "created"
    ui.console.print(
        Panel(
            f"[bold]{totals.graded_submissions}[/] graded submission(s) analysed · "
            f"[bold]{created}[/] assignment(s) {mode} · "
            f"[bold]{reused}[/] reused · [bold]{failed}[/] failed · "
            f"[bold]{totals.grades_posted}[/] grade(s) posted"
            + (f" · [red]{totals.grades_failed}[/] failed" if totals.grades_failed else "")
            + (
                f" · [yellow]{totals.late_penalties}[/] late penalty/penalties applied"
                if totals.late_penalties
                else ""
            ),
            title="Totals",
            border_style="cyan",
            box=box.ROUNDED,
        )
    )

    warnings = list(report.warnings)
    errors = list(report.errors)
    for plan in report.plans:
        for warning in plan.warnings:
            warnings.append(f"{plan.course.label} / {plan.assignment_name}: {warning}")
        for error in plan.errors:
            errors.append(f"{plan.course.label} / {plan.assignment_name}: {error}")
    _render_findings(ui, "Warnings", warnings, style="yellow")
    _render_findings(ui, "Errors", errors, style="red")


def _render_findings(ui: Ui, title: str, items: Sequence[str], *, style: str) -> None:
    if not items:
        return
    body = "\n".join(f"[{style}]•[/] {escape(item)}" for item in items[:25])
    if len(items) > 25:
        body += f"\n[{style}]…and {len(items) - 25} more[/]"
    ui.console.print(
        Panel(body, title=f"{title} ({len(items)})", border_style=style, box=box.ROUNDED)
    )
