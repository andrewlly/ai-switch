# cca — coding-agent account manager

Run any number of **Claude Code** and **Codex** accounts side by side, each in
its own **space**: separate credentials, projects, history, sessions, skills
and settings.

One script, two entry points — both symlinks to `cca.py`. The name you invoke
picks the agent:

```
cca                  launch the default claude account
cxa                  launch the default codex account
cca <name> [args]    launch that claude account; args pass through to claude
cxa <name> [args]    launch that codex account
```

Every account also gets a three-letter shortcut of its own — `cc`/`cx` plus a
letter from its name — created as another symlink to the same script:

```
ccw   work        ccc   client
ccp   personal    cxc   codex      # every argument goes to the agent
```

So `ccc -c` is exactly `cca client -c`. These are managed for you: created when
an account is added, kept as-is across a rename (your muscle memory survives),
and removed when the account goes.

Management subcommands work from either entry and act on the whole registry:

```
cca list             every account, both agents
cca pick             choose one from a list, with live usage beside each
cca usage            how much of each account's 5-hour and weekly limits is spent
cca best --launch    start whichever account has the most left
ccfleet up -w work -w codex -c client
                     a tmux fleet: orchestrator, workers, checkers
cca add <name>       a claude account   (cxa add <name> -> a codex one)
cca login <name>
cca status | doctor | which | default | tools
```

There are no shell functions and nothing to source — every one of these is a
real command on `PATH`, so they work in scripts, tmux and non-interactive
shells alike.

## Two tools, two variables

| | claude | codex |
|---|---|---|
| entry point | `cca` | `cxa` |
| shortcut prefix | `cc` | `cx` |
| binary | `claude` | `codex` |
| config-dir variable | `CLAUDE_CONFIG_DIR` | `CODEX_HOME` |
| default dir | `~/.claude` | `~/.codex` |
| credentials | `<space>/.credentials.json` | `<space>/auth.json` |
| identity from | `.claude.json` → `oauthAccount` | `auth.json` → `id_token` JWT |
| auth check | `claude auth status` (JSON) | `codex login status` (text) |
| shareable assets | skills, agents, commands, output-styles, plugins, hooks | skills, plugins, rules, themes, prompts |
| legacy quirk | **yes** — see below | no |

An account records which tool it belongs to, and each entry only launches its
own. Getting it wrong tells you the right command rather than failing vaguely:

```console
$ cxa work
error: 'work' is a claude account; launch it with `cca work` (you used `cxa`)
```

Account names are unique across both agents.

## The one non-obvious thing: `legacy_default`

This affects **Claude only**. Codex is symmetric — `CODEX_HOME=~/.codex`
selects the original account correctly.

The account Claude Code installs *first* keeps its main config at
`~/.claude.json` — in your **home root**, not inside `~/.claude`. Every later
space keeps its config **inside** the space.

That asymmetry is a trap. Passing `CLAUDE_CONFIG_DIR=$HOME/.claude` does
**not** select the original account — Claude looks for
`~/.claude/.claude.json`, finds nothing, prints a "configuration file not
found" notice and comes up amnesiac (no projects, no history):

```console
$ CLAUDE_CONFIG_DIR=$HOME/.claude claude auth status
Claude configuration file not found at: /home/you/.claude/.claude.json
```

It does not fail loudly: credentials still resolve, so the session reports
itself logged in. The original account only works when `CLAUDE_CONFIG_DIR` is
**unset**, so it is flagged `legacy_default: true` and launched by *removing*
the variable. `cca doctor` flags any account that points at a tool's default
dir without the right flag, in either direction.

## Codex token expiry is not a liveness signal

Codex's `id_token` carries an `exp` claim that goes stale within days, but the
session stays valid because Codex holds a `refresh_token` and renews on
demand. Treating `exp` as an expiry produces a false `token-expired` state on
a perfectly good account, so cca reports Codex's `last_refresh` instead and
never flags it. Claude's `expiresAt` *is* a real expiry and is flagged.

## Layout

```
~/.local/share/cc-accounts/cca.py     the tool (stdlib-only Python)
~/.local/share/cc-accounts/usage.py   rate-limit windows, live from the API
~/.local/share/cc-accounts/picker.py  the full-screen chooser
~/.local/share/cc-accounts/fleet.py   ccfleet - a tmux fleet of sessions
~/.local/share/cc-accounts/board.py   the shared task board members claim from
~/.local/share/cc-accounts/FLEET.md   the orchestrator's operating manual
~/.local/bin/cca -> cca.py            entry point for claude
~/.local/bin/cxa -> cca.py            entry point for codex
~/.local/bin/ccfleet -> fleet.py      the fleet launcher
~/.local/bin/ccw, ccp, ccc, cxc …     one shortcut per account
~/.claude-accounts/accounts.json      the registry (v3)
~/.claude-accounts/usage-cache.json   last good usage, for when a probe fails
~/.claude-accounts/spaces/<name>/     spaces for new accounts
```

A registry entry:

```json
"work": {
  "tool": "claude",
  "config_dir": "/home/you/.claude",
  "legacy_default": true,
  "description": "Work org (original install)",
  "alias": "ccw",
  "env": {},
  "created": "2026-08-14T01:52:35+00:00"
}
```

Alongside the accounts, the registry records which one each entry launches
bare:

```json
"defaults": { "claude": "work", "codex": "codex" }
```

A tool with exactly one account needs no entry there at all — it is the
default implicitly. Older registries are upgraded on first load: entries
without a `tool` key are read as Claude accounts, the single global `default`
is filed under its own tool, and a v1 registry also adopts an existing Codex
install. Detection is skipped for directories already registered, so an
upgrade can never adopt `~/.codex` twice.

Nothing is ever migrated — `~/.claude`, `~/.claude-personal`, `~/.claude.json`
and `~/.codex` stay exactly where they are.

## Commands

| command | what it does |
|---|---|
| `cca` / `cxa` | launch that agent's default account |
| `ccw`, `ccp`, `ccc`, `cxc` … | per-account shortcuts; all args go to the agent |
| `cca <name> [args]` | launch that claude account (`cxa <name>` for codex) |
| `cca list [--json] [--du] [--tool T]` | table of accounts; `*` = this shell, `>` = default |
| `cca pick` | **the chooser** — every account with live usage, arrow keys, ⏎ launches |
| `cca usage [name] [--sort] [--json] [--cached] [--no-bars]` | how much of each account's limits is spent |
| `cca best [--launch] [-q] [--json]` | the account with the most headroom left |
| `cca run [--dry-run] <name> [args]` | same, explicitly; `--dry-run` prints the command |
| `cca exec <name> -- <cmd>` | run any command inside an account's environment |
| `cca add <name> [--tool T] [--dir P] [--alias A] [--login]` | create a space |
| `cca rm <name> [--purge]` | unregister; `--purge` also deletes the space |
| `cca rename <old> <new>` | rename; the space is untouched |
| `cca status [name] [--json]` | authoritative auth status per account |
| `cca which` | which account backs this shell |
| `cca default [name]` | get/set what bare `cca`/`cxa` launches |
| `cca login/logout <name>` | authenticate a space |
| `cca link <name> <asset> --from <acct>` | symlink a shared asset in |
| `cca unlink <name> <asset>` | remove that link |
| `cca doctor` | health-check every account |
| `cca env <name>` | print shell exports (`eval "$(cca env work)"`) |
| `cca alias [name] [--set A] [--repair]` | show, set or rebuild the shortcuts |
| `cca tools` | show what each entry point drives |
| `ccfleet up [-g GOAL] [--gates CMD] [-s S] [-o A] [-w A[:L]]… [-c A[:L]]… [--repo D] [--base R] [--no-orch] [--dry-run]` | start a tmux fleet ([below](#a-fleet-of-sessions-ccfleet)); `-g` also starts every member working |
| `ccfleet status [-s S] [--json]` | each window, each `fleet/` branch, what is unmerged |
| `ccfleet board add --title T [--brief B] [--files F] [--dep ID] [--role R]` | put a task on the board |
| `ccfleet board next --as MEMBER [--wait S]` | claim the next task for a member; **blocks** |
| `ccfleet board done ID` / `accept ID` / `reject ID --why W` / `block ID --why W` | report on one |
| `ccfleet board close` / `reopen` | decomposition finished — the only thing that lets members stop |
| `ccfleet board list [--json]` | every task and its state |
| `ccfleet down [-s S] [--force] [--dry-run]` | kill the fleet; keep every worktree that still holds work |

### Argument order

Everything **after** the account name is passed straight through to the tool,
so cca's own flags go **before** it:

```bash
cca run --dry-run work -c      # cca's --dry-run
cca run work --dry-run         # claude's (invalid) --dry-run
```

## Choosing an account by what it has left

Every plan meters two rolling windows — five-hour and seven-day — and an
account is only as usable as its **tighter** one. 5% of the five-hour window is
worth nothing behind a weekly window at 99%, so everything here scores an
account by the headroom in its binding window alone, and says which window that
is.

```console
$ cca usage --sort
  ACCOUNT   PLAN  5-HOUR           WEEK             FREE  IN  RESETS
* personal  pro   ▇▇▇▁▁▁▁▁▁▁  28%  ▇▁▁▁▁▁▁▁▁▁   3%   72%  5h  3h 58m
  client    team  ▁▁▁▁▁▁▁▁▁▁   0%  ▇▇▇▇▇▁▁▁▁▁  47%   53%  wk  4d 16h
  work      team  ▇▇▇▇▁▁▁▁▁▁  37%  ▇▇▇▇▇▁▁▁▁▁  47%   53%  wk  2d 10h
  demo      team  ▇▇▇▇▁▁▁▁▁▁  36%  ▇▇▇▇▇▁▁▁▁▁  51%   49%  wk   5d 0h
  codex     plus               -   ▇▇▇▇▇▇▇▇▇▇ 100%    0%  wk  9h 46m  from last session, 5d 13h ago

$ cca best
personal  (86% free in the 5h window, the tighter one)

$ cca best --launch          # …and start it
$ ccx() { cca best -q; }     # or just the name, for scripts
```

The bars are sized to whatever the terminal has left once every other column
is satisfied, so they widen on a wide window, narrow on a small one, and drop
out entirely rather than squeezing the numbers (`--no-bars` forces that).

`cca pick` is the same information as a chooser:

```
┌ cca — 5 accounts, 4 live ───────────────────────────────────────────────┐
│  ACCOUNT   PLAN  5-HOUR         WEEK           FREE  RESETS             │
│  client    team  ▁▁▁▁▁▁▁▁  0%  ▇▇▇▇▇▁▁▁  47%   53%  4d 16h              │
│  codex     plus         -      ▇▇▇▇▇▇▇▇ 100%    0%     10h  from last…  │
│★ personal  pro   ▇▇▁▁▁▁▁▁ 17%  ▇▁▁▁▁▁▁▁   2%   83%  4h 11m              │
│  work      team  ▇▇▇▁▁▁▁▁ 27%  ▇▇▇▇▇▁▁▁  46%   54%  2d 10h              │
│  demo      team  ▇▇▇▁▁▁▁▁ 34%  ▇▇▇▇▇▁▁▁  51%   49%   5d 0h              │
└ ↑↓ move   ⏎ launch   b best   r refresh   q quit ───────────────────────┘
```

`↑↓`/`jk` move, `1`-`9` jump, `g`/`G` go to the ends, `b` jumps to `★` (the
account with the most headroom), `r` re-probes, `q` quits, `⏎` launches — the
process is replaced by the agent exactly as `cca <name>` would have done, so
nothing wraps the session and nothing survives it. Bare `cca` still launches
your default; it only opens the picker when there are several accounts and no
default set.

### Where the numbers come from

**Live.** `GET /api/oauth/usage` with each account's own bearer token — the
same call Claude Code's `/usage` makes — every account in parallel, so the
picker opens in about a second. A cached figure is true as of whenever that
account last ran, which for the idle account you are trying to switch *to* is
exactly when it is least informative.

A cache is still written after every successful probe and read back **only**
when a live probe fails, always labelled with its age, because a number with an
unstated age is worse than no number. Two corrections keep an old number
honest: a window whose reset time has passed reads as 0% (it rolled over while
nobody was looking), and everything else carries how stale it is. `--cached`
skips the network entirely — that is a request, not a failure, so those rows
say `last reading 1m ago` with no reason attached.

**The endpoint that reports your limits has a limit of its own** — on how
often you may ask, not on what you have spent. A burst of probes earns an HTTP
429 with a `Retry-After` of a couple of minutes, and the row falls back to its
last reading rather than going blank:

```
work  team  ▇▇▇▁▁▁▁▁ 37%  ▇▇▇▇▁▁▁▁ 47%  53%  wk  2d 10h  last reading 3m ago (usage API busy, retry in 62s)
```

Deliberately never phrased as "rate-limited": read on a usage row, that says
the *account* is spent, which is the one thing it does not mean. The server's
own `Retry-After` is quoted in seconds, because it is a countdown somebody is
about to wait out.

Codex has no equivalent endpoint — it reports limits as a `rate_limits` event
inside the session rollout it writes while working — so a codex account is read
from the newest rollout on disk and labelled `from last session`.

### As a library

`usage.py` takes plain dicts, not registry objects, so it can be loaded by path
from anywhere. `cca.py` exposes the one function a service wants:

```python
import importlib.util, os
spec = importlib.util.spec_from_file_location(
    "cca", os.path.expanduser("~/.local/share/cc-accounts/cca.py"))
cca = importlib.util.module_from_spec(spec); spec.loader.exec_module(cca)

cca.usage_reports(ranked=True)   # every account, most headroom first
```

Each report is `{account, tool, plan, email, ok, error, source, stale_seconds,
windows: {five_hour, seven_day}, binding, free_pct, credits}`; a window is
`{used_pct, free_pct, resets_at, resets_in, rolled_over}`. An account that
could not be measured comes back with `ok: false` and the reason rather than
being dropped. `cca usage --json` is the same data from a shell.

A service consuming this should import the tool rather than shipping a second
idea of which accounts exist — the registry is one file with one owner, and a
reader that reimplements it is a reader that goes stale the day an account is
added.

## A fleet of sessions: `ccfleet`

Several accounts means several agents can work at once — but not in one
session's context. `ccfleet` starts a **tmux fleet** of separate CLI processes:
one orchestrator, N workers each on their own git worktree and branch, N
checkers reviewing what the workers produced.

```bash
ccfleet up -w work -w codex -c client   # 2 workers (one claude, one codex), 1 checker
ccfleet status                          # what each window and each branch is doing
ccfleet down                            # kill it, keeping anything unmerged
```

| | where it works | what it does |
|---|---|---|
| **orch** | the main checkout | decomposes, briefs, collects, merges |
| **worker** | its own worktree, on `fleet/worker-<label>` | writes code |
| **checker** | the main checkout, reading a worker's tree | reviews; never edits |

The shape is per launch, not a fixed default: any account can take any role,
the same account can appear more than once, and `-o` (or the account you are
already on) decides who orchestrates.

```bash
ccfleet up -w work:parse -w work:report -o personal
    # one account, two workers; the label after ':' names the window,
    # the branch and the worktree, so a repeat is never a collision

ccfleet up -w work --no-orch
    # no orch window - you are the orchestrator, from the session you typed this in
```

### Why through `cca`

Every window is launched as `cca <account>` or `cxa <account>`, never as a bare
`claude`. A tmux pane inherits the **tmux server's** environment — whatever it
had whenever it was first started, which is not necessarily this shell's — so a
hand-rolled fleet reads whichever config directory that server happened to
carry and quietly puts two windows on one account. Launching through the
account manager makes each window's environment its own account's business.

### Isolation is a worktree, not a convention

Each worker gets `git worktree add -b fleet/worker-<label> <repo>-fleet/worker-<label> <base>`,
so two workers editing the same file cannot collide — the collision surfaces
once, at merge, in front of the one session that can see both branches. The
worktrees live in a sibling directory (`<repo>-fleet/`) rather than inside the
repository, because a repository with its own clones inside it confuses every
tool that walks it, including git.

Every worker branches from a **commit**, so uncommitted work in the main
checkout is not in any of them. `ccfleet up` says so when the checkout is dirty
rather than letting it be discovered later.

### Nothing is declared

There is no fleet state file. `status` and `down` ask tmux for the windows, git
for the branches and worktrees, and tmux again for who is sitting in each one —
which means they are right about a fleet started from another shell, and cannot
go stale about one that died.

```console
$ ccfleet status
fleet fleet   repo ~/src/thing   base 4a91c02 (main)
#  WINDOW          RUNNING       PID     CWD
0  orch            claude        214431  ~/src/thing
1  worker-work     claude        214502  ~/src/thing-fleet/worker-work
2  checker-client  dead (exit 1) 214560  ~/src/thing

BRANCH             STATE  AHEAD  MERGED  WORKTREE
fleet/worker-work  dirty  3      no      ~/src/thing-fleet/worker-work
```

`RUNNING` names the agent because each member is launched as **argv**, not as a
shell string: hand tmux one string and it inserts `sh -c`, the pane's
foreground process group belongs to that shell, and every window reports as
`bash` however healthy it is. Which is exactly what the first version of this
did — and the first orchestrator launched with it noticed, from inside the
fleet, that the column was lying about all three windows including itself.

A window that outlives its agent is tmux's own `remain-on-exit`, set **before**
the agent is spawned. That ordering is the point: an account that dies on
startup exits in milliseconds, and a window created without the option first
would be destroyed with the error still in it. Instead the pane goes
`dead (exit N)` and keeps its last screen; `tmux respawn-pane -k` puts a member
back.

### Teardown never destroys work

`down` kills the session, then removes **only** worktrees that are clean,
merged into the base, and empty of any live window. Everything else is kept and
named with the reason:

```console
$ ccfleet down
kept
  fleet/worker-report   ~/src/thing-fleet/worker-report   not merged (3 commits)
  fleet/worker-parse    ~/src/thing-fleet/worker-parse    uncommitted changes
  fleet/worker-api      ~/src/thing-fleet/worker-api      in use by other:worker-api
```

That third reason is why occupancy is read from tmux rather than from a fleet's
own records. `down -s A` acts on one tmux session but on *every* `fleet/`
worktree in the repository, so a second fleet on the same repo — or a shell you
left in a worktree — would otherwise have the ground taken out from under it.
The check runs after the session is killed, so a fleet never blocks its own
teardown.

`--force` drops those worktrees anyway — and says which uncommitted changes
went with them — but still never deletes an unmerged branch: commits survive a
forced teardown. `--dry-run` decides without doing.

### A goal, split once, worked until it is done

The topology flags are the whole required surface. `up` writes a **shared task
board** beside the worktrees and starts every member inside a claim loop
against it:

```bash
ccfleet up -w work:db -w codex:design -c personal:review
```

**Your prompt stays your prompt.** The rules a member works by are handed to
`claude` as `--append-system-prompt-file`, so they govern every turn without
being wrapped around anything you type. Give the orchestrator the goal in your
own words — at launch with `-g`, or typed into its window afterwards — and it
arrives verbatim, with the decomposition rules already standing behind it. A
brief delivered as a *first message* instead is one turn of context competing
with everything after it, which is how an orchestrator briefed that way came to
read its brief and then do the whole job itself. Codex has no equivalent flag,
so its members get the same file as an opening message; that asymmetry is
deliberate.

`--goal` is optional and only records text on the board — you can just as well
tell the orchestrator once it is up, which is the normal way to work. Pass it
when you want the fleet to start decomposing without you. `--gates` is
optional too: it names the literal commands every member must pass before
reporting a task done, and exists only because a member left to guess picks its
own and reports a green that means nothing. `--no-board` turns all of it off
and goes back to bare members you brief by hand.

```bash
ccfleet up -g "Add the three missing JSON contracts" \
    --gates "python3 contracts/validate_examples.py" \
    -w work:db -w codex:design -c personal:review
```

The orchestrator's only job is then decomposition — one `board add` per
independent unit of work, each naming the files it owns:

```bash
ccfleet board add --title "contract: loop.json" --files "contracts/loop.schema.json" \
    --brief "Write the schema and register it in NAMES."
ccfleet board add --title "wire both in" --dep t1 --dep t2 --files "contracts/validate_examples.py"
```

Every member runs the same three lines forever, and that is the whole protocol:

```bash
ccfleet board next --as worker-db --wait 240   # blocks until there is work
# ...do exactly that task...
ccfleet board done t3 --as worker-db --note "..."
```

```console
$ ccfleet board list
goal  Add the three missing JSON contracts
ID  STATE     ROLE     OWNER          DEPS   TITLE
t1  accepted  worker   worker-design  -      contract: loop.json
t2  claimed   worker   worker-db      -      contract: case_run.json
t3  pending   worker   -              t1,t2  wire both in
t4  pending   checker  -              -      review: contract: case_run.json
```

**Pull, not push — which is the only reason this works across platforms.**
Pushing work into an agent means typing into its terminal and hoping the
keystrokes landed. Pulling means the agent runs a command, and running commands
is the one capability claude, codex and anything else all share. Nothing is
injected into anyone's context; there is no daemon, and no dispatcher to lose
track of who is idle.

**`next` blocking is what removes the dispatcher.** An idle member is a member
sitting inside a shell call, not one that has to be found and woken. It exits
`4` when the wait elapsed with nothing claimable — call it again — and `5` only
when the board is drained: nothing left *and* nobody still working. A member
that stopped on `4` would never come back for the work the busy member is about
to unblock, so that distinction is the loop.

**An open board is never drained, and that is not a detail.** A member stops
only when the board is *closed* — the orchestrator saying decomposition is
finished — and there is nothing outstanding. Without that rule every member
launches, finds an empty board, is told there is no work left, and goes home
before the orchestrator has written its first task; and a fast member clears
the first task and quits between two `add` calls. So until `board close`, a
member with nothing to do waits. A member waiting too long is visible in
`list`; a member that stopped early is a fleet that quietly did nothing.

**A checker turns "done" into "agreed".** With a checker in the fleet, a
finished task goes to `review` and a review task appears; the checker
`accept`s it, or `reject`s it with a reason. A rejection puts the work back on
the board as `pending` — deliberately unassigned, since the member that did it
may be out of budget or gone — carrying the reason in the task, where the next
attempt reads it. Every attempt gets its own review.

**Blocking cascades.** `block` marks everything transitively waiting on that
task blocked too. Without it a stranded task stays `pending` for ever, the
board never drains, and every member spins on "nothing yet" — a full task list
that is actually a deadlock. Work already claimed or finished is left alone: it
is in a member's hands and will report.

Every mutation takes an exclusive lock and rewrites the file atomically, so two
members reaching `next` in the same instant get different tasks. That is the
normal case, not the edge case.

### Compared with Claude Code's agent teams

Claude Code has [agent teams](https://code.claude.com/docs/en/agent-teams):
a lead, a shared task list with dependencies, teammates that claim work and
message each other automatically. If you are on one account and only running
Claude, use that — it is better integrated than this will ever be, and the
messaging is real rather than a file.

This exists for the case it does not cover. Teammates are spawned by the lead
process and run on the lead's credentials: the cost lands on one account, and
nothing outside Claude Code can join. A `ccfleet` member is an ordinary CLI
launched through `cca`/`cxa`, so a fleet can span four Claude accounts and a
Codex one, and the board is a JSON file any of them can read.

### The orchestrator's half

The launcher is only the infrastructure. The protocol lives in
[`FLEET.md`](FLEET.md), which the orchestrator is pointed at by a kickoff brief
written to `<repo>-fleet/briefs/<session>/orch.md`. It is written for the agent, not for
you, and covers the part that is genuinely awkward: **there is no guaranteed
messaging channel between CLI sessions.** Peers sometimes appear to each other
and can be messaged directly; sometimes they do not, and Codex sessions never
do. So the manual specifies two channels — the peer channel when a member shows
up, and `tmux send-keys` / `capture-pane` as the fallback that always works —
plus the brief-file pattern that makes the tmux channel reliable (a newline
inside `send-keys` submits, so a long brief goes in a file and a one-line
message points at it).

What "sometimes they do not" turned out to mean, measured on a three-account
fleet: a session sees only peers **in its own account space**. The
orchestrator's own account was visible to it; the two members on other accounts
were running and idle and never appeared, and neither did other long-lived
sessions on those accounts. Which is the right answer for a tool whose whole
job is keeping account spaces apart — and it makes the tmux channel the primary
one for any fleet that spans accounts, not the fallback.

Workers and checkers start **bare**, with no prompt at all. They know nothing
about the fleet until the orchestrator tells them, which is why the manual's
brief template is the length it is.

## Adding accounts

The entry you use decides the agent — no flag needed:

```bash
cca add client --description "Acme contract"   # a claude account
cxa add work                                   # a codex account
cxa login work                                 # OAuth into that space alone
cxa work                                       # launch it
```

`--tool` overrides the entry if you ever want `cca add x --tool codex`, and
`--alias` overrides the generated shortcut. Shortcut allocation walks the
letters of the account name (`client` → `ccc`; if that is taken, `ccl`, `cci`,
…) and refuses any name that is an entry point or already a real command, so
nothing on your `PATH` is ever shadowed. Only symlinks pointing at this script
are ever removed.

## Sharing assets

Spaces are sealed by default. Share deliberately, and only within one tool:

```bash
cca link personal skills --from work
```

Cross-tool links are refused (a Claude skill is not a Codex skill), as are
credentials and config files. A link never overwrites a real directory.

## Prompt integration

Every launched account exports `CCA_ACCOUNT` and `CCA_TOOL`, so the prompt
needs no helper function:

```bash
PS1='[${CCA_ACCOUNT:-default}] '$PS1
```

## Why `cca` and not `cc`

`cc` is the system C compiler (`/usr/bin/cc` → gcc). Because `~/.local/bin`
comes **first** on `PATH`, installing a `cc` there would shadow the compiler
for every build on this machine.

## Extending

The file is flat on purpose. To support another agent, subclass `Tool`:

```python
class MyTool(Tool):
    name = "mytool"
    binary = "mytool"
    config_env = "MYTOOL_HOME"
    credentials_name = "creds.json"
    linkable = ("skills",)
    # then: config_file, credentials_file, identity, auth_status,
    #       login_argv, logout_argv, detect
```

…and add an instance to `TOOLS`. Everything else — listing, doctor, linking,
purge guards — works off that interface.

A new agent also wants an entry point: add it to `ENTRYPOINTS` and symlink
the script under that name in `~/.local/bin`.

Other extension points: a new subcommand is a `cmd_*` function plus a
subparser plus a name in `SUBCOMMANDS`; `SESSION_ENV` lists the variables
stripped on launch; each account's `env` map is applied at launch (no CLI
setter yet — edit `accounts.json`); `CCA_HOME` relocates the whole registry.

## Tests

```bash
python3 ~/.local/share/cc-accounts/test_cca.py      # 69 tests
python3 ~/.local/share/cc-accounts/test_usage.py    # 47 tests
python3 ~/.local/share/cc-accounts/test_fleet.py    # 89 tests
python3 ~/.local/share/cc-accounts/test_board.py    # 36 tests
```

Hermetic, both of them. `test_cca.py` runs against a temporary `CCA_HOME`
**and** a fake home directory and a temporary `BIN_DIR`, so account detection
can never see or adopt your real accounts and the suite can never rewrite your
real `~/.local/bin`. `test_usage.py` never opens a socket — every live probe
goes through an injected opener — and asserts two things that are easy to break
by accident: that every picker line is exactly the terminal width at every
width from 40 to 132 columns, and that `fmt_duration` never returns a seventh
character (which is how a row once pushed the frame's right edge off the
screen).

`test_fleet.py` starts no tmux server and launches no agent: `--dry-run` is the
seam, and the assertions are on the exact `git` and `tmux` argv a mixed
claude+codex fleet would run. The git half is real — a throwaway repository per
case, with `GIT_CONFIG_GLOBAL` pointed at `/dev/null` so your own git config
cannot reach into a fixture — because the thing worth testing is the teardown
decision, and "clean and merged" is a question only git can answer.
