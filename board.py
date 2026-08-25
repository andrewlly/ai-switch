#!/usr/bin/env python3
"""The shared task board a ccfleet works through.

Claude Code's own agent teams give a lead a task list its teammates claim from,
with dependencies and automatic delivery - but every teammate is spawned by the
lead process and runs on the lead's account, and nothing outside Claude Code
can join. This is the same idea built where a mixed fleet can reach it: one
JSON file, one CLI, no daemon.

The protocol is pull, not push. Pushing work into an agent means typing into
its terminal and hoping the keystrokes landed; pulling means the agent runs a
command, which is the one capability claude, codex and anything else all have.
So a member's whole loop is:

    ccfleet board next --as worker-db --wait 240   # blocks until there is work
    ...do it...
    ccfleet board done t3 --note "..."             # and around again

`next` blocking is what removes the dispatcher: an idle member is a member
sitting in a shell call, not one that has to be woken. It returns 4 when the
wait elapsed with nothing claimable (call again) and 5 when the board is
drained (stop), so an agent needs no clock of its own.

Every mutation takes an exclusive lock and rewrites the file atomically, which
is what makes two members claiming at the same moment safe.
"""

from __future__ import annotations

import fcntl
import json
import os
import time
from pathlib import Path

VERSION = 1

# What a task can be. `pending` is claimable, `claimed` is being worked on,
# `review` is waiting for a checker, and the last three are terminal.
STATES = ("pending", "claimed", "review", "accepted", "done", "blocked")
TERMINAL = ("accepted", "blocked")
ROLES = ("worker", "checker")

# Exit codes `next` uses, so an agent can branch on them without parsing prose.
GOT_TASK = 0
NO_TASK_YET = 4
DRAINED = 5


def role_of(member: str) -> str:
    """A member's role is in its name - `worker-db`, `checker-review`.

    The window name already carries it, so a member never has to be told what
    it is, and cannot claim work belonging to the other role by mistake.
    """
    head = member.split("-", 1)[0]
    return head if head in ROLES else "worker"


class Board:
    """The task list, and every rule about who may take what.

    Nothing here talks to tmux or to an agent: a board is a file, and the only
    reason it can coordinate a mixed fleet is that it never assumes anything
    about who is reading it.
    """

    def __init__(self, path: Path):
        self.path = path

    # -- storage ---------------------------------------------------------

    def exists(self) -> bool:
        return self.path.is_file()

    def blank(self, goal: str = "", auto_review: bool = False) -> dict:
        """An empty board. Public because `ccfleet up` writes the first one
        through its own dry-run seam, and a second hand-written copy of this
        shape is a field that goes missing the day one is added."""
        return {"version": VERSION, "goal": goal, "auto_review": auto_review,
                "closed": False, "seq": 0, "tasks": []}

    def read(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return self.blank()

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(f".tmp.{os.getpid()}")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n",
                       encoding="utf-8")
        os.replace(tmp, self.path)

    def update(self, fn):
        """Run `fn(data)` under an exclusive lock and write the result.

        The lock is a separate file, not the board itself: the board is
        replaced by rename on every write, and a lock held on a file that gets
        replaced is a lock on nothing.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock = self.path.with_suffix(".lock")
        with open(lock, "a+") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                data = self.read() if self.exists() else self.blank()
                result = fn(data)
                self._write(data)
                return result
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

    # -- building --------------------------------------------------------

    def init(self, goal: str, auto_review: bool) -> dict:
        data = self.blank(goal, auto_review)
        self._write(data)
        return data

    def add(self, title: str, brief: str = "", role: str = "worker",
            deps: list[str] | None = None, files: str = "",
            reviews: str | None = None) -> str:
        def apply(data):
            data["seq"] = data.get("seq", 0) + 1
            task_id = f"t{data['seq']}"
            data["tasks"].append({
                "id": task_id,
                "title": title,
                "brief": brief,
                "files": files,
                "role": role if role in ROLES else "worker",
                "deps": list(deps or []),
                "reviews": reviews,       # the task this one reviews, if any
                "state": "pending",
                "owner": None,
                "attempts": 0,
                "notes": [],
            })
            return task_id
        return self.update(apply)

    # -- the rules -------------------------------------------------------

    @staticmethod
    def _by_id(data: dict, task_id: str) -> dict | None:
        for task in data["tasks"]:
            if task["id"] == task_id:
                return task
        return None

    @staticmethod
    def _settled(task: dict) -> bool:
        """Has this task got as far as it is going to get?

        `accepted` and `done` both satisfy a dependency: `done` is what a task
        reaches on a board with no checker, and a dependent should not wait for
        a review that will never happen.
        """
        return task["state"] in ("accepted", "done")

    def _claimable(self, data: dict, role: str) -> list[dict]:
        settled = {t["id"] for t in data["tasks"] if self._settled(t)}
        return [t for t in data["tasks"]
                if t["state"] == "pending" and t["role"] == role
                and all(dep in settled for dep in t["deps"])]

    def _drained(self, data: dict) -> bool:
        """True when no further work can ever appear - and only then.

        Two conditions, and the first is the one that is easy to forget. An
        open board can still grow: decomposition is a series of `add` calls,
        not one, so a board that looks empty may simply not have been filled
        yet - which is exactly the state every member is in at launch. Calling
        that drained told each of them to stop before the orchestrator had
        written its first task. So a board is drained only once somebody has
        said there will be no more tasks.

        The second: a claimed task means somebody is still working, and what
        they do next may unblock more, so a board with one member busy is not
        drained however little else is left.
        """
        if not data.get("closed"):
            return False
        return not any(t["state"] in ("pending", "claimed", "review")
                       for t in data["tasks"])

    # -- the verbs a member calls ----------------------------------------

    def claim(self, member: str) -> tuple[int, dict | None]:
        """Take the next task this member may have. Atomic by construction."""
        role = role_of(member)

        def apply(data):
            ready = self._claimable(data, role)
            if ready:
                task = ready[0]
                task["state"] = "claimed"
                task["owner"] = member
                task["attempts"] += 1
                return GOT_TASK, task
            return (DRAINED if self._drained(data) else NO_TASK_YET), None
        return self.update(apply)

    def wait_claim(self, member: str, seconds: float,
                   poll: float = 2.0, sleep=time.sleep,
                   clock=time.monotonic) -> tuple[int, dict | None]:
        """Block until there is work for this member, or the board drains.

        The clock lives here rather than in the agent: an agent asked to wait
        does it by burning a turn, and an agent asked to poll forgets. A shell
        call that simply does not return until there is something to do costs
        nothing and cannot drift.
        """
        deadline = clock() + max(0.0, seconds)
        while True:
            code, task = self.claim(member)
            if code in (GOT_TASK, DRAINED):
                return code, task
            if clock() >= deadline:
                return NO_TASK_YET, None
            sleep(min(poll, max(0.0, deadline - clock())))

    def done(self, task_id: str, member: str = "", note: str = "") -> dict:
        """A member reports a task finished.

        On a board with a checker this does not end the task: it moves to
        `review` and a review task appears for the checker. The work is not
        done because the worker says so - which is the entire point of having
        one.
        """
        def apply(data):
            task = self._by_id(data, task_id)
            if task is None:
                raise KeyError(task_id)
            if note:
                task["notes"].append(note)
            if data.get("auto_review") and task["role"] == "worker":
                task["state"] = "review"
                data["seq"] = data.get("seq", 0) + 1
                data["tasks"].append({
                    "id": f"t{data['seq']}", "title": f"review: {task['title']}",
                    "brief": "", "files": task.get("files", ""),
                    "role": "checker", "deps": [], "reviews": task["id"],
                    "state": "pending", "owner": None, "attempts": 0,
                    "notes": [],
                })
            else:
                task["state"] = "done"
            return dict(task)
        return self.update(apply)

    def accept(self, task_id: str, note: str = "") -> dict:
        def apply(data):
            task = self._by_id(data, task_id)
            if task is None:
                raise KeyError(task_id)
            task["state"] = "accepted"
            if note:
                task["notes"].append(f"accepted: {note}")
            for other in data["tasks"]:
                if other.get("reviews") == task_id and other["state"] != "accepted":
                    other["state"] = "done"
            return dict(task)
        return self.update(apply)

    def reject(self, task_id: str, why: str) -> dict:
        """Send work back. The task becomes claimable again, carrying the why.

        Deliberately `pending` and not assigned back to its previous owner: the
        member that did it may be out of budget, or gone. The next member free
        picks it up, and the feedback is in the task rather than in a
        conversation only one of them had.
        """
        def apply(data):
            task = self._by_id(data, task_id)
            if task is None:
                raise KeyError(task_id)
            task["state"] = "pending"
            task["owner"] = None
            task["notes"].append(f"rejected: {why}")
            for other in data["tasks"]:
                if other.get("reviews") == task_id and other["state"] != "accepted":
                    other["state"] = "done"
            return dict(task)
        return self.update(apply)

    def block(self, task_id: str, why: str) -> dict:
        """Abandon a task - and everything that was waiting on it.

        The cascade is not tidiness. A pending task whose dependency is blocked
        can never be claimed, but it is still `pending`, so the board never
        drains and every member sits on "nothing yet, try again" forever. Work
        that has become unreachable has to say so, or the fleet deadlocks
        quietly with a full task list.
        """
        def apply(data):
            task = self._by_id(data, task_id)
            if task is None:
                raise KeyError(task_id)
            task["state"] = "blocked"
            task["notes"].append(f"blocked: {why}")
            dead = {task_id}
            changed = True
            while changed:                      # transitively, to a fixpoint
                changed = False
                for other in data["tasks"]:
                    # Only `pending` cascades. A claimed task is in a member's
                    # hands and will reach a terminal state when it reports, so
                    # it cannot deadlock the board - and finished work is not
                    # retroactively undone by a later block of what it needed.
                    if other["state"] != "pending" or other["id"] in dead:
                        continue
                    stranded = [d for d in other["deps"] if d in dead]
                    if not stranded:
                        continue
                    other["state"] = "blocked"
                    other["notes"].append(
                        f"blocked: depends on {', '.join(stranded)}, "
                        f"which cannot finish")
                    dead.add(other["id"])
                    changed = True
            return dict(task)
        return self.update(apply)

    def set_goal(self, goal: str) -> dict:
        """Record what the fleet is for, once somebody has said.

        A goal given at launch is on the board already; one typed to the
        orchestrator afterwards is not, and a board whose `list` cannot say
        what it is for is a board nobody else can pick up.
        """
        def apply(data):
            data["goal"] = goal
            return {"goal": goal}
        return self.update(apply)

    def close(self) -> dict:
        """Declare decomposition finished: no more tasks are coming.

        Until this is called every member waits rather than stopping, which is
        the safe way round - a member that waits too long is visible in
        `list`, a member that stopped early is a fleet that quietly did
        nothing.
        """
        def apply(data):
            data["closed"] = True
            return {"closed": True, "drained": self._drained(data)}
        return self.update(apply)

    def reopen(self) -> dict:
        """More work turned out to be needed. Members waiting stay waiting."""
        def apply(data):
            data["closed"] = False
            return {"closed": False}
        return self.update(apply)

    # -- reading ---------------------------------------------------------

    def summary(self) -> dict:
        data = self.read()
        counts = {state: 0 for state in STATES}
        for task in data["tasks"]:
            counts[task["state"]] = counts.get(task["state"], 0) + 1
        return {"goal": data.get("goal", ""),
                "auto_review": bool(data.get("auto_review")),
                "closed": bool(data.get("closed")),
                "counts": counts, "drained": self._drained(data),
                "tasks": data["tasks"]}
