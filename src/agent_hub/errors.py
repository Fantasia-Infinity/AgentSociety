from __future__ import annotations

from http import HTTPStatus


class ApiError(Exception):
    """Error raised at the API facade boundary with an HTTP status.

    Every interface (REST, MCP, A2A, Web) translates this single error type
    into its own wire format; business code never raises it directly.
    """

    def __init__(
        self,
        message: str,
        status: HTTPStatus = HTTPStatus.BAD_REQUEST,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


def map_error(error: Exception) -> ApiError:
    """Map the kernel's exception vocabulary onto ApiError."""

    if isinstance(error, ApiError):
        return error
    if isinstance(error, LookupError):
        return ApiError(str(error), HTTPStatus.NOT_FOUND)
    if isinstance(error, PermissionError):
        return ApiError(str(error), HTTPStatus.CONFLICT)
    return ApiError(str(error), HTTPStatus.BAD_REQUEST)
