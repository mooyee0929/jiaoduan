"""A small Cocoa form app for testing the macOS backend.

Its model only learns about changes the way a real app does, from control actions and
text-change notifications, so a write that only changes what the control shows is caught.
The model is written to the JSON file named on the command line after every change.

Usage: python -m bench.fixtures.mac.fixture_app STATE.json
The app never activates itself and has no Dock icon; its window sits off screen.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import AppKit
import objc
from Foundation import NSObject

ACCESSORY = 1
OFF_SCREEN = (-4000.0, -4000.0)


def _label(text: str, frame: tuple[float, float, float, float]) -> Any:
    field = AppKit.NSTextField.labelWithString_(text)
    field.setFrame_(frame)
    return field


class Controller(NSObject):  # type: ignore[misc]
    def initWithPath_(self, path: str) -> Controller:  # noqa: N802
        self = objc.super(Controller, self).init()
        self.path = Path(path)
        self.model: dict[str, Any] = {
            "name": "", "subscribe": False, "size": "Small", "delivery": "", "saved": 0,
        }
        return self

    def build(self) -> None:
        style = AppKit.NSWindowStyleMaskTitled | AppKit.NSWindowStyleMaskClosable
        window = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            ((OFF_SCREEN[0], OFF_SCREEN[1]), (420.0, 260.0)), style,
            AppKit.NSBackingStoreBuffered, False,
        )
        window.setTitle_("Fixture Form")
        view = window.contentView()

        view.addSubview_(_label("Name", ((20, 210), (80, 20))))
        self.name = AppKit.NSTextField.alloc().initWithFrame_(((110, 208), (280, 24)))
        self.name.setAccessibilityLabel_("Name")
        self.name.setDelegate_(self)
        view.addSubview_(self.name)

        self.subscribe = AppKit.NSButton.checkboxWithTitle_target_action_(
            "Subscribe to news", self, "changed:"
        )
        self.subscribe.setFrame_(((20, 172), (200, 24)))
        view.addSubview_(self.subscribe)

        view.addSubview_(_label("Size", ((20, 136), (80, 20))))
        self.size = AppKit.NSPopUpButton.alloc().initWithFrame_pullsDown_(
            ((110, 132), (160, 26)), False
        )
        self.size.addItemsWithTitles_(["Small", "Medium", "Large"])
        self.size.setAccessibilityLabel_("Size")
        self.size.setTarget_(self)
        self.size.setAction_("changed:")
        view.addSubview_(self.size)

        view.addSubview_(_label("Delivery method", ((20, 120), (160, 20))))
        self.pickup = AppKit.NSButton.radioButtonWithTitle_target_action_(
            "Pickup", self, "changed:"
        )
        self.pickup.setFrame_(((20, 96), (100, 24)))
        self.courier = AppKit.NSButton.radioButtonWithTitle_target_action_(
            "Courier", self, "changed:"
        )
        self.courier.setFrame_(((130, 96), (100, 24)))
        view.addSubview_(self.pickup)
        view.addSubview_(self.courier)

        save = AppKit.NSButton.buttonWithTitle_target_action_("Save", self, "save:")
        save.setFrame_(((300, 20), (100, 32)))
        save.setKeyEquivalent_("\r")
        view.addSubview_(save)

        self.window = window
        window.orderFrontRegardless()
        window.setFrameOrigin_(OFF_SCREEN)
        self.write()

    def write(self) -> None:
        self.path.write_text(json.dumps(self.model), encoding="utf-8")

    def controlTextDidChange_(self, notification: Any) -> None:  # noqa: N802
        self.model["name"] = str(self.name.stringValue())
        self.write()

    def changed_(self, sender: Any) -> None:
        self.model["subscribe"] = bool(self.subscribe.state())
        self.model["size"] = str(self.size.titleOfSelectedItem() or "")
        if self.pickup.state():
            self.model["delivery"] = "Pickup"
        elif self.courier.state():
            self.model["delivery"] = "Courier"
        self.write()

    def save_(self, sender: Any) -> None:
        self.model["saved"] += 1
        self.write()


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: fixture_app.py STATE.json")
    app = AppKit.NSApplication.sharedApplication()
    app.setActivationPolicy_(ACCESSORY)
    controller = Controller.alloc().initWithPath_(sys.argv[1])
    controller.build()
    app.run()


if __name__ == "__main__":
    main()
