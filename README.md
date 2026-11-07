# Canvas Split Assignment

Split rubric-graded Canvas assignments into **one “No Submission” assignment per CLO**
and post each student's rubric points per CLO.

For every assignment you select, the tool:

1. reads the assignment's rubric and extracts a CLO from each criterion using a
   regular expression you provide;
2. creates one **No Submission** assignment per unique CLO, named
   `<assignment name> - [<CLO>]` — points possible equal the CLO's rubric points,
   counted toward the final grade;
3. walks the graded submissions, splits each student's rubric points per CLO, and
   posts the totals as that student's grade in the matching CLO assignment.

### How grades are posted

Grades are sent with the **bulk grading endpoint**
(`POST /api/v1/courses/:id/assignments/:id/submissions/update_grades`) — **one
request per assignment** instead of one request per student. Canvas applies them
in a background job and the tool waits for it to finish, then **reads the grades
back** so the summary reflects what Canvas actually stored. Students Canvas
cannot grade (e.g. inactive enrollments, which the per-student endpoint rejects
with `403 user not authorized to perform that action`) are reported by name.
If a Canvas server lacks the bulk endpoint, the tool falls back to per-student
requests automatically.

> **Note on publishing:** Canvas rejects grades on unpublished assignments
> (403 "This assignment is still unpublished" — `Submission#can_grade_symbolic_status`),
> so each CLO assignment is **published automatically right before** its grades are
> posted. To keep the new columns and grades hidden from students longer, set the
> course's grade posting policy to *Manual* before running.

### Grade-integrity rules

* A submission is only split when its recorded grade equals the total of its
  rubric assessment (within `0.01` points).
* If the grade is lower than the rubric total by **exactly** the automatic
  late/missing penalty (`points_deducted`), the submission is kept and the
  penalty is **spread evenly across the CLOs** (rounding remainder goes to the
  largest CLO so the totals always add up to the recorded grade).
* Anything else — extra credit, manual override, a rubric not used for grading —
  is **warned about and skipped**, so the CLO columns always reconcile with the
  gradebook.
* Ungraded, excused, and rubric-less submissions are counted and reported, never posted.
* Rubric criteria the pattern cannot match are excluded and reported.

Re-running is safe: an assignment named `<assignment name> - [<CLO>]` that already
exists in the course is **reused** instead of duplicated.

## Install

As a global command with [uv](https://docs.astral.sh/uv/):

```bash
uv tool install .
```

That gives you `canvas-split-assignment` (alias: `canvas-clo-split`).

Or run it straight from the project without installing:

```bash
uv run canvas-clo-split            # or: uv run canvas-split-assignment
uv run python -m canvas_split_assignment
```

## Usage

```bash
canvas-clo-split
```

You will be prompted for:

1. **Canvas base URL** — e.g. `https://canvas.school.edu` (a missing scheme or a
   trailing `/api/v1` is fixed for you; `CANVAS_BASE_URL` is offered as default).
2. **Access token** — a Canvas API token with grading rights
   (`CANVAS_ACCESS_TOKEN` is offered if set).
3. **CLO regex** — a pattern that captures just the CLO in a rubric criterion's
   description, e.g. `(CLO\s*\d+)` or `CLO[:\s-]*(\S+)`. Group 1 is used as the
   CLO (the whole match if the pattern has no group). The criterion's
   `description` is tried first, then `long_description`.
4. **Courses** — multi-select (space to toggle, enter to confirm). Archived /
   concluded courses are hidden by default (Canvas returns them for teacher-type
   accounts; `--all-courses` includes them, marked `[archived]`). Unpublished
   courses are shown and marked `[unpublished]`.
5. **Assignments** — multi-select per course; assignments with a rubric are marked.

Then review the plan and confirm. A full summary (created/reused assignments,
students graded, points posted, warnings, errors) is printed at the end.

### Options

| Flag | Effect |
| --- | --- |
| `--url URL` | Canvas base URL (skips the prompt) |
| `--token TOKEN` | Canvas API access token (skips the prompt) |
| `--pattern REGEX` | CLO capture pattern (skips the prompt) |
| `--dry-run` | build the plan and project results without creating assignments or posting grades |
| `--all-courses` | also list archived/concluded courses (hidden by default) |
| `--yes` | skip the final confirmation prompt |

Tip: run `canvas-clo-split --dry-run` first to see exactly what would be created
and the projected per-student CLO totals — nothing is written to Canvas.

Exit codes: `0` success, `1` aborted/connection failure, `2` finished with errors.

> **About 403 errors:** Canvas rate limiting answers `429` (the tool retries
> those automatically). A `403 user not authorized to perform that action` on a
> grade means Canvas refuses to grade that *student* — typically an inactive or
> rejected enrollment (e.g. a student who dropped the course). Those students
> are skipped and reported; nothing else is affected.

## Development

```bash
uv sync --group dev     # install deps + pytest
uv run pytest           # run the test suite (unit + API-mocked end-to-end)
```

Layout:

```
src/canvas_split_assignment/
  api.py       Canvas REST client (pagination, retries, error mapping)
  models.py    shared dataclasses
  scoring.py   pure CLO mapping / per-submission scoring logic
  ui.py        rich rendering + questionary prompts
  cli.py       prompts, planning, execution, entry point
tests/
  test_core.py       unit tests for the scoring rules
  test_pipeline.py   end-to-end tests against a fake Canvas API
```
