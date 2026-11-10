"""Errors an agent can act on: a code, what was delivered, and what to do next."""

from __future__ import annotations

from jiaoduan.model import Delivery

INVALID_ARGS = "INVALID_ARGS"
STALE_REF = "STALE_REF"
AMBIGUOUS = "AMBIGUOUS"
COMMIT_GATED = "COMMIT_GATED"
COMMIT_DENIED = "COMMIT_DENIED"
SURFACE_CLAIMED = "SURFACE_CLAIMED"
NO_SURFACE = "NO_SURFACE"
NO_ACCESSIBLE_TREE = "NO_ACCESSIBLE_TREE"
BLOCKED = "BLOCKED"
NOT_ACTIONABLE = "NOT_ACTIONABLE"
NOT_SUPPORTED = "NOT_SUPPORTED"
BACKEND_UNAVAILABLE = "BACKEND_UNAVAILABLE"
PERMISSION_DENIED = "PERMISSION_DENIED"
ADAPTER_DRIFT = "ADAPTER_DRIFT"
NOT_FOUND = "NOT_FOUND"


class LayerError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        delivery: Delivery = Delivery.NOT_DELIVERED,
        hint: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.delivery = delivery
        self.hint = hint

    def render(self) -> str:
        text = f"{self.code}: {self.message} [{self.delivery.value}]"
        return f"{text}\nhint: {self.hint}" if self.hint else text
