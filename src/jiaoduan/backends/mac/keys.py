"""Key chords as macOS virtual key codes, posted to one process rather than the frontmost app."""

from __future__ import annotations

from dataclasses import dataclass

from jiaoduan.errors import INVALID_ARGS, LayerError

_FLAGS = {
    "cmd": 1 << 20, "command": 1 << 20, "meta": 1 << 20, "shift": 1 << 17,
    "ctrl": 1 << 18, "control": 1 << 18, "alt": 1 << 19, "option": 1 << 19,
}
_NAMED = {
    "enter": 36, "return": 36, "tab": 48, "space": 49, "backspace": 51, "escape": 53, "esc": 53,
    "delete": 117, "home": 115, "end": 119, "pageup": 116, "pagedown": 121, "left": 123,
    "arrowleft": 123, "right": 124, "arrowright": 124, "down": 125, "arrowdown": 125, "up": 126,
    "arrowup": 126,
}
_CHARACTERS = {
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8, "v": 9, "b": 11,
    "q": 12, "w": 13, "e": 14, "r": 15, "y": 16, "t": 17, "1": 18, "2": 19, "3": 20, "4": 21,
    "6": 22, "5": 23, "=": 24, "9": 25, "7": 26, "-": 27, "8": 28, "0": 29, "]": 30, "o": 31,
    "u": 32, "[": 33, "i": 34, "p": 35, "l": 37, "j": 38, "'": 39, "k": 40, ";": 41,
    "\\": 42, ",": 43, "/": 44, "n": 45, "m": 46, ".": 47, "`": 50,
}
# Quitting or closing someone's app is never a side effect of a key press.
_REFUSED = {(1 << 20, 12), (1 << 20, 13)}


@dataclass(frozen=True)
class MacStroke:
    keycode: int
    flags: int
    name: str

    @property
    def is_return(self) -> bool:
        return self.keycode == 36 and not self.flags

    @property
    def is_escape(self) -> bool:
        return self.keycode == 53 and not self.flags


def parse_chord(chord: str) -> MacStroke:
    parts = [p for p in chord.split("+") if p]
    if not parts:
        raise LayerError(INVALID_ARGS, f"empty key chord {chord!r}")
    flags = 0
    for part in parts[:-1]:
        flag = _FLAGS.get(part.lower())
        if flag is None:
            raise LayerError(INVALID_ARGS, f"unknown modifier {part!r} in {chord!r}")
        flags |= flag
    last = parts[-1].lower()
    keycode = _NAMED.get(last, _CHARACTERS.get(last))
    if keycode is None:
        raise LayerError(INVALID_ARGS, f"unknown key {parts[-1]!r} in {chord!r}")
    if (flags, keycode) in _REFUSED:
        raise LayerError(INVALID_ARGS, f"{chord} would quit or close the app; refused")
    return MacStroke(keycode, flags, chord)


def parse_keys(keys: str) -> list[MacStroke]:
    strokes = [parse_chord(chord) for chord in keys.split()]
    if not strokes:
        raise LayerError(INVALID_ARGS, "keys is empty")
    return strokes
