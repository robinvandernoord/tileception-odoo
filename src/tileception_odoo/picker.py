"""Curses checklist for composing the project whitelist."""

from __future__ import annotations

import curses
import os
from dataclasses import dataclass

HELP = "Type to filter  Space/Tab toggle  ^A toggle shown  ^X clear all  Enter save  Esc cancel"


@dataclass
class Entry:
    id: int
    name: str
    note: str = ""

    def matches(self, query: str) -> bool:
        if not query:
            return True
        name = self.name.casefold()
        return query in name or query in name.replace(" ", "") or query == str(self.id)


def pick(entries: list[Entry], selected: set[int], excluded: set[int]) -> set[int] | None:
    """Return the chosen IDs, or None when cancelled."""
    if not entries:
        raise ValueError("Odoo returned no projects to choose from")
    # Make Esc respond immediately instead of waiting for a possible escape sequence.
    os.environ.setdefault("ESCDELAY", "25")
    return curses.wrapper(Picker(entries, set(selected), excluded).run)


class Picker:
    def __init__(self, entries: list[Entry], selected: set[int], excluded: set[int]):
        self.entries = entries
        self.selected = selected
        self.excluded = excluded
        self.query = ""
        self.cursor = 0
        self.top = 0

    def visible(self) -> list[Entry]:
        query = self.query.casefold()
        return [entry for entry in self.entries if entry.matches(query)]

    def run(self, screen) -> set[int] | None:
        curses.curs_set(0)
        curses.use_default_colors()
        screen.keypad(True)
        while True:
            shown = self.visible()
            self.cursor = max(0, min(self.cursor, len(shown) - 1))
            self.draw(screen, shown)
            key = screen.get_wch()
            rows = max(1, screen.getmaxyx()[0] - 4)
            if key in ("\n", "\r", curses.KEY_ENTER):
                return self.selected
            if key == "\x1b":
                return None
            if key in (" ", "\t") and shown:
                self.selected ^= {shown[self.cursor].id}
            elif key == "\x01" and shown:
                ids = {entry.id for entry in shown}
                self.selected = self.selected - ids if ids <= self.selected else self.selected | ids
            elif key == "\x18":
                self.selected.clear()
            elif key == curses.KEY_UP:
                self.cursor -= 1
            elif key == curses.KEY_DOWN:
                self.cursor += 1
            elif key == curses.KEY_PPAGE:
                self.cursor -= rows
            elif key == curses.KEY_NPAGE:
                self.cursor += rows
            elif key == curses.KEY_HOME:
                self.cursor = 0
            elif key == curses.KEY_END:
                self.cursor = len(shown) - 1
            elif key in (curses.KEY_BACKSPACE, "\x7f", "\b"):
                self.query = self.query[:-1]
                self.cursor = 0
            elif key == "\x15":  # Ctrl-U clears the filter, as in a shell prompt.
                self.query = ""
                self.cursor = 0
            elif isinstance(key, str) and key.isprintable():
                self.query += key
                self.cursor = 0

    def draw(self, screen, shown: list[Entry]):
        height, width = screen.getmaxyx()
        screen.erase()
        rows = max(1, height - 4)
        if self.cursor < self.top:
            self.top = self.cursor
        elif self.cursor >= self.top + rows:
            self.top = self.cursor - rows + 1
        count = len(self.selected)
        summary = f"{count} selected" if count else "nothing selected: all projects are synchronized"
        self.line(screen, 0, f"Projects to synchronize ({summary})", width, curses.A_BOLD)
        self.line(screen, 1, f"Filter: {self.query}", width)
        for row, entry in enumerate(shown[self.top : self.top + rows]):
            index = self.top + row
            mark = "[x]" if entry.id in self.selected else "[ ]"
            notes = [entry.note] if entry.note else []
            if entry.id in self.excluded:
                notes.append("blacklisted")
            suffix = f"  ({', '.join(notes)})" if notes else ""
            label = (
                f"{mark} {entry.name}  #{entry.id}{suffix}" if entry.id else f"{mark} {entry.name}{suffix}"
            )
            self.line(screen, row + 2, label, width, curses.A_REVERSE if index == self.cursor else 0)
        if not shown:
            self.line(screen, 2, "No projects match the filter", width, curses.A_DIM)
        self.line(screen, height - 1, HELP, width, curses.A_DIM)
        screen.refresh()

    @staticmethod
    def line(screen, y: int, text: str, width: int, attributes: int = 0):
        try:
            # Writing the bottom-right cell raises after the text is drawn; nothing is lost.
            screen.addnstr(y, 0, text, max(0, width - 1), attributes)
        except curses.error:
            pass
