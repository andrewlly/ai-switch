#!/usr/bin/env python3
"""The account chooser: a tmux-style full-screen list you arrow through.

`cca pick` opens every account at once with its live usage beside it, so the
question the tool actually gets asked — *which account should I use right
now?* — is answered by looking rather than by remembering. Enter launches the
highlighted one; the process is replaced by the agent exactly as `cca <name>`
would have done, so nothing wraps the session and nothing survives it.

The drawing is a pure function. `render()` takes reports, a cursor and a
terminal size and returns a list of lines; `run()` owns raw mode, keys and
repaint and holds no formatting logic of its own. That split is why the layout
has tests at all — the interesting part of a TUI is the part you cannot drive
from a test harness, so none of it lives there.

Two smaller decisions worth stating. The list never reorders itself while you
are looking at it: `b` *jumps* the cursor to the account with the most
headroom rather than sorting it to the top, because a list that rearranges
under a keypress is a list you cannot build muscle memory against. And the
FREE column is the headroom of the binding window, not an average of the two —
the same number `cca best` ranks on, so the picker and the CLI can never
disagree about which account is the good one.
"""

from __future__ import annotations

import os
import select
import shutil
import signal
import sys

try:  # absent on non-POSIX; run() refuses there and the module still imports
    import termios
    import tty
except ImportError:  # pragma: no cover - POSIX only in practice
    termios = tty = None

# Loaded as a sibling file rather than a package: cca is one script plus two
# modules in one directory, and both entry points reach it through a symlink.
if __package__:
    from . import usage as usage_mod  # pragma: no cover
else:  # pragma: no cover - exercised by every real invocation
    import importlib.util as _il
    _spec = _il.spec_from_file_location(
        "cca_usage", os.path.join(os.path.dirname(os.path.abspath(__file__)), "usage.py"))
    usage_mod = _il.module_from_spec(_spec)
    _spec.loader.exec_module(usage_mod)

ESC = "\x1b"
ALT_ON, ALT_OFF = f"{ESC}[?1049h", f"{ESC}[?1049l"
HIDE, SHOW = f"{ESC}[?25l", f"{ESC}[?25h"
CLEAR, HOME = f"{ESC}[2J", f"{ESC}[H"

DIM, BOLD, RESET, REVERSE = f"{ESC}[2m", f"{ESC}[1m", f"{ESC}[0m", f"{ESC}[7m"
GREEN, YELLOW, RED, CYAN = (f"{ESC}[32m", f"{ESC}[33m", f"{ESC}[31m", f"{ESC}[36m")

# Used-percentage bands. Green is room, yellow is "plan the rest of the day",
# red is "this account will stop mid-task".
WARN_PCT, CRIT_PCT = 60.0, 85.0

# The frame, the mark, and everything to the right of the two windows: FREE,
# RESETS and the gaps. Added to the name and plan columns, which are measured
# from the data, this is what a row costs before either bar.
_ROW_FIXED = 2 + 1 + 1 + 2 + 2 + 2 + 2 + 4 + 2 + 6

KEYS_QUIT = {"q", "\x03", "\x04"}
KEYS_UP = {"k", f"{ESC}[A", f"{ESC}OA"}
KEYS_DOWN = {"j", f"{ESC}[B", f"{ESC}OB"}
KEYS_ENTER = {"\r", "\n"}


def _bar_width(width: int, name_w: int, plan_w: int) -> int:
    """How wide each window's bar can be, given what the rest of the row needs.

    Both cells are five columns wide with no bar and `bar + 6` with one, so a
    bar of width *b* costs *b + 1* twice over. Measured rather than guessed at
    from the terminal width alone: a registry of long account names has less
    room for bars than one of short ones, at the same terminal size.
    """
    spare = width - (_ROW_FIXED + name_w + plan_w + 5 + 5)
    return usage_mod.bar_width_for(spare // 2 - 1)


def _tone(used_pct: float | None) -> str:
    if used_pct is None:
        return DIM
    if used_pct >= CRIT_PCT:
        return RED
    if used_pct >= WARN_PCT:
        return YELLOW
    return GREEN


def _cell_width(bar_width: int) -> int:
    """Bar, a space, and ` 100%` — or just the percentage when bars are off."""
    return bar_width + 6 if bar_width else 5


def _cell(window: dict | None, bar_width: int) -> tuple[str, str]:
    """One window's column: its colour, and its text at exactly _cell_width."""
    width = _cell_width(bar_width)
    if not window:
        return DIM, "-".rjust(width)
    text = f"{window['used_pct']:4.0f}%"
    if bar_width:
        text = f"{usage_mod.bar(window['used_pct'], bar_width)} {text}"
    return _tone(window["used_pct"]), text


def _note(report: dict) -> str:
    """The rightmost column: why a row is not simply a live number."""
    if not report.get("ok"):
        return report.get("error") or "unavailable"
    source = report.get("source")
    if source == "live":
        return ""
    age = usage_mod.fmt_age(report.get("stale_seconds"))
    if source == "rollout":
        return f"from last session, {age}"
    # Not "stale <age>": a fallback taken seconds after a good probe would read
    # "stale now", which says two things at once. What it is, is the last
    # reading there was, and when.
    reason = report.get("error")
    return f"last reading {age}" + (f" ({reason})" if reason else "")


def _pad(text: str, width: int) -> str:
    """Left-justify, truncating with an ellipsis rather than wrapping."""
    if len(text) <= width:
        return text.ljust(width)
    return (text[: width - 1] + "…") if width > 1 else text[:width]


def _frame(label: str, width: int, left: str, right: str) -> str:
    """A rule with a label let into it. The label keeps its own spacers — pad
    with the rule character, never strip back to the text."""
    inner = width - 2
    text = f" {label} " if label else ""
    if len(text) > inner:
        text = _pad(text, inner)
    return left + text + "─" * (inner - len(text)) + right


def render(reports, cursor: int, width: int = 80, height: int = 24, *,
           best_name: str | None = None, active: str | None = None,
           status: str = "", color: bool = True) -> list[str]:
    """The whole screen, as lines. Pure: no terminal, no clock, no I/O."""
    def paint(code: str, text: str) -> str:
        return f"{code}{text}{RESET}" if (color and code) else text

    width = max(40, width)
    if not reports:
        return [_frame("cca", width, "┌", "┐"),
                "│" + _pad("  no accounts registered — add one with `cca add <name>`",
                           width - 2) + "│",
                _frame("q quit", width, "└", "┘")]

    name_w = max(7, *(len(str(r.get("account") or "")) for r in reports))
    plan_w = max(4, *(len(str(r.get("plan") or "-")) for r in reports))
    bar_width = _bar_width(width, name_w, plan_w)
    cell_w = _cell_width(bar_width)
    # sel(1) + gap + name + gap + plan + gap + two cells + gap + free(4)
    #                                            + gap + resets(6) + gap
    fixed = 1 + 1 + name_w + 2 + plan_w + 2 + cell_w + 2 + cell_w + 2 + 4 + 2 + 6 + 2
    note_w = max(0, width - 2 - fixed)

    # Every label is padded, not ljust: with bars off the column is five wide
    # and "5-HOUR" is six, which is how a header drifts out of its own table.
    header = ("  " + _pad("ACCOUNT", name_w) + "  " + _pad("PLAN", plan_w) + "  "
              + _pad("5-HOUR" if bar_width else "5H", cell_w) + "  "
              + _pad("WEEK" if bar_width else "WK", cell_w) + "  "
              + "FREE" + "  " + "RESETS")

    live = sum(1 for r in reports if r.get("source") == "live")
    title = f"cca — {len(reports)} accounts, {live} live"
    lines = [_frame(title, width, "┌", "┐"),
             "│" + paint(BOLD, _pad(header, width - 2)) + "│"]

    # Frame, header, footer and any status line are not rows; the rest is, and
    # the window follows the cursor so the footer is never what gets cut.
    body = max(1, height - 3 - (1 if status else 0))
    top = 0 if len(reports) <= body else min(max(0, cursor - body // 2),
                                             len(reports) - body)
    shown = list(enumerate(reports))[top:top + body]

    for index, report in shown:
        selected = index == cursor
        # A selected row is reverse-video for its whole width, so it is drawn
        # without inner colour: every `[0m` that ends a coloured cell would
        # also end the reverse, leaving the highlight stopping mid-row.
        tint = (lambda _code, text: text) if selected else paint
        name = str(report.get("account") or "?")
        mark = "★" if name == best_name else ("•" if name == active else " ")
        five_tone, five = _cell((report.get("windows") or {}).get("five_hour"), bar_width)
        week_tone, week = _cell((report.get("windows") or {}).get("seven_day"), bar_width)

        free = report.get("free_pct")
        binding = (report.get("windows") or {}).get(report.get("binding"))
        row = (mark + " " + name.ljust(name_w) + "  "
               + _pad(str(report.get("plan") or "-"), plan_w) + "  "
               + tint(five_tone, five) + "  " + tint(week_tone, week) + "  "
               + (f"{free:3.0f}%" if free is not None else "   -") + "  "
               + usage_mod.fmt_duration(binding["resets_in"] if binding else None).rjust(6))
        if note_w:
            row += "  " + tint(DIM, _pad(_note(report), note_w))

        # Padded on the visible text, then coloured: escape codes have width 0
        # and counting them is how a selected row ends up one cell short.
        visible = len(_strip(row))
        if visible > width - 2:
            row, visible = _pad(row, width - 2), width - 2
        row += " " * (width - 2 - visible)
        lines.append("│" + (paint(REVERSE, row) if selected else row) + "│")

    if status:
        lines.append("│" + paint(CYAN, _pad("  " + status, width - 2)) + "│")
    more = len(reports) - len(shown)
    footer = "↑↓ move   ⏎ launch   b best   r refresh   q quit"
    if more:
        footer += f"   (+{more} more)"
    lines.append(_frame(footer, width, "└", "┘"))
    return lines


def _strip(text: str) -> str:
    """Text without SGR codes — what the terminal will actually show."""
    out, index = [], 0
    while index < len(text):
        if text[index] == ESC:
            while index < len(text) and text[index] not in "mKHJ":
                index += 1
            index += 1
            continue
        out.append(text[index])
        index += 1
    return "".join(out)


# -- the interactive part ------------------------------------------------- #


class _Keys:
    """One keypress at a time, off the raw fd.

    Two things force this rather than `sys.stdin.read(1)`. Text-layer reads
    buffer ahead, so the `[B` of an arrow lands in Python's buffer where
    `select` cannot see it and the arrow reads as a bare Escape — which is the
    quit key. And a fast keypress or a paste delivers several keys in one
    read, so what arrives has to be split rather than treated as one key.
    """

    # The byte that ends a CSI sequence, per ECMA-48: @ through ~.
    _FINAL = set("@ABCDEFGHIJKLMNOPQRSTUVWXYZ[\\]^_`abcdefghijklmnopqrstuvwxyz{|}~")

    def __init__(self, fd: int):
        self.fd = fd
        self.pending = ""

    def _fill(self, timeout: float | None) -> bool:
        ready, _, _ = select.select([self.fd], [], [], timeout)
        if not ready:
            return False
        try:
            data = os.read(self.fd, 256)
        except OSError:
            return False
        if not data:
            return False
        self.pending += data.decode("utf-8", "replace")
        return True

    def next(self, timeout: float | None = None) -> str | None:
        if not self.pending and not self._fill(timeout):
            return None
        if self.pending == ESC:
            # Either Escape itself or an arrow whose tail is still in flight.
            self._fill(0.03)
        return self._pop()

    def _pop(self) -> str:
        buf = self.pending
        if not buf.startswith(ESC) or len(buf) == 1 or buf[1] not in "[O":
            self.pending = buf[1:]
            return buf[0]
        end = 2
        while end < len(buf) and buf[end] not in self._FINAL:
            end += 1
        end = min(end + 1, len(buf))
        self.pending = buf[end:]
        return buf[:end]


def run(load, *, active: str | None = None, out=None) -> str | None:
    """Draw the picker until something is chosen. Returns an account name.

    `load()` returns the reports; it is called again on `r`, which is the only
    thing that touches the network once the screen is up.
    """
    if termios is None or not sys.stdin.isatty() or not sys.stdout.isatty():
        raise RuntimeError("the picker needs a terminal — try `cca usage` or `cca best`")

    out = out or sys.stdout
    reports = load()
    cursor, status = 0, ""
    resized = [False]

    def on_resize(*_):
        resized[0] = True

    previous = signal.signal(signal.SIGWINCH, on_resize)
    fd = sys.stdin.fileno()
    keys = _Keys(fd)
    saved = termios.tcgetattr(fd)
    out.write(ALT_ON + HIDE)
    try:
        tty.setraw(fd)
        while True:
            best = usage_mod.best(reports)
            size = shutil.get_terminal_size((80, 24))
            lines = render(reports, cursor, size.columns, size.lines,
                           best_name=(best or {}).get("account"), active=active,
                           status=status)
            out.write(CLEAR + HOME + "\r\n".join(lines) + "\r\n")
            out.flush()
            resized[0] = False

            # The wait is only long enough that a resize repaints promptly;
            # nothing else here polls.
            key = keys.next(timeout=0.5)
            if key is None:
                continue
            status = ""
            if key in KEYS_QUIT or key == ESC:
                return None
            if key in KEYS_UP:
                cursor = (cursor - 1) % len(reports) if reports else 0
            elif key in KEYS_DOWN:
                cursor = (cursor + 1) % len(reports) if reports else 0
            elif key == "g":
                cursor = 0
            elif key == "G":
                cursor = max(0, len(reports) - 1)
            elif key == "b":
                if best is None:
                    status = "no account could be measured"
                else:
                    cursor = next(i for i, r in enumerate(reports)
                                  if r.get("account") == best["account"])
            elif key == "r":
                out.write(CLEAR + HOME + "  refreshing…\r\n")
                out.flush()
                reports = load()
                cursor = min(cursor, max(0, len(reports) - 1))
            elif key.isdigit() and key != "0":
                index = int(key) - 1
                if index < len(reports):
                    cursor = index
            elif key in KEYS_ENTER:
                if not reports:
                    return None
                chosen = reports[cursor]
                if not chosen.get("ok"):
                    status = f"{chosen['account']}: {chosen.get('error')} — launching anyway is fine, press ⏎ again"
                    chosen["ok"] = True  # second Enter goes through
                    continue
                return chosen.get("account")
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        out.write(SHOW + ALT_OFF)
        out.flush()
        signal.signal(signal.SIGWINCH, previous)
