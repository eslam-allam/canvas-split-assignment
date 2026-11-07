"""Canvas REST client: authentication, pagination and gentle retries.

Endpoint reference (Canvas LMS REST API):
  GET  /api/v1/users/self
  GET  /api/v1/courses                                  (Link-header pagination)
  GET  /api/v1/courses/:course_id/assignments
  GET  /api/v1/courses/:course_id/assignments/:id       (includes `rubric`)
  GET  /api/v1/courses/:course_id/assignments/:id/submissions
        ?include[]=rubric_assessment&include[]=user
  POST /api/v1/courses/:course_id/assignments           (assignment[submission_types][]=none)
  PUT  /api/v1/courses/:course_id/assignments/:aid/submissions/:uid
        (submission[posted_grade])
"""

from __future__ import annotations

import re
import time
from collections.abc import Iterator
from typing import Any

import httpx

from .errors import CanvasAPIError, CanvasAuthError, CanvasError
from .models import CourseInfo
from .scoring import format_points

API_PREFIX = "/api/v1"
PER_PAGE = 100
USER_AGENT = "canvas-split-assignment/0.1"

#: Attempts for idempotent requests (GET / PUT).
GET_ATTEMPTS = 5
#: Attempts for non-idempotent requests (POST); only throttling is retried.
POST_ATTEMPTS = 2


def normalize_base_url(raw: str) -> tuple[str, list[str]]:
    """Return a clean Canvas base URL plus human-readable notes about fixes."""
    notes: list[str] = []
    url = (raw or "").strip()
    if not url:
        raise CanvasError("The Canvas base URL cannot be empty.")
    if not re.match(r"^https?://", url, re.IGNORECASE):
        url = "https://" + url
        notes.append("no scheme given — assuming https://")
    match = re.match(r"^(https?://)(.*)$", url, re.IGNORECASE)
    assert match is not None
    scheme, rest = match.group(1), match.group(2).rstrip("/")
    if rest.lower().endswith("/api/v1"):
        rest = rest[: -len("/api/v1")].rstrip("/")
        notes.append("stripped a trailing '/api/v1' from the URL")
    if not rest:
        raise CanvasError("The Canvas base URL cannot be empty.")
    return scheme + rest, notes


def _next_link(link_header: str | None) -> str | None:
    if not link_header:
        return None
    for part in link_header.split(","):
        match = re.match(r'\s*<([^>]+)>\s*;\s*rel="?([^";]+)"?', part)
        if match and match.group(2) == "next":
            return match.group(1)
    return None


def _error_message(response: httpx.Response) -> str:
    """Best-effort extraction of Canvas' error message."""
    try:
        body = response.json()
    except ValueError:
        return (response.text or "").strip()[:160] or "no error message"
    if isinstance(body, dict):
        errors = body.get("errors")
        if isinstance(errors, list):
            messages = []
            for item in errors:
                if isinstance(item, dict):
                    messages.append(str(item.get("message") or item))
                else:
                    messages.append(str(item))
            if messages:
                return "; ".join(messages)
        if isinstance(errors, dict):
            return "; ".join(f"{k}: {v}" for k, v in errors.items())
        for key in ("message", "error_message", "error"):
            if body.get(key):
                return str(body[key])
    return str(body)[:160]


class CanvasClient:
    """Small Canvas REST client with pagination and gentle retries."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.base_url, _ = normalize_base_url(base_url)
        self.token = token.strip()
        self._client = httpx.Client(
            transport=transport,
            timeout=timeout,
            follow_redirects=True,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/json",
                "User-Agent": USER_AGENT,
            },
        )

    # -- plumbing ---------------------------------------------------------- #

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> CanvasClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _url(self, path: str) -> str:
        if path.startswith(("http://", "https://")):
            return path
        return f"{self.base_url}{API_PREFIX}{path}"

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        idempotent: bool = True,
    ) -> httpx.Response:
        url = self._url(path)
        attempts = GET_ATTEMPTS if idempotent else POST_ATTEMPTS
        for attempt in range(1, attempts + 1):
            try:
                response = self._client.request(method, url, params=params, data=data)
            except httpx.TimeoutException as exc:
                if attempt < attempts:
                    time.sleep(min(2**attempt, 8))
                    continue
                raise CanvasError(
                    f"Timed out contacting {url} — check the base URL and your connection."
                ) from exc
            except httpx.HTTPError as exc:
                if attempt < attempts:
                    time.sleep(min(2**attempt, 8))
                    continue
                raise CanvasError(f"Network error contacting {url}: {exc}") from exc

            status = response.status_code
            if status == 429 or status >= 500:
                if attempt < attempts:
                    retry_after = response.headers.get("Retry-After")
                    try:
                        delay = float(retry_after) if retry_after else min(2**attempt, 8)
                    except ValueError:
                        delay = min(2**attempt, 8)
                    time.sleep(max(delay, 1.0))
                    continue
                raise CanvasAPIError(
                    f"Canvas returned {status} for {method} {url} after {attempts} attempts.",
                    status=status,
                )
            if status == 401:
                raise CanvasAuthError(
                    f"Canvas rejected the access token (401) for {url}. "
                    "Check that the token is valid and not expired."
                )
            if status == 403:
                raise CanvasAPIError(
                    f"Permission denied (403) for {method} {url}. "
                    "You need grading rights (teacher/TA/admin) in this course.",
                    status=status,
                )
            if status == 404:
                raise CanvasAPIError(
                    f"Not found (404) for {method} {url} — check the base URL and ids.",
                    status=status,
                )
            if not response.is_success:
                raise CanvasAPIError(
                    f"Canvas returned {status} for {method} {url}: {_error_message(response)}",
                    status=status,
                )
            return response
        raise CanvasError(f"Request failed: {method} {url}")  # pragma: no cover

    @staticmethod
    def _json(response: httpx.Response, url: str) -> Any:
        try:
            return response.json()
        except ValueError as exc:
            body = (response.text or "").strip()[:120]
            raise CanvasError(
                f"Expected JSON from {url} but got "
                f"{response.headers.get('content-type', '?')!r}. "
                "Check that the base URL points at your Canvas instance. "
                f"Response starts with: {body!r}"
            ) from exc

    def _paginate(self, path: str, params: dict[str, Any] | None = None) -> Iterator[dict]:
        response = self._request("GET", path, params=params)
        while True:
            payload = self._json(response, self._url(path))
            if not isinstance(payload, list):
                raise CanvasAPIError(
                    f"Expected a list from {self._url(path)} but got {type(payload).__name__}."
                )
            for item in payload:
                if isinstance(item, dict):
                    yield item
            nxt = _next_link(response.headers.get("Link") or response.headers.get("link"))
            if not nxt:
                return
            response = self._request("GET", nxt)

    # -- endpoints --------------------------------------------------------- #

    def me(self) -> dict[str, Any]:
        response = self._request("GET", "/users/self")
        payload = self._json(response, self._url("/users/self"))
        if not isinstance(payload, dict):
            raise CanvasAPIError("Unexpected response for /users/self.")
        return payload

    def list_courses(self) -> list[CourseInfo]:
        courses: list[CourseInfo] = []
        for item in self._paginate(
            "/courses",
            params={
                "include[]": ["term", "total_students", "concluded"],
                "per_page": PER_PAGE,
            },
        ):
            term = item.get("term") if isinstance(item.get("term"), dict) else None
            courses.append(
                CourseInfo(
                    id=int(item.get("id") or 0),
                    name=str(item.get("name") or item.get("original_name") or "(unnamed course)"),
                    code=str(item.get("course_code") or ""),
                    workflow_state=str(item.get("workflow_state") or ""),
                    term=str(term.get("name")) if term else None,
                    concluded=bool(item.get("concluded")),
                )
            )
        courses.sort(
            key=lambda c: (c.archived, c.term is None, (c.term or "").lower(), c.name.lower())
        )
        return courses

    def list_assignments(self, course_id: int) -> list[dict[str, Any]]:
        return list(
            self._paginate(
                f"/courses/{course_id}/assignments",
                params={"per_page": PER_PAGE, "order_by": "name"},
            )
        )

    def get_assignment(self, course_id: int, assignment_id: int) -> dict[str, Any]:
        path = f"/courses/{course_id}/assignments/{assignment_id}"
        response = self._request("GET", path)
        payload = self._json(response, self._url(path))
        if not isinstance(payload, dict):
            raise CanvasAPIError(f"Unexpected response for assignment {assignment_id}.")
        return payload

    def list_submissions(self, course_id: int, assignment_id: int) -> list[dict[str, Any]]:
        return list(
            self._paginate(
                f"/courses/{course_id}/assignments/{assignment_id}/submissions",
                params={
                    "include[]": ["rubric_assessment", "user"],
                    "per_page": PER_PAGE,
                },
            )
        )

    def create_assignment(self, course_id: int, data: dict[str, Any]) -> dict[str, Any]:
        response = self._request(
            "POST", f"/courses/{course_id}/assignments", data=data, idempotent=False
        )
        payload = self._json(response, self._url(f"/courses/{course_id}/assignments"))
        if not isinstance(payload, dict):
            raise CanvasAPIError("Unexpected response when creating an assignment.")
        return payload

    def post_grade(
        self, course_id: int, assignment_id: int, user_id: int, points: float
    ) -> dict[str, Any]:
        path = f"/courses/{course_id}/assignments/{assignment_id}/submissions/{user_id}"
        response = self._request(
            "PUT",
            path,
            data={"submission[posted_grade]": format_points(points)},
        )
        payload = self._json(response, self._url(path))
        return payload if isinstance(payload, dict) else {}
