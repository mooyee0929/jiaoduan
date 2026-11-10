"""Turn key chords like "shift+Tab" or "cmd+a" into CDP key events."""

from __future__ import annotations

import sys
from dataclasses import dataclass

from agentlayer.errors import INVALID_ARGS, LayerError

_ALT, _CTRL, _META, _SHIFT = 1, 2, 4, 8
_MODIFIERS = {
    "alt": _ALT, "option": _ALT, "ctrl": _CTRL, "control": _CTRL,
    "cmd": _META, "command": _META, "meta": _META, "shift": _SHIFT,
}
_NAMED: dict[str, tuple[str, str, int, str]] = {
    "enter": ("Enter", "Enter", 13, "\r"), "return": ("Enter", "Enter", 13, "\r"),
    "tab": ("Tab", "Tab", 9, ""), "escape": ("Escape", "Escape", 27, ""),
    "esc": ("Escape", "Escape", 27, ""), "backspace": ("Backspace", "Backspace", 8, ""),
    "delete": ("Delete", "Delete", 46, ""), "space": (" ", "Space", 32, " "),
    "up": ("ArrowUp", "ArrowUp", 38, ""), "arrowup": ("ArrowUp", "ArrowUp", 38, ""),
    "down": ("ArrowDown", "ArrowDown", 40, ""), "arrowdown": ("ArrowDown", "ArrowDown", 40, ""),
    "left": ("ArrowLeft", "ArrowLeft", 37, ""), "arrowleft": ("ArrowLeft", "ArrowLeft", 37, ""),
    "right": ("ArrowRight", "ArrowRight", 39, ""),
    "arrowright": ("ArrowRight", "ArrowRight", 39, ""),
    "home": ("Home", "Home", 36, ""), "end": ("End", "End", 35, ""),
    "pageup": ("PageUp", "PageUp", 33, ""), "pagedown": ("PageDown", "PageDown", 34, ""),
}
# Chrome needs editing shortcuts named as commands; on macOS a Meta key event alone does nothing.
_MAC_COMMANDS = {"a": "selectAll", "c": "copy", "v": "paste", "x": "cut", "z": "undo"}


@dataclass(frozen=True)
class KeyStroke:
    key: str
    code: str
    virtual: int
    text: str
    modifiers: int
    commands: tuple[str, ...] = ()

    @property
    def is_return(self) -> bool:
        return self.key == "Enter" and not self.modifiers & (_CTRL | _META | _ALT)


def _character(token: str, modifiers: int) -> tuple[str, str, int, str, tuple[str, ...]]:
    upper = token.upper()
    if token.isascii() and token.isalpha():
        code = f"Key{upper}"
    elif token.isdigit():
        code = f"Digit{token}"
    else:
        code = ""
    typed = "" if modifiers & (_CTRL | _META | _ALT) else token
    commands: tuple[str, ...] = ()
    if modifiers & _META and sys.platform == "darwin" and token.lower() in _MAC_COMMANDS:
        commands = (_MAC_COMMANDS[token.lower()],)
    return token, code, ord(upper[0]), typed, commands


def parse_chord(chord: str) -> KeyStroke:
    parts = [p for p in chord.split("+") if p] if chord != "+" else ["+"]
    if not parts:
        raise LayerError(INVALID_ARGS, f"empty key chord {chord!r}")
    modifiers = 0
    for part in parts[:-1]:
        flag = _MODIFIERS.get(part.lower())
        if flag is None:
            raise LayerError(INVALID_ARGS, f"unknown modifier {part!r} in {chord!r}")
        modifiers |= flag
    last = parts[-1]
    named = _NAMED.get(last.lower())
    if named is not None:
        key, code, virtual, text = named
        return KeyStroke(key, code, virtual, text, modifiers)
    if len(last) != 1:
        raise LayerError(INVALID_ARGS, f"unknown key {last!r} in {chord!r}")
    key, code, virtual, text, commands = _character(last, modifiers)
    return KeyStroke(key, code, virtual, text, modifiers, commands)


def parse_keys(keys: str) -> list[KeyStroke]:
    """One or more chords separated by spaces."""
    strokes = [parse_chord(chord) for chord in keys.split()]
    if not strokes:
        raise LayerError(INVALID_ARGS, "keys is empty")
    return strokes
