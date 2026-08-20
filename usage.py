#!/usr/bin/env python3
"""How much of each account's rate-limit windows is already spent.

Every plan meters two rolling windows — a five-hour one and a seven-day one —
and an account is only as usable as its *tightest* one: 5% of the five-hour
window is worth nothing behind a weekly window at 99%. So every report here
names a `binding` window and scores the account by that window's headroom
alone. That score is what `cca best` sorts on and what the picker's FREE
column shows.

**The numbers are fetched live.** `GET /api/oauth/usage` with the account's own
bearer token is the same call Claude Code's `/usage` makes, and it is the only
source that is true *now*: a cached figure is true as of whenever that account
last ran, which for the idle account you are trying to switch *to* is exactly
when it is least informative. Accounts are probed in parallel because the
picker opens on the result.

A cache is still written after every successful probe, and read back only when
a live probe fails — never as a first choice, and always labelled `stale` with
its age, because a number with an unstated age is worse than no number. Two
corrections make an old number honest: a window whose `resets_at` has passed is
reported as 0% (it rolled over while nobody was looking), and everything else
carries `stale_seconds`.

Codex has no equivalent endpoint. Its limits arrive as a `rate_limits` event
inside the session rollout it writes as it works, so a codex account is read
from the newest rollout on disk and reported with `source: "rollout"`. That is
a cache by another name and it is labelled as one.

Stdlib only, and importable on its own: `probe`/`collect` take plain dicts, not
registry objects, so a service can load this module by path.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
# The scope Claude Code's own OAuth calls announce. Without it the endpoint
# answers 401 for a subscription token.
OAUTH_BETA = "oauth-2025-04-20"

DEFAULT_TIMEOUT = 6.0
MAX_WORKERS = 8
# The endpoint returns a few KB. Anything past this is not that response.
READ_CAP = 1024 * 1024

# The two windows every report speaks in, and what to call them in a column
# three characters wide.
WINDOWS = ("five_hour", "seven_day")
SHORT = {"five_hour": "5h", "seven_day": "wk"}

# Codex reports one window at a time and says how long it is. Anything up to a
# day is the short window; a week-long one is the weekly.
_SHORT_WINDOW_MINUTES = 24 * 60


class UsageError(RuntimeError):
    """A probe that failed in a way the operator should read."""


# -- small helpers -------------------------------------------------------- #


def _read_json(path: Path) -> dict | None:
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _parse_iso(text: str | None) -> float | None:
    """`2026-08-20T12:19:59.85+00:00` -> epoch seconds."""
    if not isinstance(text, str) or not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _pct(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return max(0.0, min(100.0, float(value)))


def cache_path() -> Path:
    """Our own cache, beside the registry — never inside an agent's space."""
    root = Path(os.environ.get("CCA_HOME", Path.home() / ".claude-accounts"))
    return root / "usage-cache.json"


# -- the shape every source is normalised into ---------------------------- #


def _window(used_pct: float | None, resets_at: float | None, now: float) -> dict | None:
    """One rolling window, corrected for having already rolled over.

    A `resets_at` in the past is the one piece of self-repair a stale figure
    allows: that window emptied at that moment, whatever it read before.
    """
    if used_pct is None:
        return None
    rolled = resets_at is not None and resets_at <= now
    if rolled:
        used_pct, resets_at = 0.0, None
    return {
        "used_pct": round(used_pct, 1),
        "free_pct": round(100.0 - used_pct, 1),
        "resets_at": resets_at,
        "resets_in": None if resets_at is None else max(0, int(resets_at - now)),
        "rolled_over": rolled,
    }


def _score(windows: dict) -> tuple[str | None, float | None]:
    """The binding window and its headroom — the whole ranking, in one line."""
    known = [(name, w) for name, w in windows.items() if w]
    if not known:
        return None, None
    name, window = min(known, key=lambda item: item[1]["free_pct"])
    return name, window["free_pct"]


def _report(spec: dict, *, source: str, as_of: float, windows: dict,
            credits: dict | None = None, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    binding, free = _score(windows)
    return {
        "account": spec.get("name"),
        "tool": spec.get("tool"),
        "plan": spec.get("plan"),
        "email": spec.get("email"),
        "ok": True,
        "error": None,
        "source": source,
        "checked_at": now,
        "as_of": as_of,
        "stale_seconds": max(0, int(now - as_of)),
        "windows": windows,
        "binding": binding,
        "free_pct": free,
        "credits": credits,
    }


def _failed(spec: dict, error: str, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    return {
        "account": spec.get("name"),
        "tool": spec.get("tool"),
        "plan": spec.get("plan"),
        "email": spec.get("email"),
        "ok": False,
        "error": error,
        "source": None,
        "checked_at": now,
        "as_of": None,
        "stale_seconds": None,
        "windows": {name: None for name in WINDOWS},
        "binding": None,
        "free_pct": None,
        "credits": None,
    }


def _credits(block) -> dict | None:
    """Extra-usage credits, in whole currency units rather than minor ones."""
    if not isinstance(block, dict) or not block.get("is_enabled"):
        return None
    places = block.get("decimal_places")
    scale = 10 ** places if isinstance(places, int) else 100
    used, limit = block.get("used_credits"), block.get("monthly_limit")
    return {
        "used": round(used / scale, 2) if isinstance(used, (int, float)) else None,
        "limit": round(limit / scale, 2) if isinstance(limit, (int, float)) else None,
        "used_pct": _pct(block.get("utilization")),
        "currency": block.get("currency") or "USD",
        "exhausted": bool(block.get("spend_limit_reached")),
    }


def parse_utilization(payload: dict, spec: dict, *, source: str,
                      as_of: float, now: float | None = None) -> dict:
    """The `/api/oauth/usage` body (live or cached) -> one report.

    Only `five_hour`, `seven_day` and `extra_usage` are read. The response also
    carries a dozen nulls under codenames for limits this account does not have
    and a parallel `limits` array saying the same thing twice; reading the two
    named windows keeps this from tracking a shape that is mostly placeholder.
    """
    now = time.time() if now is None else now
    windows = {}
    for name in WINDOWS:
        block = payload.get(name)
        block = block if isinstance(block, dict) else {}
        windows[name] = _window(_pct(block.get("utilization")),
                                _parse_iso(block.get("resets_at")), now)
    return _report(spec, source=source, as_of=as_of, windows=windows,
                   credits=_credits(payload.get("extra_usage")), now=now)


# -- live: the Claude usage endpoint -------------------------------------- #


def _retry_after(exc: urllib.error.HTTPError) -> str:
    """`retry in 108s`, when the 429 says so.

    Raw seconds, not `fmt_duration` — this is a countdown somebody is about to
    wait out, and rounding 108s to "1m" would have them come back too early.
    """
    try:
        seconds = int((exc.headers or {}).get("Retry-After", ""))
    except (TypeError, ValueError):
        return "retry shortly"
    return f"retry in {seconds}s"


def _bearer(spec: dict) -> str:
    creds = _read_json(Path(spec["credentials_file"])) or {}
    oauth = creds.get("claudeAiOauth") or {}
    token = oauth.get("accessToken")
    if not token:
        raise UsageError("not logged in")
    return token


def fetch_live(spec: dict, timeout: float = DEFAULT_TIMEOUT,
               opener=urllib.request.urlopen) -> dict:
    """`GET /api/oauth/usage` for one Claude account. Raises UsageError."""
    request = urllib.request.Request(USAGE_URL, headers={
        "Authorization": f"Bearer {_bearer(spec)}",
        "Content-Type": "application/json",
        "anthropic-beta": OAUTH_BETA,
        "User-Agent": "cca (account manager)",
    })
    try:
        with opener(request, timeout=timeout) as response:
            body = response.read(READ_CAP)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise UsageError("token rejected — log in again") from exc
        if exc.code == 429:
            # The endpoint that *reports* your limits has a limit of its own,
            # on how often you may ask. Deliberately not called "rate-limited"
            # in a tool whose whole subject is rate limits — read on a usage
            # row, that phrasing says the account is spent, which is the one
            # thing it does not mean. It is a cooldown of a couple of minutes
            # on asking, and the server says how long, so quote it.
            raise UsageError(f"usage API busy, {_retry_after(exc)}") from exc
        raise UsageError(f"HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise UsageError(f"unreachable ({exc.reason})") from exc
    except OSError as exc:
        raise UsageError(str(exc)) from exc

    try:
        payload = json.loads(body)
    except ValueError as exc:
        raise UsageError("unreadable response") from exc
    if not isinstance(payload, dict):
        raise UsageError("unreadable response")
    return payload


# -- fallbacks: only ever reached when a live probe failed ---------------- #


def _from_our_cache(spec: dict, now: float) -> dict | None:
    entry = (_read_json(cache_path()) or {}).get(spec.get("name"))
    if not isinstance(entry, dict) or not isinstance(entry.get("payload"), dict):
        return None
    return parse_utilization(entry["payload"], spec, source="cache",
                             as_of=float(entry.get("at") or 0), now=now)


def _from_agent_cache(spec: dict, now: float) -> dict | None:
    """What Claude Code itself last saw, in the account's own config file."""
    cached = (_read_json(Path(spec["config_file"])) or {}).get("cachedUsageUtilization")
    if not isinstance(cached, dict):
        return None
    payload = cached.get("utilization")
    fetched = cached.get("fetchedAtMs")
    if not isinstance(payload, dict) or not isinstance(fetched, (int, float)):
        return None
    return parse_utilization(payload, spec, source="cache",
                             as_of=fetched / 1000.0, now=now)


def _newest_rollout(config_dir: Path) -> Path | None:
    sessions = config_dir / "sessions"
    if not sessions.is_dir():
        return None
    newest, newest_at = None, -1.0
    for path in sessions.rglob("rollout-*.jsonl"):
        try:
            stamp = path.stat().st_mtime
        except OSError:
            continue
        if stamp > newest_at:
            newest, newest_at = path, stamp
    return newest


def _from_rollout(spec: dict, now: float) -> dict | None:
    """Codex writes its limits into the session it is running; read the last.

    Scanned from the end because the useful event is the most recent one and a
    long session's rollout runs to tens of megabytes.
    """
    path = _newest_rollout(Path(spec["config_dir"]))
    if path is None:
        return None
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None

    for line in reversed(lines):
        if '"rate_limits"' not in line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        limits = _find_key(event, "rate_limits")
        if not isinstance(limits, dict):
            continue
        windows = {name: None for name in WINDOWS}
        for slot in ("primary", "secondary"):
            block = limits.get(slot)
            if not isinstance(block, dict):
                continue
            minutes = block.get("window_minutes")
            name = ("five_hour" if isinstance(minutes, (int, float))
                    and minutes <= _SHORT_WINDOW_MINUTES else "seven_day")
            resets = block.get("resets_at")
            windows[name] = _window(
                _pct(block.get("used_percent")),
                float(resets) if isinstance(resets, (int, float)) else None, now)
        if not any(windows.values()):
            continue
        as_of = path.stat().st_mtime
        return _report(spec, source="rollout", as_of=as_of, windows=windows, now=now)
    return None


def _find_key(node, key: str):
    """The rollout nests its payload differently across versions; go find it."""
    if isinstance(node, dict):
        if key in node:
            return node[key]
        for value in node.values():
            found = _find_key(value, key)
            if found is not None:
                return found
    elif isinstance(node, list):
        for value in node:
            found = _find_key(value, key)
            if found is not None:
                return found
    return None


# -- one account, then all of them ---------------------------------------- #


def _remember(name: str, payload: dict, at: float) -> None:
    """Keep the last good body so a later failure has something to fall back to.

    Stamped with the probe's own clock, not `time.time()` — the age this cache
    reports later has to be measured against the same reading that produced it.
    """
    path = cache_path()
    store = _read_json(path) or {}
    store[name] = {"at": at, "payload": payload}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(store, indent=2), encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(path)
    except OSError:
        pass  # a cache that cannot be written is not worth failing a probe over


def probe(spec: dict, timeout: float = DEFAULT_TIMEOUT, *, live: bool = True,
          opener=urllib.request.urlopen, now: float | None = None) -> dict:
    """One account's usage: live if it can be, labelled stale if it cannot."""
    now = time.time() if now is None else now

    if spec.get("tool") == "codex":
        # No endpoint to call — the rollout is all there is, and it says so.
        return _from_rollout(spec, now) or _failed(
            spec, "no usage recorded yet (codex reports it only while running)", now)

    # With `live=False` the caller asked for the cache, so there is no error
    # to report — the source and the age already say everything.
    error = None
    if live:
        try:
            payload = fetch_live(spec, timeout, opener=opener)
        except UsageError as exc:
            error = str(exc)
        else:
            _remember(spec["name"], payload, now)
            return parse_utilization(payload, spec, source="live", as_of=now, now=now)

    for fallback in (_from_our_cache, _from_agent_cache):
        report = fallback(spec, now)
        if report is not None:
            report["error"] = error
            return report
    return _failed(spec, error, now)


def collect(specs, timeout: float = DEFAULT_TIMEOUT, *, live: bool = True,
            opener=urllib.request.urlopen) -> list[dict]:
    """Probe every account at once — the picker opens on the slowest one."""
    specs = list(specs)
    if not specs:
        return []
    if len(specs) == 1:
        return [probe(specs[0], timeout, live=live, opener=opener)]
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(specs))) as pool:
        return list(pool.map(
            lambda spec: probe(spec, timeout, live=live, opener=opener), specs))


# -- ranking -------------------------------------------------------------- #


def rank(reports) -> list[dict]:
    """Most headroom first: the binding window decides, the other breaks ties.

    An account that could not be measured sorts last whatever it might have
    had — `best` picking an account on no evidence is how you land in a window
    that was already full.
    """
    def key(report):
        if not report.get("ok") or report.get("free_pct") is None:
            return (1, 0.0, 0.0, report.get("account") or "")
        other = [w["free_pct"] for name, w in report["windows"].items()
                 if w and name != report["binding"]]
        return (0, -report["free_pct"], -(max(other) if other else 0.0),
                report.get("account") or "")

    return sorted(reports, key=key)


def best(reports) -> dict | None:
    """The account to use next, or None if nothing could be measured."""
    ranked = rank(reports)
    if not ranked:
        return None
    top = ranked[0]
    return top if top.get("ok") and top.get("free_pct") is not None else None


# -- formatting shared by the CLI and the picker -------------------------- #


def fmt_duration(seconds: float | None) -> str:
    """`41m`, `2h 04m`, `13h`, `2d 7h`, `now`.

    Never wider than six characters — the picker lays this column out at a
    fixed width, and a seventh character there pushes the frame's right edge
    off the row. Precision is dropped from the least significant end as the
    number grows, which is also the end nobody is reading by then.
    """
    if seconds is None:
        return "-"
    seconds = int(seconds)
    if seconds <= 0:
        return "now"
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        hours, minutes = divmod(seconds // 60, 60)
        return f"{hours}h {minutes:02d}m" if hours < 10 else f"{hours}h"
    days, hours = divmod(seconds // 3600, 24)
    return f"{days}d {hours}h" if days < 10 else f"{days}d"


def fmt_age(seconds: float | None) -> str:
    """How long ago, phrased to sit after a noun: `just now`, `2h 04m ago`."""
    if seconds is None:
        return "-"
    return "just now" if seconds < 45 else fmt_duration(seconds) + " ago"


# How wide a bar gets, by how much room the layout has left for one. Below the
# floor there is no bar at all — three cells cannot show a percentage, and a
# stub that always looks half full is worse than the number on its own.
BAR_MAX = 10
BAR_MIN = 4


def bar_width_for(available: int) -> int:
    """A bar that fits `available` columns, or 0 when one would not be honest."""
    if available >= BAR_MAX:
        return BAR_MAX
    return available if available >= BAR_MIN else 0


def bar(used_pct: float | None, width: int = 8, filled: str = "▇",
        empty: str = "▁") -> str:
    """A used-fraction bar. Any non-zero use shows at least one block, so an
    account at 2% never renders as visually empty."""
    if used_pct is None:
        return "?" * width
    cells = int(round(used_pct / 100.0 * width))
    if used_pct > 0:
        cells = max(1, cells)
    cells = min(width, cells)
    return filled * cells + empty * (width - cells)
