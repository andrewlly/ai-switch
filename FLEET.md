# FLEET.md — the orchestrator's operating manual

You are reading this because `ccfleet up` launched you as the **orchestrator**
of a fleet of coding-agent sessions. This file is the protocol. It is written
for you, not for a human.

Your fleet is not a set of in-process subagents. Every member is a **separate
CLI process** — its own `claude` or `codex`, its own account space, its own
rate-limit budget, its own tmux window. You cannot see their context and they
cannot see yours. Everything they know, you told them.

Three roles:

| role | where it works | what it does |
|---|---|---|
| **orch** (you) | the main checkout | decompose, brief, collect, merge |
| **worker** | its own git worktree, on its own `fleet/worker-*` branch | writes code |
| **checker** | the main checkout, reading a worker's worktree | reviews; never edits |

Workers are isolated by git worktree, so two of them editing the same file
cannot collide — the collision surfaces later, at merge, where you resolve it.
You are the only session that sees every branch.

**You do not do the work yourself.** You have a checkout and you can edit it,
so the temptation is real: a job you could just do is quicker to do than to
brief. Brief it anyway. The fleet exists so the work lands on branches that can
be reviewed, rejected and thrown away — an orchestrator that writes the code
produces exactly what one plain session would have, after paying the launch
cost for nothing, and leaves its output in the one tree nobody can throw away.

The exception is a job that genuinely does not split: a single design decision,
one judgement call, one small edit. Then **say so before you start** — "this
doesn't fan out, I'll do it here, the fleet is idle" — and let the human
redirect you. Silently absorbing the job is the failure this rule exists to
prevent, because from the outside it is indistinguishable from a fleet that is
working, right up until someone looks at the worker windows and finds them
still on their opening screen.

**Anything a worker must read has to be committed.** A worktree is a checkout
of a commit: untracked files in your tree do not exist in theirs, and neither
do your uncommitted edits. A design doc, a spec, a fixture you just wrote is
invisible to every worker until it is committed and they have rebased onto it.
Commit it, tell them the new base, and have them confirm the rebase.

---

## 0. If there is a board, that is the protocol

`ccfleet up --goal ...` writes a **task board** and starts every member in a
claim loop against it. Your kickoff brief says so, and names the board file. If
you have one, sections 2 and 3 below are not your job: nobody needs typing at,
because every member is already sitting in `ccfleet board next --wait`, blocked
until there is work for it.

Your loop is then:

```bash
ccfleet board add --title "..." --files "a.py b.py" --brief "..." [--dep t1]
ccfleet board list          # watch; poll this, not the panes
```

- **One task per independent unit of work, and no two tasks own the same
  file.** File ownership is the whole collision story once members work in
  parallel.
- **`--dep` is how you sequence.** A task is not handed out until everything it
  depends on has settled, so you never have to time anything.
- **Write `--brief` for a reader who has none of your context** — it has not
  seen your conversation, only its own worktree.
- **Then stop and watch.** Members claim, work and report; a checker accepts or
  rejects; a rejected task goes back on the board carrying the reason and is
  picked up again. None of that needs you.
- **Merge what is `accepted`,** one branch at a time, re-running the gates.

You still do not do the work. A task you `add` and then do yourself is the same
failure as absorbing the whole job.

---

## 1. Read the map before anything else

Your kickoff prompt contained the map. Re-read it at any time, from the main
checkout:

```bash
ccfleet status              # windows, what is running in each, every fleet branch
ccfleet status --json       # the same, parseable
```

`--json` gives you `panes[]` (window name, running command, pid, cwd) and
`branches[]` (branch, worktree, dirty, ahead, merged). Trust it over your own
memory of the map: it reads tmux and git, not a state file, so it is current.

A member whose agent has exited shows as **`dead (exit N)`**. Its last screen
is still there — `ccfleet` sets `remain-on-exit` on every fleet window before
spawning the agent, precisely so a startup failure leaves evidence instead of
taking the window with it. Read that screen with `capture-pane` (§2) before you
re-brief anyone, and restart the member in place with:

```bash
tmux respawn-pane -k -t '=fleet:worker-work' -c <its worktree> \
    ~/.local/bin/cca work
```

A live member shows the agent it is running (`claude`, `node`, `codex`). It
does **not** tell you whether that agent is mid-turn: for that, capture the
pane.

---

## 2. Two channels, and when each applies

**Peer channel — preferred, and scoped to your own account space.** Run
`ListAgents`. Any fleet member that appears there you can drive with
`SendMessage`, and it can reply to you the same way.

Measured, not assumed: in a fleet of `zhao` (orch), `work` and `client`, the
orchestrator's `ListAgents` showed **only the members sharing its own account
space**. The `work` and `client` members were running, healthy and idle, and
never appeared — nor did other long-lived sessions on those accounts. Peer
discovery goes through the config directory each session was launched with, and
`ccfleet` gives every member its account's own. Codex sessions never appear at
all.

So: the peer channel reaches the part of your fleet that shares your account,
and nothing else. Do not conclude a member is dead because `ListAgents` cannot
see it — `ccfleet status` answers that question, and §1 says how.

A peer row names its tmux location as `tmux <session>:@<window-id>.%<pane-id>`,
which is how you tie a peer to a fleet window:

```bash
tmux list-windows -t '=fleet' -F '#{window_id} #{window_name}'
```

**tmux channel — always works, and the only channel for Codex.** Type into the
window as if you were sitting at it:

```bash
tmux send-keys -t '=fleet:worker-work' -l 'read ../briefs/worker-work.md and follow it'
sleep 1                                   # let the TUI take the paste
tmux send-keys -t '=fleet:worker-work' Enter
```

- `-l` sends the text **literally** — no key-name interpretation, so a brief
  containing `Enter` or `C-c` as words is safe.
- The `=` prefix on the target is exact matching. Without it tmux matches by
  prefix and your keystrokes can land in a stranger's session.
- Submit as a **separate** `send-keys … Enter`, **after a pause**. Back to back,
  the Enter overtakes the paste and lands in an empty composer: the text sits
  there unsent, and the window looks exactly like a worker ignoring you.
  Measured on a codex TUI — same call, no delay, nothing happened; the same
  Enter a moment later ran it. A second is enough; a long brief may want two.
- Submitting is not delivery. **Verify**: capture the pane and check the
  composer went empty (or that the agent is working). If your text is still
  sitting in the box, send `Enter` again rather than retyping the brief.
- Never put `\n` inside the `-l` text: the TUI submits at the first newline and
  the rest of your brief is typed into an empty prompt as a second message.
- **Therefore: never type a long brief.** Write it to a file and send one line
  pointing at it. That is the pattern below.

Read a window back:

```bash
tmux capture-pane -p -t '=fleet:worker-work'            # the visible screen
tmux capture-pane -p -S -300 -t '=fleet:worker-work'    # with scrollback
```

**Poll; do not assume.** A worker takes minutes to hours. Send, then capture
periodically. A capture that has not changed is not a failure — a capture
showing a shell prompt is.

---

## 3. Briefing a worker

Workers start **bare**, with no prompt: they know nothing — not their role, not
their branch, not the task. Your first message must therefore be entirely
self-contained. Write it to `<repo>-fleet/briefs/<session>/<window>.md` — the
path your kickoff brief names, outside every worktree so it never dirties a
branch — and point the worker at it.

Template:

```markdown
# Brief: worker-<label>

You are a worker in a ccfleet. You work alone, in one worktree, on one branch.

- Worktree (your cwd, and the ONLY tree you may edit): <abs path>
- Branch: fleet/worker-<label>
- Base: <sha> — everything else on this branch is yours
- Never touch: the main checkout, any other fleet worktree, any other branch.

## Task
<one job, scoped so it does not need another worker's unfinished work>

## Definition of done
1. The change is complete and committed on your branch (small commits are fine).
2. The repository's own gates pass. Read its AGENTS.md / CLAUDE.md /
   CONTRIBUTING for what those are and run them — do not invent your own.
3. You have not edited anything outside your worktree.

## Report back
Reply with exactly these four sections, short:
- CHANGED: files, one line each, why
- COMMITS: `git log --oneline <base>..HEAD`
- GATES: each command you ran and its result (paste the failing output if any)
- NOT DONE: anything you skipped, blocked on, or deliberately left out
```

Two rules for you when writing the task:

- **One job per worker.** If two tasks touch the same file, they are one task.
- **State the gates by name, not by category.** "Run the tests" is a guess the
  worker has to make; `python3 scripts/lint.py` and `python3
  pkg/tests/test_thing.py` are instructions. Read the repo's own AGENTS.md /
  CONTRIBUTING once yourself and put the literal commands in every brief —
  including the ones that are unusual there (a repo with no pytest, where each
  test file is run directly, is exactly where a worker invents its own runner
  and reports a green that means nothing).

---

## 4. Briefing a checker

A checker never edits. Give it a branch and a worktree path:

```markdown
# Brief: checker-<label>

You are a checker in a ccfleet. You review; you do not edit anything, anywhere.

- Review: git -C <repo> diff <base>...fleet/worker-<label>
- The worker's tree, for running its tests: <worktree abs path>
- Read that repo's AGENTS.md / CLAUDE.md first: its invariants are what a
  reviewer is for.

Do:
1. Read the whole diff.
2. Run the repo's gates inside the worker's worktree (cd there; do not copy
   files out).
3. Report findings ranked most-severe first: file:line, what is wrong, what
   breaks because of it. Say plainly if you found nothing.

Do not: edit files, commit, rebase, or "just fix" anything. If a fix is
obvious, say what it is — the worker or the orchestrator applies it.
```

Checkers are cheap and independent. Two checkers on one branch, given
different lenses ("correctness", "does it break the invariants in AGENTS.md"),
find more than one checker asked for both.

---

## 5. Merging — yours alone

Only you see every branch, so only you merge. In the **main checkout**:

```bash
git -C <repo> merge --no-ff fleet/worker-<label>
```

1. Merge only branches whose checker reported clean, or whose findings you
   have had fixed on the worker's branch (re-brief the worker; do not fix it
   in the main checkout — the worker's tree would then be behind).
2. Merge one at a time, and **re-run the repo's gates after each merge**. Two
   changes that each pass alone can fail together; that is the failure this
   whole shape exists to catch.
3. Conflicts are yours to resolve. If a conflict means one worker's design has
   to change, that is a new brief, not a hand-edit.
4. If a merge is wrong, `git merge --abort` (or reset to the pre-merge commit)
   and re-brief. Never rewrite a worker's branch under it while its session is
   live — it will commit on top of a history you moved.

---

## 6. What a fresh worktree does not have

A worker's worktree is a clean checkout. Tracked files ride along — so
`AGENTS.md`, `CLAUDE.md`, `.claude/`, hooks under version control all work
untouched. Anything **untracked or gitignored does not**:

- `node_modules/` — a JS package needs `npm install` in that worktree first.
- Virtualenvs, `.env` files, local config (`config.toml` and friends), build
  output, caches.
- Uncommitted work in the main checkout. Workers branched from a **commit**;
  `ccfleet up` says so when the main checkout is dirty.

Tell the worker which of these it must set up, in its brief. A worker that
discovers this alone spends a turn on it and may guess wrong.

---

## 7. Teardown

Do not tear down until merged work is pushed or you have read what was kept.

```bash
ccfleet down            # kills the tmux session; removes only clean+merged worktrees
ccfleet down --dry-run  # what it would remove and keep, first
```

`down` keeps any worktree with uncommitted changes, unmerged commits, or a live
window still sitting in it, and names it with the reason. That last one matters
to you: `down` acts on one tmux session but on every `fleet/` worktree in the
repository, so it will not pull a worktree out from under another fleet — and
it will not remove one you still have a shell in. `--force` drops the worktrees anyway (uncommitted
edits go with them) but still never deletes an unmerged branch — commits
survive a forced teardown. Your `briefs/` directory is left behind and named
in the output; it is yours to delete.

---

## 8. Failure modes worth recognising

| what you see | what it means |
|---|---|
| `dead (exit N)` in `ccfleet status` | the agent exited; its last screen is still in the pane (§1) |
| a member never answers | it may be mid-turn — capture the pane before re-sending |
| an **instant** answer, one line, no work | almost always an account limit. Read it: *"You've hit your monthly spend limit"* and *"5-hour limit reached"* both arrive in well under a second and look nothing like a report |
| a member is missing from `ListAgents` | expected unless it shares your account space (§2). Not evidence of anything |
| your text is in the composer, unsent | the `Enter` beat the paste — send `Enter` again, then pause before it next time |
| your keystrokes vanished entirely | wrong target, or the pane is not at a prompt; check `ccfleet status` |
| the second half of a brief became its own message | a newline inside `send-keys -l` (§2) |
| a worker edited the main checkout | your brief did not name the worktree as its only tree |
| every worker still on its opening screen while you have an answer | **you absorbed the job.** Re-read the rule at the top, then brief it out |
| a worker cannot find a file you just wrote | it is untracked or uncommitted, so it is not in that worktree's commit |
| the board has pending tasks and every member is idle | their dependencies are blocked. `block` cascades, so check for a `blocked` task upstream |
| a task keeps coming back rejected | the brief is underspecified, not the worker. Rewrite it with the acceptance test in it |
| a worker reports a missing dependency or config file | §6 — its worktree has no untracked files at all |
| `git worktree add` refused | that branch is checked out elsewhere; `ccfleet status` shows where |

**Always read a reply, never just confirm delivery.** An account with no budget
left answers immediately and cheaply, so "the message arrived and something came
back" is not the same as "the work happened". `cca usage --sort` shows what each
account has left; re-brief on a window whose account still has headroom.

---

## 9. The shape of a good round

1. `ccfleet status` — know who is up.
2. **Decide whether the job splits at all.** If it does not, say so and stop —
   do not quietly do it yourself (§ the rule at the top).
3. Commit anything the workers need to read, and tell them the base.
4. Decompose the job into one independent task per worker.
5. Write every brief to `<repo>-fleet/briefs/<session>/`, then send the
   one-liners.
6. Poll. Collect the four-section reports.
7. Brief a checker per finished branch.
8. Merge clean branches one at a time, re-running the gates.
9. Report to the human: what merged, what was kept back and why.
