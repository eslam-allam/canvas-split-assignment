"""Command-line pipeline: prompts, planning, execution and the entry point."""

from __future__ import annotations

import argparse
import html
import os
import re
import sys
from collections.abc import Sequence
from typing import Any

from rich import box
from rich.panel import Panel

from .api import CanvasClient, normalize_base_url
from .errors import CanvasAuthError, CanvasError
from .models import AssignmentPlan, CourseInfo, RunReport
from .scoring import (
    build_clo_mapping,
    build_targets,
    evaluate_submission,
    find_clo_variants,
    format_points,
)
from .ui import Ui, describe_pattern, render_plan, render_summary


# --------------------------------------------------------------------------- #
# Prompts
# --------------------------------------------------------------------------- #


def get_pattern(ui: Ui, args: argparse.Namespace) -> re.Pattern[str]:
    provided = args.pattern
    while True:
        raw = provided or ui.text(
            "Regex that captures the CLO in a rubric criterion",
            default="",
            instruction="e.g.  (CLO\\s*\\d+)  or  CLO[:\\s-]*(\\S+)",
        )
        provided = None
        try:
            return re.compile(raw)
        except re.error as exc:
            ui.error(f"Invalid regular expression: {exc}")
            args.pattern = None


def connect(ui: Ui, args: argparse.Namespace) -> tuple[CanvasClient, dict[str, Any]] | None:
    """Prompt for (or reuse) connection settings and verify the token."""
    first = True
    while True:
        if first and args.url:
            raw_url = args.url
        else:
            default = os.environ.get("CANVAS_BASE_URL", "")
            raw_url = ui.text(
                "Canvas base URL", default=default, instruction="e.g. https://canvas.school.edu"
            )
        try:
            base_url, notes = normalize_base_url(raw_url)
        except CanvasError as exc:
            ui.error(str(exc))
            first = False
            continue
        for note in notes:
            ui.info(note)

        if first and args.token:
            token = args.token
        else:
            default_token = os.environ.get("CANVAS_ACCESS_TOKEN", "")
            if default_token and first:
                if ui.confirm(
                    f"Use the access token from $CANVAS_ACCESS_TOKEN ({default_token[:4]}…)?",
                    default=True,
                ):
                    token = default_token
                else:
                    token = ui.secret("Canvas access token")
            else:
                token = ui.secret("Canvas access token")
        token = token.strip()
        if not token:
            ui.error("The access token cannot be empty.")
            first = False
            continue

        client = CanvasClient(base_url, token)
        try:
            with ui.status(f"Connecting to {base_url} …"):
                me = client.me()
        except CanvasAuthError as exc:
            client.close()
            ui.error(str(exc))
        except CanvasError as exc:
            client.close()
            ui.error(str(exc))
        else:
            return client, me
        first = False
        if not ui.confirm("Try different connection settings?", default=True):
            return None


# --------------------------------------------------------------------------- #
# Planning
# --------------------------------------------------------------------------- #


def plan_assignment(
    course: CourseInfo,
    detail: dict[str, Any],
    pattern: re.Pattern[str],
    existing_by_name: dict[str, int],
) -> AssignmentPlan:
    """Work out which CLO assignments a source assignment needs."""
    assignment_id = int(detail.get("id") or 0)
    source_name = str(detail.get("name") or f"assignment {assignment_id}")
    plan = AssignmentPlan(course=course, assignment_id=assignment_id, assignment_name=source_name)

    criteria = detail.get("rubric") or []
    if not criteria:
        plan.skipped_reason = "no rubric"
        plan.warnings.append("has no rubric attached — skipped")
        return plan

    if detail.get("use_rubric_for_grading") is False:
        plan.warnings.append(
            "rubric is advisory only (not used for grading) — grades that were set "
            "outside the rubric will be skipped"
        )
    if detail.get("group_category_id") and not detail.get("grade_group_students_individually"):
        plan.warnings.append("group assignment — every student's submission is split individually")

    mapped, unmatched = build_clo_mapping(criteria, pattern)
    plan.unmatched = unmatched
    for description in unmatched[:3]:
        plan.warnings.append(f"criterion '{description[:70]}' matches no CLO — excluded")
    if len(unmatched) > 3:
        plan.warnings.append(f"...and {len(unmatched) - 3} more criteria match no CLO")
    if not mapped:
        plan.skipped_reason = "no criteria matched the CLO pattern"
        plan.warnings.append("no rubric criterion matched the CLO pattern — skipped")
        return plan

    for variants in find_clo_variants([c.clo for c in mapped]):
        plan.warnings.append(
            "possible duplicate CLO labels: " + ", ".join(f"'{v}'" for v in variants)
        )

    targets, name_warnings = build_targets(source_name, mapped, existing_by_name)
    plan.warnings.extend(name_warnings)
    for target in targets:
        if target.existing_assignment_id is not None:
            plan.warnings.append(
                f"an assignment named '{target.name}' already exists "
                f"(#{target.existing_assignment_id}) — it will be reused"
            )
    plan.mapping = {c.criterion_id: c for c in mapped}
    plan.targets = targets
    return plan


# --------------------------------------------------------------------------- #
# Execution
# --------------------------------------------------------------------------- #


def execute_plan(
    ui: Ui,
    client: CanvasClient,
    plan: AssignmentPlan,
    pattern: re.Pattern[str],
    *,
    dry_run: bool = False,
) -> None:
    """Create the CLO assignments and post the per-student totals.

    With ``dry_run=True`` nothing is written to Canvas: the submissions are
    still fetched and scored so the summary can show projected numbers.
    """
    stats = plan.stats
    if not plan.is_processable:
        return

    course_id = plan.course.id

    # 1. resolve/create the CLO assignments -------------------------------- #
    if dry_run:
        for target in plan.targets:
            target.action = (
                "would create" if target.existing_assignment_id is None else "would reuse"
            )
        live_targets = list(plan.targets)
    else:
        for target in plan.targets:
            if target.existing_assignment_id is not None:
                target.assignment_id = target.existing_assignment_id
                target.action = "reused"
                continue
            description = (
                f"<p>Auto-generated CLO breakdown of "
                f"<strong>{html.escape(plan.assignment_name)}</strong>. "
                f"The grade in this column is the total rubric points a student earned for "
                f"<strong>{html.escape(target.clo)}</strong>.</p>"
            )
            data = {
                "assignment[name]": target.name,
                "assignment[submission_types][]": "none",
                "assignment[grading_type]": "points",
                "assignment[points_possible]": format_points(target.points_possible),
                "assignment[published]": "false",
                "assignment[omit_from_final_grade]": "false",
                "assignment[description]": description,
            }
            try:
                with ui.status(f"Creating '{target.name}' …"):
                    created = client.create_assignment(course_id, data)
            except CanvasError as exc:
                target.action = "failed"
                plan.errors.append(f"could not create '{target.name}': {exc}")
                continue
            target.assignment_id = int(created.get("id") or 0) or None
            target.action = "created" if target.assignment_id else "failed"
            if target.assignment_id is None:
                plan.errors.append(f"Canvas did not return an id for '{target.name}'")

        live_targets = [
            t for t in plan.targets if t.assignment_id and t.action in ("created", "reused")
        ]
        if not live_targets:
            plan.errors.append("no CLO assignment could be created — nothing was posted")
            return

    # 2. fetch submissions ------------------------------------------------- #
    try:
        with ui.status(f"Fetching graded submissions of '{plan.assignment_name}' …"):
            submissions = client.list_submissions(course_id, plan.assignment_id)
    except CanvasError as exc:
        plan.errors.append(f"could not fetch submissions: {exc}")
        return

    # 3. evaluate every submission ---------------------------------------- #
    posts: list[tuple[dict[str, Any], dict[str, float], str]] = []
    orphan: dict[str, int] = {}
    live_clos = {t.clo for t in live_targets}
    for submission in submissions:
        if not isinstance(submission, dict):
            continue
        student = submission.get("user") if isinstance(submission.get("user"), dict) else {}
        name = str(student.get("name") or f"user {submission.get('user_id')}")
        evaluation = evaluate_submission(submission, plan.mapping, pattern)
        if evaluation.status == "ungraded":
            stats.skipped_ungraded += 1
            continue
        if evaluation.status == "excused":
            stats.skipped_excused += 1
            continue
        stats.graded_submissions += 1
        if evaluation.status == "no_rubric":
            stats.skipped_no_rubric += 1
            plan.warnings.append(f"skipped {name}: {evaluation.warnings[0]}")
            continue
        if evaluation.status == "score_mismatch":
            stats.skipped_score_mismatch += 1
            plan.warnings.append(f"skipped {name}: {evaluation.warnings[0]}")
            continue
        if evaluation.status == "no_mapped_criteria":
            stats.skipped_no_clo += 1
            plan.warnings.append(
                f"skipped {name}: {'; '.join(evaluation.warnings) or 'no CLO points'}"
            )
            continue
        if evaluation.penalty_applied:
            stats.late_penalties += 1
            stats.late_penalty_points += evaluation.penalty_applied
        for warning in evaluation.warnings:
            plan.warnings.append(f"{name}: {warning}")
        for clo in evaluation.totals:
            if clo not in live_clos:
                orphan[clo] = orphan.get(clo, 0) + 1
        posts.append((submission, evaluation.totals, name))

    if orphan:
        plan.warnings.append(
            "no CLO assignment available to post to for "
            + ", ".join(
                f"'{clo}' ({count} submission(s))" for clo, count in sorted(orphan.items())
            )
            + " — those points were not posted"
        )

    # 4. post grades (or project them on a dry run) ----------------------- #
    planned_posts = [
        (target, submission, name, points)
        for submission, totals, name in posts
        for target in live_targets
        if (points := totals.get(target.clo)) is not None
    ]
    if not planned_posts:
        plan.warnings.append("no grades to post")
        return

    if dry_run:
        for target, _submission, _name, points in planned_posts:
            stats.grades_posted += 1
            target.students_graded += 1
            target.points_posted += float(points)
        return

    with ui.progress(len(planned_posts), f"Posting grades for '{plan.assignment_name}'") as tick:
        for target, submission, name, points in planned_posts:
            try:
                client.post_grade(
                    course_id, int(target.assignment_id), int(submission.get("user_id")), points
                )
            except (CanvasError, TypeError, ValueError) as exc:
                stats.grades_failed += 1
                plan.errors.append(
                    f"could not post {target.clo} = {format_points(points)} for {name}: {exc}"
                )
            else:
                stats.grades_posted += 1
                target.students_graded += 1
                target.points_posted += float(points)
            tick()


# --------------------------------------------------------------------------- #
# Pipeline
# --------------------------------------------------------------------------- #


def run(ui: Ui, args: argparse.Namespace, report: RunReport | None = None) -> int:
    ui.banner()
    if report is None:
        report = RunReport()
    report.dry_run = bool(args.dry_run)

    ui.step(1, 6, "Connect to Canvas")
    connection = connect(ui, args)
    if connection is None:
        ui.warn("Aborted.")
        return 1
    client, me = connection
    with client:
        report.base_url = client.base_url
        report.user = str(me.get("name") or me.get("id") or "")
        ui.ok(f"Signed in as {report.user or 'unknown user'} on {client.base_url}")

        ui.step(2, 6, "CLO extraction pattern")
        pattern = get_pattern(ui, args)
        report.pattern = pattern.pattern
        describe_pattern(ui, pattern)

        ui.step(3, 6, "Select courses")
        try:
            with ui.status("Fetching your courses …"):
                courses = client.list_courses()
        except CanvasError as exc:
            ui.error(f"Could not list your courses: {exc}")
            return 1
        if not courses:
            ui.error("No active courses found for this account.")
            return 1
        hidden = [c for c in courses if c.archived]
        selectable = courses if args.all_courses else [c for c in courses if not c.archived]
        if hidden and not args.all_courses:
            ui.info(
                f"{len(selectable)} active course(s) found — {len(hidden)} archived/concluded "
                "course(s) hidden (use --all-courses to include them)"
            )
        else:
            ui.info(f"{len(selectable)} course(s) found")

        def course_title(course: CourseInfo) -> str:
            bits = [course.label]
            if course.term:
                bits.append(f"— {course.term}")
            if course.archived:
                bits.append("[archived]")
            elif course.workflow_state == "unpublished":
                bits.append("[unpublished]")
            return " ".join(bits)

        course_ids = ui.checkbox(
            "Which course(s) do you want to process?",
            [(course_title(c), c.id) for c in selectable],
        )
        selected_courses = [c for c in selectable if c.id in set(course_ids)]
        if not selected_courses:
            ui.warn("No courses selected — nothing to do.")
            return 0

        ui.step(4, 6, "Select assignments")
        work: list[tuple[CourseInfo, dict[str, Any], dict[str, int]]] = []
        for course in selected_courses:
            try:
                with ui.status(f"Fetching assignments of {course.label} …"):
                    assignments = client.list_assignments(course.id)
            except CanvasError as exc:
                report.errors.append(f"{course.label}: could not list assignments: {exc}")
                continue
            if not assignments:
                ui.warn(f"{course.label} has no assignments.")
                continue
            existing_by_name: dict[str, int] = {}
            choices: list[tuple[str, Any]] = []
            for item in assignments:
                name = str(item.get("name") or f"assignment {item.get('id')}")
                existing_by_name.setdefault(name, int(item.get("id") or 0))
                points = item.get("points_possible")
                marker = "  ✓ rubric" if item.get("rubric") else ""
                choices.append(
                    (
                        f"{name}  ·  {format_points(points)} pts  ·  "
                        f"#{item.get('id')}{marker}",
                        item.get("id"),
                    )
                )
            picked = ui.checkbox(f"Which assignment(s) in {course.label}?", choices)
            by_id = {int(a.get("id") or 0): a for a in assignments}
            for assignment_id in picked:
                item = by_id.get(int(assignment_id))
                if item is not None:
                    work.append((course, item, existing_by_name))
        if not work:
            ui.warn("No assignments selected — nothing to do.")
            return 0

        ui.step(5, 6, "Review plan")
        for course, item, existing_by_name in work:
            assignment_id = int(item.get("id") or 0)
            try:
                with ui.status(f"Reading rubric of '{item.get('name')}' …"):
                    detail = client.get_assignment(course.id, assignment_id)
            except CanvasError as exc:
                plan = AssignmentPlan(
                    course=course,
                    assignment_id=assignment_id,
                    assignment_name=str(item.get("name")),
                )
                plan.errors.append(f"could not read the assignment: {exc}")
                report.plans.append(plan)
                continue
            plan = plan_assignment(course, detail, pattern, existing_by_name)
            report.plans.append(plan)

        processable = [p for p in report.plans if p.is_processable]
        if not processable:
            ui.error("None of the selected assignments has a usable rubric — nothing to do.")
            render_summary(ui, report)
            return 2
        render_plan(ui, report.plans)

        if args.dry_run:
            for plan in report.plans:
                execute_plan(ui, client, plan, pattern, dry_run=True)
            render_summary(ui, report)
            ui.console.print(
                Panel(
                    "Dry run only — no assignments were created and no grades were posted. "
                    "Student counts and points above are projections.",
                    border_style="yellow",
                    box=box.ROUNDED,
                )
            )
            return 0

        new_count = sum(
            1 for p in processable for t in p.targets if t.existing_assignment_id is None
        )
        reuse_count = sum(
            1 for p in processable for t in p.targets if t.existing_assignment_id is not None
        )
        if not args.yes:
            message = (
                f"Create {new_count} assignment(s)"
                + (f", reuse {reuse_count}" if reuse_count else "")
                + " and post per-student CLO grades? They will stay unpublished."
            )
            if not ui.confirm(message, default=True):
                ui.warn("Aborted before any change was made.")
                return 0

        ui.step(6, 6, "Apply changes")
        for plan in report.plans:
            execute_plan(ui, client, plan, pattern)

        render_summary(ui, report)
        return 2 if report.errors else 0


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="canvas-split-assignment",
        description=(
            "Split rubric-graded Canvas assignments into one 'No Submission' "
            "assignment per CLO and post per-student CLO totals."
        ),
    )
    parser.add_argument("--url", help="Canvas base URL (skips the prompt)")
    parser.add_argument("--token", help="Canvas API access token (skips the prompt)")
    parser.add_argument(
        "--pattern", help="Regex capturing the CLO in a rubric criterion (skips the prompt)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="build and show the plan without creating assignments or posting grades",
    )
    parser.add_argument(
        "--all-courses",
        action="store_true",
        help="also list archived/concluded courses (hidden by default)",
    )
    parser.add_argument("--yes", action="store_true", help="skip the final confirmation prompt")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    ui = Ui()
    try:
        return run(ui, args)
    except KeyboardInterrupt:
        ui.console.print("\n[yellow]Cancelled — no further changes were made.[/]")
        return 130
    except (EOFError, OSError):
        ui.console.print(
            "\n[yellow]Input closed — this tool needs an interactive terminal "
            "for its prompts.[/]"
        )
        return 130
