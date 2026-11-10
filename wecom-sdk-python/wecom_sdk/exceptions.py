"""Exceptions raised by the asynchronous WeCom SDK."""


class WeComError(Exception):
    """Base error for transport and API failures."""

    def __init__(self, message: str, *, errcode: int | None = None, payload=None):
        super().__init__(message)
        self.errcode = errcode
        self.payload = payload


class WeComAPIError(WeComError):
    """WeCom returned a non-zero errcode."""


class WeComHTTPError(WeComError):
    """The HTTP request failed before a usable WeCom response was received."""
