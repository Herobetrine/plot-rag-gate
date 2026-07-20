"""Typed errors returned by the initialization service."""

from __future__ import annotations

from typing import Any


class PlotInitError(RuntimeError):
    """An initialization protocol error with a stable machine-readable code."""

    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "status": "failed",
            "code": self.code,
            "reason": self.message,
        }
        if self.details:
            payload["details"] = self.details
        return payload
