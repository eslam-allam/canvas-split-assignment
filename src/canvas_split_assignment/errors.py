"""Errors raised while talking to Canvas."""


class CanvasError(Exception):
    """A user-facing error while talking to Canvas."""


class CanvasAuthError(CanvasError):
    """The access token was rejected."""


class CanvasAPIError(CanvasError):
    """Canvas answered with an unexpected status or payload."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status
