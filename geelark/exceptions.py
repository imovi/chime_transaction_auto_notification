"""Custom exceptions for GeeLark API interactions."""

from typing import Any, Optional


class GeeLarkError(Exception):
    """Base exception for GeeLark SDK."""
    pass


class GeeLarkAPIError(GeeLarkError):
    """Exception raised when GeeLark API returns a non-zero status code."""

    def __init__(
        self,
        code: int,
        message: str,
        trace_id: Optional[str] = None,
        data: Optional[Any] = None,
    ):
        self.code = code
        self.message = message
        self.trace_id = trace_id
        self.data = data
        super().__init__(f"GeeLark API error [code={code}, traceId={trace_id}]: {message}")


class GeeLarkAuthenticationError(GeeLarkAPIError):
    """Raised on authentication/signature/token failure."""
    pass


class GeeLarkRateLimitError(GeeLarkAPIError):
    """Raised when rate limit (40007 / 40014) is exceeded."""
    pass


class GeeLarkDeviceError(GeeLarkAPIError):
    """Raised on device specific errors (not found, expired, in use)."""
    pass
