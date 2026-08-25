#!/usr/bin/env python3
"""ccfleet - a tmux fleet of coding-agent sessions.

One orchestrator session drives a fleet of *separate* agent processes - not
in-process subagents - each in its own tmux window and its own account space:

    workers    one git worktree and branch each, so parallel edits never collide
    checkers   no worktree; they read the workers' diffs and run their tests
    orch       whichever account you are already on, in the main checkout

Every window is launched through `cca`/`cxa`, so each session gets its
account's environment from the account manager itself rather than inheriting
the tmux server's - which is the trap that makes hand-rolled tmux fleets read
the wrong config directory.

Nothing about a fleet is declared in a state file: the tmux session holds the
window list and git holds the branches and worktrees, so `status` and `down`
report what is actually there, including a fleet some other shell started.

    ccfleet up -w work -w codex -c client     start a fleet
    ccfleet status                            what each window and branch is doing
    ccfleet down                              kill it; keep any worktree with work in it

Stdlib only. Reads cca's registry for account names; never writes to it.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve()


def _load_cca():
    """Load cca.py from beside this script.

    Same reason cca's own `sibling()` spells the path out: this directory is
    reached through symlinks in ~/.local/bin, so importing by name would
    depend on which of those paths Python picked as sys.path[0].
    """
    path = SCRIPT.parent / "cca.py"
    if not path.is_file():
        print(f"error: cca.py not found beside {SCRIPT}", file=sys.stderr)
        sys.exit(1)
    spec = importlib.util.spec_from_file_location("cca", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_sibling(name: str):
    """Load a module from beside this script, by path - see _load_cca."""
    import importlib.util
    path = SCRIPT.parent / f"{name}.py"
    if not path.is_file():
        print(f"error: {name}.py not found beside {SCRIPT}", file=sys.stderr)
        sys.exit(1)
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cca = _load_cca()
board = _load_sibling("board")
c = cca.c
die = cca.die
warn = cca.warn
info = cca.info
tilde = cca.tilde

MANUAL = SCRIPT.parent / "FLEET.md"

DEFAULT_SESSION = "fleet"
BRANCH_PREFIX = "fleet/"
FLEET_SUFFIX = "-fleet"          # worktrees live in <repo>-fleet/, beside the repo

# Window names, tmux session names and branch names all come from these, so the
# character set is the intersection of what all three take without quoting.
LABEL_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$")
SESSION_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$")

ROLES = ("orch", "worker", "checker")


# --------------------------------------------------------------------------
# running things
# --------------------------------------------------------------------------

def probe(argv: list[str], cwd: Path | None = None) -> tuple[int, str]:
    """Run a read-only command. Returns (returncode, stdout stripped).

    Reads happen even under --dry-run: a plan is only worth printing if it was
    computed against the real repository.
    """
    try:
        p = subprocess.run(argv, cwd=str(cwd) if cwd else None,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           text=True)
    except OSError as exc:
        return 127, str(exc)
    return p.returncode, (p.stdout or "").strip()


class Doer:
    """Runs the mutating git and tmux commands, or prints them under --dry-run.

    The one seam the tests use: a whole fleet can be planned and asserted on
    without a tmux server or a real launch.
    """

    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run
        self.commands: list[list[str]] = []
        self.writes: list[Path] = []

    def write(self, path: Path, text: str, what: str) -> None:
        """Create a file the fleet needs. Printed with `*`, not `+`: a
        --dry-run's `+` lines are commands you could paste, and this is not
        one of them."""
        self.writes.append(path)
        if self.dry_run:
            print(f"  {c.dim}*{c.reset} write {path}    "
                  f"{c.dim}({what}, {len(text.splitlines())} lines){c.reset}")
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def __call__(self, argv: list[str], *, cwd: Path | None = None,
                 check: bool = True) -> int:
        self.commands.append(list(argv))
        if self.dry_run:
            line = shlex.join(argv)
            if cwd:
                line += f"    {c.dim}# in {tilde(cwd)}{c.reset}"
            print(f"  {c.dim}+{c.reset} {line}")
            return 0
        p = subprocess.run(argv, cwd=str(cwd) if cwd else None,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           text=True)
        if p.returncode and check:
            out = (p.stdout or "").strip()
            die(f"command failed ({p.returncode}): {shlex.join(argv)}"
                + (f"\n{out}" if out else ""))
        return p.returncode


# --------------------------------------------------------------------------
# git
# --------------------------------------------------------------------------

def git_root(start: Path) -> Path:
    rc, out = probe(["git", "-C", str(start), "rev-parse", "--show-toplevel"])
    if rc:
        die(f"{tilde(start)} is not inside a git repository")
    return Path(out)


def main_worktree(anywhere: Path) -> Path:
    """The repository's primary checkout, even when called from a worktree.

    `git worktree list` always names the main worktree first, so a fleet run
    from inside one of its own worker worktrees still finds the real repo.
    """
    for wt in list_worktrees(anywhere):
        return wt["path"]
    return git_root(anywhere)


def list_worktrees(repo: Path) -> list[dict]:
    rc, out = probe(["git", "-C", str(repo), "worktree", "list", "--porcelain"])
    if rc:
        return []
    entries, cur = [], {}
    for line in out.splitlines():
        if not line.strip():
            if cur:
                entries.append(cur)
                cur = {}
            continue
        key, _, value = line.partition(" ")
        if key == "worktree":
            cur = {"path": Path(value), "branch": None, "head": None,
                   "detached": False, "prunable": False}
        elif key == "branch":
            cur["branch"] = value.removeprefix("refs/heads/")
        elif key == "HEAD":
            cur["head"] = value
        elif key == "detached":
            cur["detached"] = True
        elif key == "prunable":
            cur["prunable"] = True
    if cur:
        entries.append(cur)
    return entries


def resolve_rev(repo: Path, ref: str) -> tuple[str, str]:
    """(full sha, human description) for a ref, or die."""
    rc, sha = probe(["git", "-C", str(repo), "rev-parse", "--verify", f"{ref}^{{commit}}"])
    if rc:
        die(f"no such commit-ish in {tilde(repo)}: {ref}")
    _, short = probe(["git", "-C", str(repo), "rev-parse", "--short", sha])
    _, name = probe(["git", "-C", str(repo), "rev-parse", "--abbrev-ref", ref])
    label = short or sha[:8]
    # Name the commit if there is a name worth printing: the branch it is on,
    # or the ref that was asked for - but never the sha twice.
    for candidate in (name, ref):
        if candidate and candidate not in ("HEAD", sha) and not sha.startswith(candidate):
            label += f" ({candidate})"
            break
    return sha, label


def branch_exists(repo: Path, branch: str) -> bool:
    rc, _ = probe(["git", "-C", str(repo), "rev-parse", "--verify", "--quiet",
                   f"refs/heads/{branch}"])
    return rc == 0


def is_dirty(worktree: Path) -> bool:
    """Uncommitted or untracked content - anything a removal would destroy."""
    rc, out = probe(["git", "-C", str(worktree), "status", "--porcelain"])
    return rc != 0 or bool(out)


def is_ancestor(repo: Path, maybe: str, of: str) -> bool:
    rc, _ = probe(["git", "-C", str(repo), "merge-base", "--is-ancestor", maybe, of])
    return rc == 0


def commits_ahead(repo: Path, base: str, branch: str) -> int:
    rc, out = probe(["git", "-C", str(repo), "rev-list", "--count",
                     f"{base}..{branch}"])
    return int(out) if rc == 0 and out.isdigit() else -1


def fleet_dir_for(repo: Path) -> Path:
    return repo.parent / (repo.name + FLEET_SUFFIX)


def fleet_worktrees(repo: Path) -> list[dict]:
    """Every worktree of this repo on a fleet/ branch."""
    return [wt for wt in list_worktrees(repo)
            if wt["branch"] and wt["branch"].startswith(BRANCH_PREFIX)]


def fleet_branches(repo: Path) -> list[str]:
    rc, out = probe(["git", "-C", str(repo), "for-each-ref", "--format=%(refname:short)",
                     f"refs/heads/{BRANCH_PREFIX}"])
    return sorted(out.splitlines()) if rc == 0 and out else []


# --------------------------------------------------------------------------
# tmux
# --------------------------------------------------------------------------

def tmux_bin() -> str:
    exe = cca.which("tmux")
    if not exe:
        die("`tmux` not found on PATH - ccfleet is a tmux tool")
    return exe


def target(session: str, window: str | None = None) -> str:
    """An exact tmux target. The `=` matters: without it tmux matches by
    prefix, so `-t fleet` could resolve to somebody else's `fleettest`."""
    return f"={session}" + (f":{window}" if window else "")


def has_session(session: str) -> bool:
    exe = cca.which("tmux")
    if not exe:
        return False
    rc, _ = probe([exe, "has-session", "-t", target(session)])
    return rc == 0


def session_panes(session: str) -> list[dict]:
    exe = cca.which("tmux")
    if not exe:
        return []
    fields = ("window_index", "window_name", "pane_current_command", "pane_pid",
              "pane_current_path", "pane_dead", "pane_dead_status")
    fmt = "\t".join("#{%s}" % f for f in fields)
    rc, out = probe([exe, "list-panes", "-s", "-t", target(session), "-F", fmt])
    if rc or not out:
        return []
    rows = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) > len(fields):
            continue
        # Pad, never skip. Trailing fields come back empty - `pane_dead_status`
        # on a live pane, `pane_current_path` on a dead one - and the last line
        # of the output has had its trailing tabs stripped by then, which is one
        # short row and, on a two-window fleet, half the fleet missing.
        parts += [""] * (len(fields) - len(parts))
        rows.append({"index": parts[0], "window": parts[1], "command": parts[2],
                     "pid": parts[3], "cwd": parts[4],
                     "dead": parts[5] == "1",
                     "exit_status": parts[6] or None})
    return rows


def panes_under(paths: list[Path]) -> dict[str, list[str]]:
    """For each path, the tmux windows whose cwd is inside it.

    Asked of tmux rather than tracked, for the same reason nothing else here
    is declared - and asked of *every* session, because the worktree `down` is
    about to remove may belong to a different fleet that is still working in
    it. A worktree pulled out from under a live agent is the one destruction
    this tool must not perform by accident.
    """
    exe = cca.which("tmux")
    users: dict[str, list[str]] = {str(p): [] for p in paths}
    if not exe or not paths:
        return users
    rc, out = probe([exe, "list-panes", "-a", "-F",
                     "#{session_name}:#{window_name}\t#{pane_current_path}"])
    if rc or not out:
        return users
    for line in out.splitlines():
        where, _, cwd = line.partition("\t")
        if not cwd:
            continue
        for path in paths:
            if cwd == str(path) or cwd.startswith(str(path) + os.sep):
                if where not in users[str(path)]:
                    users[str(path)].append(where)
    return users


# --------------------------------------------------------------------------
# fleet members
# --------------------------------------------------------------------------

def parse_spec(spec: str, role: str, taken: set[str]) -> dict:
    """`account` or `account:label` -> one fleet member.

    The label names the window, the branch and the worktree, so a repeated
    account gets a numeric suffix rather than a collision.
    """
    account, _, label = spec.partition(":")
    account = account.strip()
    label = (label or account).strip()
    if not account:
        die(f"empty {role} spec: {spec!r} (want: <account>[:<label>])")
    if not LABEL_RE.match(label):
        die(f"invalid label {label!r} in {role} spec {spec!r}: "
            f"use letters, digits, '-' and '_'")

    window = f"{role}-{label}"
    if window in taken:
        n = 2
        while f"{role}-{label}{n}" in taken:
            n += 1
        label, window = f"{label}{n}", f"{role}-{label}{n}"
    taken.add(window)
    return {"role": role, "account": account, "label": label, "window": window}


def launcher_for(acct) -> str:
    """The absolute `cca`/`cxa` path for this account's agent.

    Absolute on purpose: the pane is started by the tmux *server*, whose PATH
    is whatever it inherited whenever it was started - possibly without
    ~/.local/bin. Invoking cca.py directly is not an option either, since it
    reads the agent to drive from the name it was invoked as.
    """
    entry = cca.entry_for(acct.tool_name)
    link = cca.BIN_DIR / entry
    if link.exists():
        return str(link)
    found = cca.which(entry)
    if found:
        return found
    die(f"`{entry}` (needed to launch the {acct.tool_name} account "
        f"{acct.name!r}) is not installed in {tilde(cca.BIN_DIR)} or on PATH")


def member_argv(member: dict) -> list[str]:
    """How a member is launched: separate arguments, never a shell string.

    tmux execs a multi-argument command directly, which is what makes
    `pane_current_command` name the agent. Handing it one string instead puts a
    shell in front of every member, and then every window reports as `bash`
    however healthy it is - the pane's foreground process group belongs to that
    shell, not to its child. (It also means a prompt needs no quoting at all.)
    """
    prompt = member.get("prompt")
    return [member["launcher"], member["account"]] + ([prompt] if prompt else [])


def spawn(do: "Doer", exe: str, session: str, member: dict, cwd: Path,
          first: bool) -> None:
    """Create a member's window in three steps, in this order for a reason.

    The window is created empty, `remain-on-exit` is set while a shell is
    sitting in it, and only then is the agent spawned. Setting the option
    *after* the agent would be a race the interesting case loses: an account
    that dies on startup exits in milliseconds, tmux destroys the window, and
    the error goes with it. A dead pane keeps its last screen and its exit
    status instead.
    """
    window = member["window"]
    if first:
        do([exe, "new-session", "-d", "-s", session, "-n", window, "-c", str(cwd)])
    else:
        do([exe, "new-window", "-t", target(session), "-n", window, "-c", str(cwd)])
    do([exe, "set-option", "-w", "-t", target(session, window),
        "remain-on-exit", "on"])
    do([exe, "respawn-pane", "-k", "-t", target(session, window),
        "-c", str(cwd)] + member_argv(member))


def briefs_dir(fleet_dir: Path, session: str) -> Path:
    """Where briefs live: beside the worktrees, inside none of them.

    A brief written into a worker's own tree would dirty its branch, and
    `ccfleet down` would then refuse to remove that worktree. Namespaced by
    session because two fleets on one repository have two orchestrators, and
    one `briefs/orch.md` between them means the second overwrites the first.
    """
    return fleet_dir / "briefs" / session


def kickoff_brief(session: str, repo: Path, base_label: str, fleet_dir: Path,
                  members: list[dict], goal: str = "", gates: str = "",
                  use_board: bool = False) -> str:
    """The orchestrator's opening brief, written to disk rather than typed.

    It is a file for the same three reasons the orchestrator writes its own
    briefs to files: it can be re-read after a compaction, a human can read
    it, and nothing has to survive being quoted through a shell.
    """
    workers = [m for m in members if m["role"] == "worker"]
    checkers = [m for m in members if m["role"] == "checker"]
    lines = [
        f"# ccfleet kickoff - orchestrator of `{session}`",
        "",
        f"You are the orchestrator of a ccfleet: {len(workers)} worker and "
        f"{len(checkers)} checker agent sessions, each a separate CLI process "
        f"in a tmux window beside you. You cannot see their context and they "
        f"cannot see yours.",
        "",
        f"**Read your operating manual before anything else: {MANUAL}**",
        "",
        "It defines how to reach the other sessions (two channels, and when "
        "each applies), the brief format they need, the checker protocol, and "
        "how merging back works. Follow it.",
        "",
        f"## Fleet map - tmux session `{session}`",
        "",
        "| window | role | account | agent | branch | cwd |",
        "|---|---|---|---|---|---|",
    ]
    for m in members:
        where = m["worktree"] if m["role"] == "worker" else repo
        lines.append(f"| `{session}:{m['window']}` | {m['role']} "
                     f"| {m['account']} | {m.get('tool', '-')} "
                     f"| `{m.get('branch') or '-'}` | `{where}` |")
    lines += [
        "",
        "## Facts",
        "",
        f"- Repository (your cwd): `{repo}`",
        f"- Base commit every worker branched from: `{base_label}`",
        f"- Worker worktrees: `{fleet_dir}/<window name>`",
        f"- Write your briefs here: `{briefs_dir(fleet_dir, session)}/<window>.md`",
        "- Workers and checkers were launched **bare**, with no prompt: they "
        "know nothing about the fleet, their branch or the task until you tell "
        "them. Your first message to each must be self-contained.",
        f"- `ccfleet status --json` re-reads this map from tmux and git at any "
        f"time; it is current, this file is not.",
    ]
    if use_board:
        cmd = board_cmd(session)
        lines += ["", "## The goal", ""]
        lines += [f"> {goal}"] if goal else [
            "**Not given yet.** The human will tell you. Until then, add "
            "nothing to the board: every member is already waiting on it, and "
            "they wait quietly.",
        ]
        lines += [
            "## How this fleet works - read this twice",
            "",
            "Every member is already running its own loop against a shared task "
            "board, waiting for work. They are not waiting for you to type at "
            "them, and you must not: **you fill the board, they empty it.**",
            "",
            f"- Board: `{board_path(fleet_dir, session)}`",
            f"- Add a task: `{cmd} add --title \"...\" --brief \"...\" "
            f"--files \"a.py b.py\" [--dep t1] [--role worker|checker]`",
            f"- Watch it: `{cmd} list`",
            "",
            "### Your job, in order",
            "",
            "1. **Understand the goal well enough to split it.** Read what you "
            "must. This is the one part that is yours alone.",
            "2. **Decompose it into tasks, one `add` per independent unit of "
            "work.** Each task names the files it owns, and no two tasks own "
            "the same file. Work that must happen in order gets `--dep`; the "
            "board will not hand out a task until what it depends on is "
            "settled.",
            "3. **Write each `--brief` as if the member has never seen this "
            "repository** - it has not seen your context, only its own. State "
            "the change, the files, and how to tell it worked.",
            "4. **Then stop and watch.** Poll `list`. Members claim, work, "
            "report; a checker accepts or rejects; a rejected task goes back "
            "on the board with the reason and is picked up again. None of that "
            "needs you.",
            f"5. **When every task is on the board, `{cmd} close`.** Until "
            f"you do, a member that runs out of work waits instead of "
            f"stopping - which is deliberate, because decomposition is "
            f"several `add` calls and a fast member would otherwise finish "
            f"the board between two of them and go home. `close` is what "
            f"lets them stop. If more work appears later, `{cmd} reopen`.",
            "6. **Merge what is accepted**, one branch at a time, re-running "
            "the gates after each. Report to the human at the end.",
            "",
            "**You do not do the work yourself and you do not add a task you "
            "then do.** If the goal turns out not to split, say so and stop - "
            "do not quietly absorb it.",
        ]
        if gates:
            lines += ["", f"The gates every member must pass: `{gates}`"]
        lines += [
            "",
            ("Start now: decompose the goal and fill the board."
             if goal else
             "Report the map back to me and wait for the goal."),
            "",
        ]
    else:
        lines += [
            "",
            "## Now",
            "",
            "Do not start any work yet. Confirm the map back to me - including "
            "which sessions you can reach on which channel - and wait for the "
            "job. When it comes, you decompose and delegate it; you do not do "
            "it yourself.",
            "",
        ]
    return "\n".join(lines)


def board_path(fleet_dir: Path, session: str) -> Path:
    """One board per fleet, named after it - two fleets on one repo are two
    independent pieces of work and must not claim from each other."""
    return fleet_dir / "boards" / f"{session}.json"


def board_cmd(session: str) -> str:
    """The literal command a member types. Spelled out with its session, so a
    brief can be followed without the member knowing which fleet it is in."""
    return "ccfleet board" + ("" if session == DEFAULT_SESSION else f" -s {session}")


def kickoff_prompt(brief: Path, role: str = "orch") -> str:
    """One line, because it is the argv a window is launched with.

    Role-aware: the same sentence for every member would tell three of them
    they are the orchestrator, and an agent told that acts like one.
    """
    if role == "orch":
        return (f"You are the orchestrator of a ccfleet. Read {brief} - it is "
                f"your kickoff brief, and it names the operating manual you "
                f"work from. Follow it.")
    return (f"You are a {role} in a ccfleet. Read {brief} - it is your brief, "
            f"and it contains the loop you are to run. Start that loop now, "
            f"and stay in it until the board tells you it is drained.")


# --------------------------------------------------------------------------
# briefs a member works from
# --------------------------------------------------------------------------

def render_task(task: dict, member: str, cmd: str) -> str:
    """A claimed task, written for whoever has to act on it.

    Everything the member needs is here and nowhere else: the previous
    rejection it has to answer, the files it owns, and the literal commands
    that end the task. A member that has to go and ask what to do next is a
    member the board has failed.
    """
    lines = [f"TASK {task['id']}   {task['title']}"]
    if task["attempts"] > 1:
        lines.append(f"(attempt {task['attempts']} - read the notes below)")
    if task.get("files"):
        lines.append(f"files you own: {task['files']}")
    if task.get("reviews"):
        lines.append(f"reviewing: {task['reviews']}")
    if task.get("brief"):
        lines += ["", task["brief"]]
    if task.get("notes"):
        lines += ["", "notes so far:"] + [f"  - {n}" for n in task["notes"]]
    lines.append("")
    if task["role"] == "checker":
        target = task.get("reviews") or "<task>"
        lines += [
            "When you have reviewed it, one of:",
            f"  {cmd} accept {target} --note \"<why it passes>\"",
            f"  {cmd} reject {target} --why \"<what must change>\"",
        ]
    else:
        lines += [
            "When it is finished and committed:",
            f"  {cmd} done {task['id']} --as {member} --note \"<what you did>\"",
            "If you cannot finish it:",
            f"  {cmd} block {task['id']} --why \"<why>\"",
        ]
    lines += ["", f"Then go straight back to: {cmd} next --as {member} --wait 240"]
    return "\n".join(lines)


def loop_brief(member: dict, session: str, repo: Path, base_label: str,
               gates: str = "") -> str:
    """The one thing a worker or checker is told, once, at launch.

    It is a loop rather than a task because the board decides what the work is
    and when there is more of it. A member briefed this way needs nothing typed
    at it ever again - which is what makes a fleet of foreign CLIs coordinate
    at all.
    """
    cmd = board_cmd(session)
    name = member["window"]
    checker = member["role"] == "checker"
    head = [
        f"# Brief: {name}",
        "",
        f"You are a **{member['role']}** in a ccfleet, working from a shared "
        f"task board. You are one of several agent sessions; you never talk to "
        f"the others, and the board is the only thing you coordinate through.",
        "",
    ]
    if checker:
        head += [
            f"- You review; you do not edit. Not in `{repo}`, not in any "
            f"worktree, not \"just this one line\".",
            f"- Repository: `{repo}`",
            f"- Review a branch with: `git -C {repo} diff {base_label.split()[0]}"
            f"...<branch>`, and run its tests inside that worker's worktree.",
        ]
    else:
        head += [
            f"- Worktree - your cwd, and the **only** tree you may edit: "
            f"`{member['worktree']}`",
            f"- Branch: `{member.get('branch')}`  (base: {base_label})",
            "- Never edit the main checkout or another worker's worktree. "
            "Commit your work on your own branch.",
        ]
    body = [
        "",
        "## Your loop - start it now, and stay in it",
        "",
        f"1. `{cmd} next --as {name} --wait 240`",
        "   - exit 0: the task is printed. Do exactly that, nothing more.",
        "   - exit 4: nothing claimable yet. Run the same command again.",
        "   - exit 5: the board is drained. Report what you did and stop.",
        "2. Do the task.",
    ]
    if gates:
        body.append(f"3. Run the gates before reporting it done: {gates}")
    report = (f"`{cmd} accept <id>` / `{cmd} reject <id> --why \"...\"`"
              if checker else
              f"`{cmd} done <id> --as {name} --note \"...\"`, or "
              f"`{cmd} block <id> --why \"...\"`")
    body += [
        f"{'4' if gates else '3'}. Report it: {report}. Each task prints its "
        f"own line with the id already filled in - use that.",
        f"{'5' if gates else '4'}. Go back to step 1.",
        "",
        "Rules that make this work:",
        "",
        "- **Only ever work on a task `next` gave you.** Do not read the board "
        "and pick something you like the look of; claiming is what stops two "
        "members doing the same task.",
        "- **Do not stop at exit 4.** It means the work you are waiting on is "
        "still in someone else's hands. Only exit 5 ends your loop.",
        "- A rejected task comes back to you through `next` like any other, "
        "carrying the reason. Answer the reason.",
    ]
    return "\n".join(head + body) + "\n"


# --------------------------------------------------------------------------
# up
# --------------------------------------------------------------------------

def plan_worktree(repo: Path, existing: list[dict], branch: str,
                  path: Path) -> tuple[str, str]:
    """How to get `branch` checked out at `path`: create / attach / reuse.

    Reuse matters: `ccfleet down` keeps a worktree that still holds work, so
    the next `up` for the same label has to pick it back up rather than fail.
    """
    at_path = next((w for w in existing if w["path"] == path), None)
    elsewhere = next((w for w in existing
                      if w["branch"] == branch and w["path"] != path), None)
    if elsewhere:
        die(f"branch {branch} is already checked out at "
            f"{tilde(elsewhere['path'])}; use a different label or remove it")
    if at_path:
        if at_path["branch"] != branch:
            die(f"{tilde(path)} is already a worktree on branch "
                f"{at_path['branch'] or '(detached)'}, not {branch}")
        return "reuse", "existing worktree kept as it is"
    if path.exists():
        die(f"{tilde(path)} exists and is not a worktree of {tilde(repo)}")
    if branch_exists(repo, branch):
        return "attach", f"branch {branch} already existed; worktree re-created on it"
    return "create", ""


def cmd_up(args) -> int:
    session = args.session
    if not SESSION_RE.match(session):
        die(f"invalid session name {session!r}: use letters, digits, '-' and '_'")
    tmux_bin()
    if has_session(session):
        die(f"tmux session {session!r} already exists - `ccfleet status -s "
            f"{session}` to see it, `ccfleet down -s {session}` to end it, "
            f"or pick another name with -s")

    reg = cca.Registry.load()
    taken: set[str] = set()
    members = [parse_spec(s, "worker", taken) for s in (args.worker or [])]
    members += [parse_spec(s, "checker", taken) for s in (args.checker or [])]
    if not members:
        die("a fleet needs at least one member: -w <account> and/or -c <account>")

    if args.no_orch and args.orch:
        die("--no-orch and -o contradict each other: either there is an "
            "orchestrator window or you are the orchestrator")
    orch = None
    if not args.no_orch:
        name = args.orch or cca.current_account_name(reg) \
            or reg.default_for(cca.DEFAULT_TOOL)
        if not name:
            die("no orchestrator account: pass -o <account>, or --no-orch to "
                "run the fleet from this session")
        orch = {"role": "orch", "account": name, "label": name, "window": "orch"}

    # Resolve every account before touching git or tmux, so an unknown name
    # cannot leave half a fleet behind.
    for m in ([orch] if orch else []) + members:
        acct = reg.get(m["account"])          # dies with the known names
        m["tool"] = acct.tool_name
        m["launcher"] = launcher_for(acct)
        if not cca.which(acct.tool.binary):
            warn(f"{m['window']}: `{acct.tool.binary}` is not on PATH; that "
                 f"window will open on the error")

    repo = main_worktree(git_root(Path(args.repo).expanduser() if args.repo
                                  else Path.cwd()))
    base_sha, base_label = resolve_rev(repo, args.base or "HEAD")
    fleet_dir = fleet_dir_for(repo)
    existing = list_worktrees(repo)

    for m in members:
        if m["role"] != "worker":
            m["worktree"] = repo
            continue
        m["branch"] = f"{BRANCH_PREFIX}{m['window']}"
        m["worktree"] = fleet_dir / m["window"]
        m["action"], m["note"] = plan_worktree(repo, existing, m["branch"],
                                               m["worktree"])

    all_members = ([orch] if orch else []) + members
    print_map(session, repo, base_label, fleet_dir, all_members,
              dry_run=args.dry_run)

    goal = (getattr(args, "goal", "") or "").strip()
    gates = (getattr(args, "gates", "") or "").strip()
    # The board is how a fleet works on its own, so it is the default. The
    # goal is just text on it: a fleet is perfectly startable before anyone
    # has decided what it is for, and the orchestrator can be told later.
    use_board = not getattr(args, "no_board", False)
    do = Doer(args.dry_run)
    briefs = briefs_dir(fleet_dir, session)
    if use_board:
        # A checker in the fleet is what turns "the worker says it is done"
        # into "a second session agreed" - so it is the board's review switch.
        path = board_path(fleet_dir, session)
        blank = board.Board(path).blank(
            goal, auto_review=any(m["role"] == "checker" for m in members))
        do.write(path, json.dumps(blank, indent=2, sort_keys=True) + "\n",
                 "task board")
    if orch:
        orch["brief"] = briefs / "orch.md"
        do.write(orch["brief"],
                 kickoff_brief(session, repo, base_label, fleet_dir, members,
                               goal, gates, use_board),
                 "kickoff brief")
    if use_board:
        for m in members:
            m["brief"] = briefs / f"{m['window']}.md"
            do.write(m["brief"],
                     loop_brief(m, session, repo, base_label, gates),
                     f"{m['window']} loop brief")
    for m in members:
        if m["role"] != "worker":
            continue
        if m["action"] == "create":
            do(["git", "-C", str(repo), "worktree", "add", "-b", m["branch"],
                str(m["worktree"]), base_sha])
        elif m["action"] == "attach":
            do(["git", "-C", str(repo), "worktree", "add", str(m["worktree"]),
                m["branch"]])

    exe = tmux_bin()
    if orch:
        orch["prompt"] = kickoff_prompt(orch["brief"])
    # Without a goal, members start bare and wait to be briefed by hand. With
    # one, every member starts inside its loop - which is the whole difference.
    for m in members:
        if m.get("brief"):
            m["prompt"] = kickoff_prompt(m["brief"], m["role"])
    for n, m in enumerate(all_members):
        cwd = m["worktree"] if m["role"] == "worker" else repo
        spawn(do, exe, session, m, cwd, first=(n == 0))

    if args.dry_run:
        print(f"\n{c.dim}(dry run - nothing above was executed){c.reset}")
        return 0

    if is_dirty(repo):
        sys.stdout.flush()      # so the warning lands after the map in a pipe
        warn(f"{tilde(repo)} has uncommitted changes; they are NOT in the "
             f"worker branches, which start from {base_label}")
    info(f"fleet {session!r} is up")
    print(f"\n  attach:   tmux attach -t {session}")
    print(f"  a window: tmux attach -t {session}:{all_members[-1]['window']}")
    if use_board:
        print(f"\n  board:    {board_cmd(session)} list")
    if orch:
        print(f"\n{c.dim}The orch window was pointed at "
              f"{tilde(orch['brief'])}, which names {tilde(MANUAL)}.{c.reset}")
    else:
        print(f"\n{c.dim}No orch window: brief the fleet from this session. "
              f"The manual is {tilde(MANUAL)}.{c.reset}")
    return 0


def print_map(session: str, repo: Path, base_label: str, fleet_dir: Path,
              members: list[dict], dry_run: bool = False) -> None:
    head = "fleet (planned)" if dry_run else "fleet"
    print(f"{c.bold}{head} {session}{c.reset}   repo {tilde(repo)}   "
          f"base {base_label}")
    cols = [("window", "WINDOW"), ("role", "ROLE"), ("account", "ACCOUNT"),
            ("tool", "AGENT"), ("branch", "BRANCH"), ("cwd", "CWD"),
            ("note", "")]
    rows = []
    for m in members:
        rows.append({
            "window": f"{session}:{m['window']}",
            "role": m["role"],
            "account": m["account"],
            "tool": m.get("tool", "-"),
            "branch": m.get("branch") or "-",
            "cwd": tilde(m.get("worktree") or repo),
            "note": m.get("note") or "",
        })
    widths = {k: max(len(h), *(len(str(r[k])) for r in rows)) for k, h in cols}
    print(f"{c.bold}{'  '.join(h.ljust(widths[k]) for k, h in cols).rstrip()}{c.reset}")
    for r in rows:
        line = "  ".join(str(r[k]).ljust(widths[k]) for k, _ in cols).rstrip()
        print(line)
    print()


# --------------------------------------------------------------------------
# status
# --------------------------------------------------------------------------

def branch_states(repo: Path, base: str,
                  ignore_session: str | None = None) -> list[dict]:
    """Every fleet branch and worktree, with what removing it would cost.

    `ignore_session` drops one session's own windows from the occupancy count.
    `down` passes the session it is tearing down: in a real run those panes are
    already dead by the time this is read, and a --dry-run that did not do the
    same would report every one of its own worktrees as in use and predict the
    opposite of what the real command does.
    """
    by_branch = {w["branch"]: w for w in fleet_worktrees(repo)}
    users = panes_under([w["path"] for w in by_branch.values()])
    if ignore_session:
        mine = ignore_session + ":"
        users = {path: [w for w in where if not w.startswith(mine)]
                 for path, where in users.items()}
    rows = []
    for branch in sorted(set(fleet_branches(repo)) | set(by_branch)):
        wt = by_branch.get(branch)
        merged = is_ancestor(repo, branch, base) if branch_exists(repo, branch) else None
        rows.append({
            "branch": branch,
            "worktree": str(wt["path"]) if wt else None,
            "dirty": is_dirty(wt["path"]) if wt else None,
            "ahead": commits_ahead(repo, base, branch),
            "merged": merged,
            "in_use": users.get(str(wt["path"]), []) if wt else [],
        })
    return rows


def cmd_status(args) -> int:
    repo = main_worktree(git_root(Path(args.repo).expanduser() if args.repo
                                 else Path.cwd()))
    base_sha, base_label = resolve_rev(repo, args.base or "HEAD")
    panes = session_panes(args.session)
    branches = branch_states(repo, base_sha)

    if args.json:
        print(json.dumps({
            "session": args.session,
            "session_exists": has_session(args.session),
            "repo": str(repo),
            "base": {"sha": base_sha, "label": base_label},
            "fleet_dir": str(fleet_dir_for(repo)),
            "panes": panes,
            "branches": branches,
        }, indent=2))
        return 0

    print(f"{c.bold}fleet {args.session}{c.reset}   repo {tilde(repo)}   "
          f"base {base_label}")
    if not panes:
        state = "no windows" if has_session(args.session) \
            else f"not running (no tmux session {args.session!r})"
        print(f"  {c.dim}{state}{c.reset}")
    else:
        cols = [("index", "#"), ("window", "WINDOW"), ("command", "RUNNING"),
                ("pid", "PID"), ("cwd", "CWD")]
        rows = [dict(r, cwd=tilde(r["cwd"]),
                     command=(f"dead (exit {r['exit_status']})" if r["dead"]
                              else r["command"]))
                for r in panes]
        widths = {k: max(len(h), *(len(str(r[k])) for r in rows)) for k, h in cols}
        print(f"{c.bold}{'  '.join(h.ljust(widths[k]) for k, h in cols)}{c.reset}")
        for r in rows:
            line = "  ".join(str(r[k]).ljust(widths[k]) for k, _ in cols).rstrip()
            print(f"{c.yellow}{line}{c.reset}" if r["dead"] else line)
        if any(r["dead"] for r in panes):
            print(f"{c.dim}A dead pane is an agent that exited; its last screen "
                  f"is still there. Restart it in place with: tmux respawn-pane "
                  f"-k -t '={args.session}:<window>' <command>{c.reset}")

    print()
    if not branches:
        print(f"  {c.dim}no {BRANCH_PREFIX} branches in {tilde(repo)}{c.reset}")
        return 0
    cols = [("branch", "BRANCH"), ("state", "STATE"), ("ahead", "AHEAD"),
            ("merged", "MERGED"), ("IN USE BY", "IN USE BY"),
            ("worktree", "WORKTREE")]
    rows = []
    for b in branches:
        rows.append({
            "branch": b["branch"],
            "state": "-" if b["worktree"] is None
                     else ("dirty" if b["dirty"] else "clean"),
            "ahead": "?" if b["ahead"] < 0 else str(b["ahead"]),
            "merged": "-" if b["merged"] is None else ("yes" if b["merged"] else "no"),
            "worktree": tilde(b["worktree"]) if b["worktree"] else "(none)",
            "IN USE BY": ",".join(b["in_use"]) or "-",
        })
    widths = {k: max(len(h), *(len(str(r[k])) for r in rows)) for k, h in cols}
    print(f"{c.bold}{'  '.join(h.ljust(widths[k]) for k, h in cols)}{c.reset}")
    for r in rows:
        color = c.yellow if (r["state"] == "dirty" or r["merged"] == "no") else ""
        line = "  ".join(str(r[k]).ljust(widths[k]) for k, _ in cols).rstrip()
        print(f"{color}{line}{c.reset}")
    print(f"\n{c.dim}AHEAD = commits on the branch that {base_label} does not "
          f"have. `ccfleet down` removes only clean+merged worktrees.{c.reset}")
    return 0


# --------------------------------------------------------------------------
# down
# --------------------------------------------------------------------------

def removal_decision(row: dict, force: bool) -> tuple[bool, str]:
    """Whether this worktree may go, and why - the whole safety model.

    Clean and merged means removing it destroys nothing. --force will still
    drop the worktree (and with it uncommitted edits, said out loud), but an
    unmerged *branch* is never deleted: commits survive a forced teardown.
    """
    if row["worktree"] is None:
        return False, "no worktree"
    if row["in_use"]:
        where = ", ".join(row["in_use"])
        return (force, f"forced: taken from {where}" if force
                else f"in use by {where}")
    if row["dirty"] and row["merged"]:
        return (force, "forced: uncommitted changes discarded" if force
                else "uncommitted changes")
    if row["dirty"] and not row["merged"]:
        return (force, "forced: uncommitted changes discarded, branch kept"
                if force else "uncommitted changes, and not merged")
    if not row["merged"]:
        return (force, f"forced: worktree removed, branch kept ({row['ahead']} "
                f"commits)" if force else f"not merged ({row['ahead']} commits)")
    return True, "clean and merged"


def cmd_down(args) -> int:
    repo = main_worktree(git_root(Path(args.repo).expanduser() if args.repo
                                 else Path.cwd()))
    base_sha, base_label = resolve_rev(repo, args.base or "HEAD")
    do = Doer(args.dry_run)

    exe = cca.which("tmux")
    if exe and has_session(args.session):
        do([exe, "kill-session", "-t", target(args.session)])
        if not args.dry_run:
            info(f"tmux session {args.session!r} killed")
    else:
        print(f"{c.dim}no tmux session {args.session!r} to kill{c.reset}")

    # This fleet's own windows never count as occupancy: the kill above already
    # ended them, and under --dry-run it would have.
    rows = branch_states(repo, base_sha, ignore_session=args.session)
    removed, kept = [], []
    for row in rows:
        ok, why = removal_decision(row, args.force)
        if row["worktree"] is None:
            continue
        if not ok:
            kept.append((row, why))
            continue
        argv = ["git", "-C", str(repo), "worktree", "remove", row["worktree"]]
        if args.force:
            argv.append("--force")
        do(argv, check=False)
        # Only a merged branch is deleted, and `-d` is the safety net: git
        # refuses anything it would lose. A forced teardown keeps the commits.
        if row["merged"]:
            do(["git", "-C", str(repo), "branch", "-d", row["branch"]], check=False)
        removed.append((row, why))

    if rows:
        do(["git", "-C", str(repo), "worktree", "prune"], check=False)
    fleet_dir = fleet_dir_for(repo)
    leftovers: list[str] = []
    if not args.dry_run and fleet_dir.is_dir():
        try:
            fleet_dir.rmdir()          # only when nothing is left in it
        except OSError:
            keeping = {Path(row["worktree"]).name for row, _ in kept}
            leftovers = sorted(p.name for p in fleet_dir.iterdir()
                               if p.name not in keeping)

    print()
    if removed:
        print(f"{c.bold}removed{c.reset}")
        for row, why in removed:
            print(f"  {row['branch']:<28} {tilde(row['worktree'])}   "
                  f"{c.dim}{why}{c.reset}")
    if kept:
        print(f"{c.bold}kept{c.reset}")
        for row, why in kept:
            print(f"  {c.yellow}{row['branch']:<28}{c.reset} "
                  f"{tilde(row['worktree'])}   {why}")
        print(f"\n{c.dim}Kept worktrees hold work that is not in "
              f"{base_label}, or a live window is sitting in them. Merge, "
              f"push or end those, then re-run; `ccfleet down --force` drops "
              f"the worktrees anyway (unmerged branches are still kept)."
              f"{c.reset}")
    if not removed and not kept:
        print(f"{c.dim}no fleet worktrees in {tilde(repo)}{c.reset}")
    if leftovers:
        print(f"\n{c.dim}{tilde(fleet_dir)} also holds "
              f"{', '.join(leftovers)} - no worktree, safe to delete.{c.reset}")
    if args.dry_run:
        print(f"\n{c.dim}(dry run - nothing above was executed){c.reset}")
    return 0


# --------------------------------------------------------------------------
# board
# --------------------------------------------------------------------------

def open_board(args) -> "board.Board":
    repo = main_worktree(git_root(Path(args.repo).expanduser() if args.repo
                                  else Path.cwd()))
    return board.Board(board_path(fleet_dir_for(repo), args.session))


def cmd_board(args) -> int:
    bd = open_board(args)
    cmd = board_cmd(args.session)
    verb = args.board_cmd

    if verb == "init":
        bd.init(args.goal or "", bool(args.review))
        info(f"board created: {tilde(bd.path)}")
        return 0

    if not bd.exists() and verb != "list":
        die(f"no board at {tilde(bd.path)} - `ccfleet up --goal \"...\"` "
            f"creates one, or `{cmd} init --goal \"...\"`")

    if verb == "add":
        task_id = bd.add(args.title, args.brief or "", args.role,
                         args.dep or [], args.files or "")
        print(task_id)
        return 0

    if verb == "next":
        code, task = bd.wait_claim(args.member, float(args.wait or 0))
        if code == board.GOT_TASK:
            print(render_task(task, args.member, cmd))
            return 0
        if code == board.DRAINED:
            print("The board is drained: nothing left to claim and nobody "
                  "still working. Your loop is over - report what you did.")
            return board.DRAINED
        data = bd.summary()
        why = ("the board has no tasks on it yet - the orchestrator is still "
               "deciding how to split the goal" if not data["tasks"] else
               "the work it depends on is still with someone else")
        print(f"No task for {args.member} yet: {why}. This is not the end of "
              f"your loop - run the same command again.")
        return board.NO_TASK_YET

    if verb == "close":
        state = bd.close()
        info("board closed: no more tasks. Members will stop once the work on "
             "it is finished." if not state["drained"] else
             "board closed and already drained - every member will now stop.")
        return 0

    if verb == "reopen":
        bd.reopen()
        info("board reopened - members will wait for new tasks instead of "
             "stopping")
        return 0

    try:
        if verb == "done":
            task = bd.done(args.id, args.member or "", args.note or "")
            state = task["state"]
            print(f"{task['id']} -> {state}"
                  + ("  (a review task is now on the board)"
                     if state == "review" else ""))
        elif verb == "accept":
            print(f"{bd.accept(args.id, args.note or '')['id']} -> accepted")
        elif verb == "reject":
            print(f"{bd.reject(args.id, args.why)['id']} -> pending again "
                  f"(the reason travels with it)")
        elif verb == "block":
            print(f"{bd.block(args.id, args.why)['id']} -> blocked")
    except KeyError:
        die(f"no task {args.id!r} on {tilde(bd.path)}")

    if verb == "list":
        return print_board(bd, args)
    return 0


def print_board(bd: "board.Board", args) -> int:
    data = bd.summary()
    if getattr(args, "json", False):
        print(json.dumps(data, indent=2))
        return 0
    if not bd.exists():
        print(f"{c.dim}no board for session {args.session!r}{c.reset}")
        return 0
    if data["goal"]:
        print(f"{c.bold}goal{c.reset}  {data['goal']}")
    if not data["tasks"]:
        print(f"{c.dim}no tasks yet{c.reset}")
        return 0
    cols = [("id", "ID"), ("state", "STATE"), ("role", "ROLE"),
            ("owner", "OWNER"), ("deps", "DEPS"), ("title", "TITLE")]
    rows = [{"id": t["id"], "state": t["state"], "role": t["role"],
             "owner": t["owner"] or "-", "deps": ",".join(t["deps"]) or "-",
             "title": t["title"]} for t in data["tasks"]]
    widths = {k: max(len(h), *(len(str(r[k])) for r in rows)) for k, h in cols}
    print(f"{c.bold}{'  '.join(h.ljust(widths[k]) for k, h in cols)}{c.reset}")
    tone = {"blocked": c.red, "review": c.yellow, "accepted": c.green,
            "done": c.green}
    for r in rows:
        line = "  ".join(str(r[k]).ljust(widths[k]) for k, _ in cols).rstrip()
        print(f"{tone.get(r['state'], '')}{line}{c.reset}")
    counts = "  ".join(f"{n} {state}" for state, n in data["counts"].items() if n)
    print(f"\n{counts}"
          + (f"   {c.green}drained{c.reset}" if data["drained"]
             else f"   {c.dim}open{c.reset}" if not data["closed"] else ""))
    if not data["closed"]:
        print(f"{c.dim}The board is open: members wait rather than stop. "
              f"`{board_cmd(args.session)} close` when every task is on it."
              f"{c.reset}")
    return 0


# --------------------------------------------------------------------------
# plumbing
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ccfleet",
        description="Run a tmux fleet of coding-agent sessions: one "
                    "orchestrator, N workers on their own git worktrees, "
                    "N checkers reviewing them.",
        epilog=(
            "ccfleet up -w work -w codex -c client\n"
            "    orchestrator on the current account, two workers (one claude,\n"
            "    one codex) each on their own worktree and fleet/ branch, and a\n"
            "    checker in the main checkout.\n"
            "\n"
            "ccfleet up -w work:parse -w work:report\n"
            "    same account twice; the label after ':' names the window,\n"
            "    branch and worktree.\n"
            "\n"
            "ccfleet status            what each window and each fleet branch is doing\n"
            "ccfleet down              kill the session, keep anything unmerged\n"
            "ccfleet up --dry-run ...  print the git and tmux commands only\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest="cmd")

    def common(s):
        s.add_argument("-s", "--session", default=DEFAULT_SESSION,
                       help=f"tmux session name (default {DEFAULT_SESSION})")
        s.add_argument("--repo", help="repository to work on (default: the git "
                                      "root of the current directory)")
        s.add_argument("--base", help="base ref (default HEAD)")

    s = sub.add_parser("up", help="start a fleet")
    common(s)
    s.add_argument("-w", "--worker", action="append", metavar="ACCOUNT[:LABEL]",
                   help="a worker session on its own worktree; repeatable")
    s.add_argument("-c", "--checker", action="append", metavar="ACCOUNT[:LABEL]",
                   help="a checker session in the main checkout; repeatable")
    s.add_argument("-o", "--orch", metavar="ACCOUNT",
                   help="orchestrator account (default: the account this shell "
                        "is on, else that agent's default)")
    s.add_argument("--no-orch", action="store_true",
                   help="no orchestrator window - drive the fleet from here")
    s.add_argument("-g", "--goal", metavar="TEXT",
                   help="what the fleet is for. Optional - it is only recorded "
                        "on the board, and you can just as well tell the "
                        "orchestrator once it is up")
    s.add_argument("--no-board", action="store_true",
                   help="no task board: members start bare and wait to be "
                        "briefed by hand, one message at a time")
    s.add_argument("--gates", metavar="CMD",
                   help="the literal commands every member must pass before "
                        "reporting a task done")
    s.add_argument("--dry-run", action="store_true",
                   help="print every git and tmux command instead of running it")
    s.set_defaults(func=cmd_up)

    s = sub.add_parser("status", help="windows, branches, and what is unmerged")
    common(s)
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("board", help="the shared task list members work from")
    common(s)
    bsub = s.add_subparsers(dest="board_cmd", required=True)

    b = bsub.add_parser("init", help="create an empty board")
    b.add_argument("--goal", help="what this fleet is for")
    b.add_argument("--review", action="store_true",
                   help="a finished task waits for a checker to accept it")
    b = bsub.add_parser("add", help="put a task on the board")
    b.add_argument("--title", required=True)
    b.add_argument("--brief", help="everything the member needs to do it")
    b.add_argument("--files", help="the files this task owns; no two tasks share one")
    b.add_argument("--role", default="worker", choices=sorted(board.ROLES))
    b.add_argument("--dep", action="append", metavar="ID",
                   help="task that must settle first; repeatable")
    b = bsub.add_parser("next", help="claim the next task for a member (blocks)")
    b.add_argument("--as", dest="member", required=True, metavar="MEMBER")
    b.add_argument("--wait", type=float, default=240,
                   help="seconds to wait for work before exiting 4 (default 240)")
    b = bsub.add_parser("done", help="a member reports a task finished")
    b.add_argument("id")
    b.add_argument("--as", dest="member", metavar="MEMBER")
    b.add_argument("--note")
    b = bsub.add_parser("accept", help="a checker passes a task")
    b.add_argument("id")
    b.add_argument("--note")
    b = bsub.add_parser("reject", help="a checker sends a task back, with why")
    b.add_argument("id")
    b.add_argument("--why", required=True)
    b = bsub.add_parser("block", help="a task cannot be finished")
    b.add_argument("id")
    b.add_argument("--why", required=True)
    b = bsub.add_parser("close", help="decomposition is finished; no more tasks")
    b = bsub.add_parser("reopen", help="undo close: more tasks are coming")
    b = bsub.add_parser("list", help="every task and its state")
    b.add_argument("--json", action="store_true")
    for b in bsub.choices.values():
        b.set_defaults(func=cmd_board)
    s.set_defaults(func=cmd_board)

    s = sub.add_parser("down", help="kill the session; remove only safe worktrees")
    common(s)
    s.add_argument("--force", action="store_true",
                   help="remove worktrees even when dirty or unmerged "
                        "(unmerged branches are still kept)")
    s.add_argument("--dry-run", action="store_true",
                   help="print what would be removed and kept")
    s.set_defaults(func=cmd_down)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    if not getattr(args, "cmd", None):
        parser.print_help()
        return 0
    return args.func(args) or 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
