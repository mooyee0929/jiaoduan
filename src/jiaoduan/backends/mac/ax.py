"""The macOS Accessibility API, behind a Protocol so everything above it can be tested."""

from __future__ import annotations

from collections.abc import Sequence
from types import SimpleNamespace
from typing import Any, Protocol

AX_SUCCESS = 0
AX_INVALID_ELEMENT = -25202
AX_CANNOT_COMPLETE = -25204
AX_ATTRIBUTE_UNSUPPORTED = -25205
AX_ACTION_UNSUPPORTED = -25206
AX_API_DISABLED = -25211

MESSAGING_TIMEOUT = 3.0
MANUAL_ACCESSIBILITY = "AXManualAccessibility"
ENHANCED_USER_INTERFACE = "AXEnhancedUserInterface"
_FRAMEWORK = "/System/Library/Frameworks/ApplicationServices.framework"


class AXApi(Protocol):
    def trusted(self) -> bool: ...

    def app(self, pid: int) -> Any: ...

    def attribute(self, element: Any, name: str) -> Any: ...

    def attributes(self, element: Any, names: Sequence[str]) -> dict[str, Any]: ...

    def settable(self, element: Any, name: str) -> bool: ...

    def set(self, element: Any, name: str, value: Any) -> int: ...

    def actions(self, element: Any) -> list[str]: ...

    def perform(self, element: Any, action: str) -> int: ...


def _bridge() -> SimpleNamespace:
    import objc

    ns: dict[str, Any] = {}
    bundle = objc.loadBundle("ApplicationServices", ns, bundle_path=_FRAMEWORK)
    objc.loadBundleFunctions(
        bundle, ns, [("AXUIElementGetTypeID", b"Q"), ("AXValueGetTypeID", b"Q")]
    )
    objc.registerCFSignature("AXUIElementRef", b"^{__AXUIElement=}", ns["AXUIElementGetTypeID"]())
    objc.registerCFSignature("AXValueRef", b"^{__AXValue=}", ns["AXValueGetTypeID"]())
    element = b"^{__AXUIElement=}"
    objc.loadBundleFunctions(bundle, ns, [
        ("AXIsProcessTrusted", b"Z"),
        ("AXUIElementCreateApplication", element + b"i"),
        ("AXUIElementCopyAttributeValue", b"i" + element + b"@o^@"),
        ("AXUIElementCopyMultipleAttributeValues", b"i" + element + b"@Io^@"),
        ("AXUIElementSetAttributeValue", b"i" + element + b"@@"),
        ("AXUIElementIsAttributeSettable", b"i" + element + b"@o^Z"),
        ("AXUIElementPerformAction", b"i" + element + b"@"),
        ("AXUIElementCopyActionNames", b"i" + element + b"o^@"),
        ("AXUIElementSetMessagingTimeout", b"i" + element + b"f"),
    ])
    return SimpleNamespace(**ns)


def _plain(value: Any) -> Any:
    """Geometry, ranges and per-attribute errors arrive wrapped; none of them are wanted here."""
    return None if type(value).__name__ == "AXValueRef" else value


class SystemAX:
    def __init__(self) -> None:
        self._fn = _bridge()

    def trusted(self) -> bool:
        return bool(self._fn.AXIsProcessTrusted())

    def app(self, pid: int) -> Any:
        element = self._fn.AXUIElementCreateApplication(pid)
        # An app that hangs would otherwise block every call for the 6 s default.
        self._fn.AXUIElementSetMessagingTimeout(element, MESSAGING_TIMEOUT)
        return element

    def attribute(self, element: Any, name: str) -> Any:
        error, value = self._fn.AXUIElementCopyAttributeValue(element, name, None)
        return _plain(value) if error == AX_SUCCESS else None

    def attributes(self, element: Any, names: Sequence[str]) -> dict[str, Any]:
        error, values = self._fn.AXUIElementCopyMultipleAttributeValues(
            element, list(names), 0, None
        )
        if error != AX_SUCCESS or values is None:
            return {}
        return {name: _plain(value) for name, value in zip(names, values, strict=False)}

    def settable(self, element: Any, name: str) -> bool:
        error, ok = self._fn.AXUIElementIsAttributeSettable(element, name, None)
        return error == AX_SUCCESS and bool(ok)

    def set(self, element: Any, name: str, value: Any) -> int:
        return int(self._fn.AXUIElementSetAttributeValue(element, name, value))

    def actions(self, element: Any) -> list[str]:
        error, names = self._fn.AXUIElementCopyActionNames(element, None)
        if error != AX_SUCCESS or names is None:
            return []
        return [str(n) for n in names]

    def perform(self, element: Any, action: str) -> int:
        return int(self._fn.AXUIElementPerformAction(element, action))
