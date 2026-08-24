#!/usr/bin/env python3
"""Hermetic tests for ccfleet.

No tmux server is started, no agent is launched and no real account is read:
every mutating command goes through the --dry-run seam, and the git side runs
against a throwaway repository created here. Run directly:

    python3 ~/.local/share/cc-accounts/test_fleet.py
"""

import argparse
import contextlib
import importlib.util
import io
import json
import os
import shlex
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent

# Read at import time by both modules, so set them before loading anything.
_TMP = Path(tempfile.mkdtemp(prefix="fleet-test-"))
os.environ["CCA_HOME"] = str(_TMP / "accounts-home")
os.environ["NO_COLOR"] = "1"          # assertions compare plain text
# Keep git out of the operator's configuration: a global core.hooksPath or
# commit.gpgsign would otherwise reach into these fixtures.
os.environ["GIT_CONFIG_GLOBAL"] = os.devnull
os.environ["GIT_CONFIG_SYSTEM"] = os.devnull

_spec = importlib.util.spec_from_file_location("fleet", HERE / "fleet.py")
fleet = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fleet)
cca = fleet.cca


REGISTRY = {
    "version": 3,
    "defaults": {"claude": "work", "codex": "cx"},
    "accounts": {
        "work": {"tool": "claude", "config_dir": "/nonexistent/work",
                 "legacy_default": True, "description": "", "alias": "ccw",
                 "env": {}, "created": ""},
        "client": {"tool": "claude", "config_dir": "/nonexistent/client",
                   "legacy_default": False, "description": "", "alias": "ccc",
                   "env": {}, "created": ""},
        "cx": {"tool": "codex", "config_dir": "/nonexistent/cx",
               "legacy_default": False, "description": "", "alias": "cxc",
               "env": {}, "created": ""},
    },
}

FAKE_BINARIES = {"tmux": "/usr/bin/tmux", "claude": "/usr/bin/claude",
                 "codex": "/usr/bin/codex"}


def git(repo, *args, check=True):
    argv = ["git", "-C", str(repo), "-c", "user.name=fleet test",
            "-c", "user.email=fleet@example.invalid", *args]
    p = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       text=True)
    if check and p.returncode:
        raise AssertionError(f"{shlex.join(argv)} failed:\n{p.stdout}")
    return (p.stdout or "").strip()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fleet-case-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

        # Registry: a temporary one, never the operator's.
        cca.ROOT = self.tmp / "cca-home"
        cca.REGISTRY = cca.ROOT / "accounts.json"
        cca.SPACES = cca.ROOT / "spaces"
        cca.write_json_atomic(cca.REGISTRY, REGISTRY)

        # The launcher symlinks ccfleet resolves to absolute paths.
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        for name in ("cca", "cxa"):
            (self.bin / name).write_text("#!/bin/sh\n")
        self._saved = {"BIN_DIR": cca.BIN_DIR, "which": cca.which,
                       "has_session": fleet.has_session,
                       "panes": fleet.session_panes}
        self.addCleanup(self._restore)
        cca.BIN_DIR = self.bin
        cca.which = lambda binary: FAKE_BINARIES.get(binary)
        fleet.has_session = lambda name: False
        fleet.session_panes = lambda name: []

        # A repository with one commit, and nothing else in it.
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-b", "main", "-q")
        (self.repo / "README").write_text("base\n")
        git(self.repo, "add", "README")
        git(self.repo, "commit", "-q", "-m", "base")
        self.base_sha = git(self.repo, "rev-parse", "HEAD")
        self.fleet_dir = self.tmp / "repo-fleet"

    def _restore(self):
        cca.BIN_DIR = self._saved["BIN_DIR"]
        cca.which = self._saved["which"]
        fleet.has_session = self._saved["has_session"]
        fleet.session_panes = self._saved["panes"]

    # -- helpers ---------------------------------------------------------

    @contextlib.contextmanager
    def capture(self):
        """Collect everything printed, including on the way out of a die()."""
        buf = io.StringIO()
        self.output = ""
        try:
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                yield buf
        finally:
            self.output = buf.getvalue()

    def up(self, workers=None, checkers=None, orch="work", no_orch=False,
           base=None, session="fleet", dry_run=True):
        args = argparse.Namespace(
            session=session, repo=str(self.repo), base=base,
            worker=workers, checker=checkers, no_orch=no_orch,
            # The default here is a convenience, not the CLI's: --no-orch and
            # -o are refused together, so the helper drops the default.
            orch=None if no_orch and orch == "work" else orch,
            dry_run=dry_run)
        with self.capture():
            rc = fleet.cmd_up(args)
        return rc, self.output

    def refuses(self, containing, **kwargs):
        """`up` must die and say why. (`up` captures its own output.)"""
        with self.assertRaises(SystemExit):
            self.up(**kwargs)
        self.assertIn(containing, self.output)

    @staticmethod
    def commands(output):
        """The argv of every mutating command a --dry-run printed.

        `+` lines are commands; `*` lines are the files ccfleet writes itself.
        """
        return [shlex.split(line.strip()[2:])
                for line in output.splitlines() if line.startswith("  + ")]

    @staticmethod
    def writes(output):
        return [line.split()[2] for line in output.splitlines()
                if line.startswith("  * ")]

    def add_worker_tree(self, label, dirty=False, commits=0):
        branch = f"fleet/worker-{label}"
        path = self.fleet_dir / f"worker-{label}"
        git(self.repo, "worktree", "add", "-q", "-b", branch, str(path),
            self.base_sha)
        for n in range(commits):
            (path / f"f{n}.txt").write_text(f"{n}\n")
            git(path, "add", f"f{n}.txt")
            git(path, "commit", "-q", "-m", f"work {n}")
        if dirty:
            (path / "scratch.txt").write_text("uncommitted\n")
        return branch, path

    def with_panes(self, occupants):
        """Fake `tmux list-panes -a` - who is sitting where - and leave every
        other probe (all the git ones) real."""
        real = fleet.probe

        def fake(argv, cwd=None):
            if argv[1:3] == ["list-panes", "-a"]:
                return 0, "\n".join(f"{w}\t{path}" for w, path in occupants)
            return real(argv, cwd)

        fleet.probe = fake
        self.addCleanup(lambda: setattr(fleet, "probe", real))

    def down(self, force=False, dry_run=False, session="fleet"):
        args = argparse.Namespace(session=session, repo=str(self.repo),
                                  base=None, force=force, dry_run=dry_run)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            rc = fleet.cmd_down(args)
        return rc, buf.getvalue()


class TestSpecParsing(Base):
    def test_bare_account_names_everything_after_itself(self):
        m = fleet.parse_spec("work", "worker", set())
        self.assertEqual(m, {"role": "worker", "account": "work",
                             "label": "work", "window": "worker-work"})

    def test_label_after_colon_replaces_the_account_in_the_name(self):
        m = fleet.parse_spec("work:parse", "worker", set())
        self.assertEqual(m["account"], "work")
        self.assertEqual(m["window"], "worker-parse")

    def test_repeated_account_is_suffixed_not_collided(self):
        taken = set()
        first = fleet.parse_spec("work", "worker", taken)
        second = fleet.parse_spec("work", "worker", taken)
        third = fleet.parse_spec("work", "worker", taken)
        self.assertEqual([m["window"] for m in (first, second, third)],
                         ["worker-work", "worker-work2", "worker-work3"])
        self.assertEqual({m["account"] for m in (first, second, third)}, {"work"})

    def test_same_label_in_different_roles_does_not_collide(self):
        taken = set()
        w = fleet.parse_spec("work", "worker", taken)
        ch = fleet.parse_spec("work", "checker", taken)
        self.assertEqual(w["window"], "worker-work")
        self.assertEqual(ch["window"], "checker-work")

    def test_label_charset_is_refused_not_sanitised(self):
        for bad in ("work:has space", "work:slash/es", "work:.dot", "work::"):
            with self.subTest(bad=bad), self.assertRaises(SystemExit):
                with contextlib.redirect_stderr(io.StringIO()):
                    fleet.parse_spec(bad, "worker", set())

    def test_empty_account_is_refused(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            fleet.parse_spec(":label", "worker", set())


class TestRefusals(Base):
    def test_unknown_account_names_the_known_ones(self):
        self.refuses("no account named 'nosuch'", workers=["nosuch"])
        self.assertIn("client, cx, work", self.output)

    def test_unknown_account_creates_nothing(self):
        with self.assertRaises(SystemExit), self.capture():
            self.up(workers=["work", "nosuch"], dry_run=False)
        self.assertFalse(self.fleet_dir.exists())
        self.assertEqual(fleet.fleet_branches(self.repo), [])

    def test_existing_session_is_not_stacked_on(self):
        fleet.has_session = lambda name: True
        self.refuses("already exists", workers=["work"])

    def test_a_fleet_needs_a_member(self):
        self.refuses("at least one member")

    def test_a_bad_session_name_is_refused(self):
        self.refuses("invalid session name", workers=["work"], session="has:colon")

    def test_a_non_repository_is_refused(self):
        outside = self.tmp / "not-a-repo"
        outside.mkdir()
        args = argparse.Namespace(session="fleet", repo=str(outside), base=None,
                                  worker=["work"], checker=None, orch="work",
                                  no_orch=False, dry_run=True)
        with self.assertRaises(SystemExit), self.capture():
            fleet.cmd_up(args)
        self.assertIn("not inside a git repository", self.output)


class TestDryRunSequence(Base):
    """The exact commands a mixed claude+codex fleet would run."""

    def setUp(self):
        super().setUp()
        _, self.out = self.up(workers=["work", "cx"], checkers=["client"])
        self.cmds = self.commands(self.out)
        self.tmux = [cmd for cmd in self.cmds if cmd[0] == "/usr/bin/tmux"]

    def spawn_of(self, window):
        """The respawn-pane argv for one window: how that member is launched."""
        for cmd in self.tmux:
            if cmd[1] == "respawn-pane" and cmd[cmd.index("-t") + 1].endswith(
                    ":" + window):
                return cmd
        raise AssertionError(f"no respawn-pane for {window}")

    def test_worktrees_are_created_before_any_window(self):
        self.assertEqual([cmd[0] for cmd in self.cmds][:2], ["git", "git"])
        self.assertEqual(len(self.tmux), len(self.cmds) - 2)

    def test_each_worker_gets_a_branch_and_a_worktree_off_the_base_commit(self):
        self.assertEqual(self.cmds[0], [
            "git", "-C", str(self.repo), "worktree", "add",
            "-b", "fleet/worker-work",
            str(self.fleet_dir / "worker-work"), self.base_sha])
        self.assertEqual(self.cmds[1], [
            "git", "-C", str(self.repo), "worktree", "add",
            "-b", "fleet/worker-cx",
            str(self.fleet_dir / "worker-cx"), self.base_sha])

    def test_checkers_get_no_worktree(self):
        adds = [cmd for cmd in self.cmds if cmd[3:5] == ["worktree", "add"]]
        self.assertEqual(len(adds), 2)
        self.assertNotIn("checker", " ".join(" ".join(a) for a in adds))

    def test_the_orchestrator_owns_the_first_window(self):
        self.assertEqual(self.tmux[0], [
            "/usr/bin/tmux", "new-session", "-d", "-s", "fleet", "-n", "orch",
            "-c", str(self.repo)])

    def test_every_window_is_spawned_in_the_same_three_steps(self):
        verbs = [cmd[1] for cmd in self.tmux]
        self.assertEqual(verbs, ["new-session", "set-option", "respawn-pane"]
                         + ["new-window", "set-option", "respawn-pane"] * 3)

    def test_a_window_is_told_to_survive_its_agent_before_the_agent_starts(self):
        """The ordering is the whole point: an account that dies on startup
        would otherwise take its window, and the error, with it."""
        for i, cmd in enumerate(self.tmux):
            if cmd[1] != "respawn-pane":
                continue
            window = cmd[cmd.index("-t") + 1]
            before = self.tmux[i - 1]
            self.assertEqual(before[1], "set-option")
            self.assertEqual(before[before.index("-t") + 1], window)
            self.assertEqual(before[-2:], ["remain-on-exit", "on"])

    def test_no_member_is_launched_through_a_shell(self):
        """A shell in front of the agent is why `pane_current_command` used to
        report `bash` for every healthy window."""
        for cmd in self.tmux:
            if cmd[1] != "respawn-pane":
                continue
            launcher = cmd[cmd.index("-c") + 2]
            self.assertIn(Path(launcher).name, ("cca", "cxa"))
            self.assertTrue(Path(launcher).is_absolute())
            for arg in cmd:
                self.assertNotIn(";", arg)

    def test_windows_are_targeted_exactly_never_by_prefix(self):
        for cmd in self.tmux:
            wanted = "=fleet" if cmd[1] == "new-window" else None
            got = cmd[cmd.index("-t") + 1] if "-t" in cmd else None
            if wanted:
                self.assertEqual(got, wanted)
            elif got:
                self.assertTrue(got.startswith("=fleet:"), got)

    def test_each_window_is_named_and_started_in_its_own_tree(self):
        made = [(cmd[cmd.index("-n") + 1], cmd[cmd.index("-c") + 1])
                for cmd in self.tmux if cmd[1] in ("new-session", "new-window")]
        self.assertEqual(made, [
            ("orch", str(self.repo)),
            ("worker-work", str(self.fleet_dir / "worker-work")),
            ("worker-cx", str(self.fleet_dir / "worker-cx")),
            ("checker-client", str(self.repo)),
        ])

    def test_the_agent_is_spawned_in_that_same_tree(self):
        for cmd in self.tmux:
            if cmd[1] == "respawn-pane":
                window = cmd[cmd.index("-t") + 1].split(":", 1)[1]
                expected = str(self.fleet_dir / window) \
                    if window.startswith("worker-") else str(self.repo)
                self.assertEqual(cmd[cmd.index("-c") + 1], expected)

    def test_a_codex_account_is_launched_through_cxa_and_claude_through_cca(self):
        self.assertEqual(self.spawn_of("worker-cx")[-2:],
                         [str(self.bin / "cxa"), "cx"])
        self.assertEqual(self.spawn_of("worker-work")[-2:],
                         [str(self.bin / "cca"), "work"])
        self.assertEqual(self.spawn_of("checker-client")[-2:],
                         [str(self.bin / "cca"), "client"])

    def test_the_orchestrator_is_launched_with_one_line_pointing_at_its_brief(self):
        argv = self.spawn_of("orch")
        launcher, account, prompt = argv[-3:]
        self.assertEqual([launcher, account], [str(self.bin / "cca"), "work"])
        self.assertEqual(prompt.splitlines(), [prompt])       # one line, always
        self.assertIn(str(self.fleet_dir / "briefs" / "fleet" / "orch.md"), prompt)

    def test_the_brief_is_the_only_file_ccfleet_writes(self):
        self.assertEqual(self.writes(self.out),
                         [str(self.fleet_dir / "briefs" / "fleet" / "orch.md")])

    def test_workers_start_bare_so_the_orchestrator_briefs_them(self):
        for window in ("worker-work", "worker-cx", "checker-client"):
            cmd = self.spawn_of(window)
            with self.subTest(window=window):
                # launcher + account, and nothing after them.
                self.assertEqual(len(cmd) - cmd.index("-c") - 2, 2)

    def test_dry_run_touches_nothing(self):
        self.assertFalse(self.fleet_dir.exists())
        self.assertEqual(fleet.fleet_branches(self.repo), [])
        self.assertIn("dry run", self.out)

    def test_the_map_is_printed_before_the_commands(self):
        self.assertLess(self.out.index("fleet:worker-work"),
                        self.out.index("worktree add"))


class TestOrchestratorChoice(Base):
    @staticmethod
    def windows(cmds):
        return [c[c.index("-n") + 1] for c in cmds
                if c[1:2] in (["new-session"], ["new-window"])]

    @staticmethod
    def first_spawn(cmds):
        return next(c for c in cmds if c[1:2] == ["respawn-pane"])

    def test_no_orch_promotes_the_first_member_to_the_new_session(self):
        _, out = self.up(workers=["work"], checkers=["client"], no_orch=True)
        cmds = self.commands(out)
        self.assertEqual(self.windows(cmds), ["worker-work", "checker-client"])
        self.assertEqual(self.writes(out), [])       # no orchestrator, no brief

    def test_no_orch_and_an_orch_account_contradict_each_other(self):
        self.refuses("contradict", workers=["work"], orch="client", no_orch=True)

    def test_any_account_can_orchestrate_including_a_codex_one(self):
        _, out = self.up(workers=["work"], orch="cx")
        spawn = self.first_spawn(self.commands(out))
        self.assertEqual(spawn[-3:-1], [str(self.bin / "cxa"), "cx"])

    def test_without_o_it_falls_back_to_the_registry_default(self):
        saved = dict(os.environ)
        for key in ("CCA_ACCOUNT", "CLAUDE_CONFIG_DIR", "CODEX_HOME"):
            os.environ.pop(key, None)
        try:
            _, out = self.up(workers=["work"], orch=None)
        finally:
            os.environ.clear()
            os.environ.update(saved)
        spawn = self.first_spawn(self.commands(out))
        self.assertEqual(spawn[-3:-1], [str(self.bin / "cca"), "work"])


class TestWorktreeReuse(Base):
    def test_a_kept_worktree_is_picked_back_up_not_recreated(self):
        branch, path = self.add_worker_tree("work", dirty=True)
        _, out = self.up(workers=["work"])
        self.assertNotIn("worktree add", out)
        self.assertIn("existing worktree kept", out)
        self.assertTrue((path / "scratch.txt").exists())

    def test_a_branch_without_a_worktree_is_re_attached_not_re_created(self):
        _, path = self.add_worker_tree("work")
        git(self.repo, "worktree", "remove", str(path))
        _, out = self.up(workers=["work"])
        add = next(c for c in self.commands(out) if "add" in c)
        self.assertEqual(add[-2:], [str(path), "fleet/worker-work"])
        self.assertIn("already existed", out)

    def test_a_branch_checked_out_somewhere_else_is_refused(self):
        elsewhere = self.tmp / "elsewhere"
        git(self.repo, "worktree", "add", "-q", "-b", "fleet/worker-work",
            str(elsewhere), self.base_sha)
        self.refuses("already checked out", workers=["work"])
        self.assertIn(str(elsewhere), self.output)

    def test_an_unrelated_directory_in_the_way_is_refused(self):
        (self.fleet_dir / "worker-work").mkdir(parents=True)
        self.refuses("not a worktree", workers=["work"])


class TestRemovalDecision(unittest.TestCase):
    """The safety model, as a table."""

    def row(self, **kw):
        base = {"branch": "fleet/worker-x", "worktree": "/w", "dirty": False,
                "ahead": 0, "merged": True, "in_use": []}
        base.update(kw)
        return base

    def test_clean_and_merged_goes(self):
        self.assertEqual(fleet.removal_decision(self.row(), False),
                         (True, "clean and merged"))

    def test_dirty_is_kept(self):
        ok, why = fleet.removal_decision(self.row(dirty=True), False)
        self.assertFalse(ok)
        self.assertIn("uncommitted", why)

    def test_unmerged_is_kept_and_says_how_much(self):
        ok, why = fleet.removal_decision(self.row(ahead=3, merged=False), False)
        self.assertFalse(ok)
        self.assertIn("3 commits", why)

    def test_force_takes_everything_but_says_what_it_costs(self):
        for kw in ({"dirty": True}, {"ahead": 2, "merged": False},
                   {"dirty": True, "ahead": 2, "merged": False}):
            ok, why = fleet.removal_decision(self.row(**kw), True)
            with self.subTest(**kw):
                self.assertTrue(ok)
                self.assertTrue(why.startswith("forced:"))

    def test_a_live_window_in_it_outranks_clean_and_merged(self):
        ok, why = fleet.removal_decision(self.row(in_use=["s:w"]), False)
        self.assertFalse(ok)
        self.assertEqual(why, "in use by s:w")

    def test_a_branch_without_a_worktree_is_not_downs_business(self):
        self.assertEqual(fleet.removal_decision(self.row(worktree=None), True),
                         (False, "no worktree"))


class TestDown(Base):
    def test_clean_and_merged_worktree_and_branch_both_go(self):
        branch, path = self.add_worker_tree("done")
        _, out = self.down()
        self.assertFalse(path.exists())
        self.assertNotIn(branch, fleet.fleet_branches(self.repo))
        self.assertIn("removed", out)

    def test_uncommitted_changes_are_kept_with_the_reason(self):
        branch, path = self.add_worker_tree("dirty", dirty=True)
        _, out = self.down()
        self.assertTrue((path / "scratch.txt").exists())
        self.assertIn(branch, fleet.fleet_branches(self.repo))
        self.assertIn("uncommitted changes", out)

    def test_unmerged_commits_are_kept_with_the_reason(self):
        branch, path = self.add_worker_tree("ahead", commits=2)
        _, out = self.down()
        self.assertTrue(path.exists())
        self.assertIn(branch, fleet.fleet_branches(self.repo))
        self.assertIn("not merged (2 commits)", out)

    def test_one_dirty_worker_does_not_hold_up_a_finished_one(self):
        _, done = self.add_worker_tree("done")
        _, busy = self.add_worker_tree("busy", commits=1)
        self.down()
        self.assertFalse(done.exists())
        self.assertTrue(busy.exists())

    def test_a_merged_branch_goes_even_when_it_had_commits(self):
        branch, path = self.add_worker_tree("landed", commits=1)
        git(self.repo, "merge", "-q", "--no-ff", "-m", "merge", branch)
        self.down()
        self.assertFalse(path.exists())
        self.assertNotIn(branch, fleet.fleet_branches(self.repo))

    def test_force_drops_worktrees_but_never_unmerged_commits(self):
        branch, path = self.add_worker_tree("ahead", commits=2, dirty=True)
        tip = git(path, "rev-parse", "HEAD")
        _, out = self.down(force=True)
        self.assertFalse(path.exists())
        self.assertIn(branch, fleet.fleet_branches(self.repo))
        self.assertEqual(git(self.repo, "rev-parse", branch), tip)
        self.assertIn("forced:", out)

    def test_dry_run_decides_without_removing(self):
        branch, path = self.add_worker_tree("done")
        _, out = self.down(dry_run=True)
        self.assertTrue(path.exists())
        self.assertIn(branch, fleet.fleet_branches(self.repo))
        self.assertIn("worktree remove", out)
        self.assertIn("dry run", out)

    def test_the_fleet_directory_goes_when_the_last_worktree_does(self):
        self.add_worker_tree("done")
        self.assertTrue(self.fleet_dir.is_dir())
        self.down()
        self.assertFalse(self.fleet_dir.exists())

    def test_a_missing_session_is_reported_not_an_error(self):
        rc, out = self.down()
        self.assertEqual(rc, 0)
        self.assertIn("no tmux session", out)


class TestOccupancy(Base):
    """A worktree with a live window in it is not `down`'s to remove."""

    def test_a_pane_inside_a_worktree_counts_and_a_lookalike_does_not(self):
        tree = self.fleet_dir / "worker-a"
        self.with_panes([
            ("mine:worker-a", str(tree)),
            ("mine:worker-a", str(tree / "webapp" / "backend")),
            ("other:worker-a2", str(self.fleet_dir / "worker-a2")),
            ("elsewhere:0", str(self.repo)),
        ])
        self.assertEqual(fleet.panes_under([tree]), {str(tree): ["mine:worker-a"]})

    def test_an_empty_cwd_from_a_dead_pane_matches_nothing(self):
        tree = self.fleet_dir / "worker-a"
        self.with_panes([("mine:worker-a", "")])
        self.assertEqual(fleet.panes_under([tree]), {str(tree): []})

    def test_a_clean_merged_worktree_is_kept_while_someone_is_in_it(self):
        branch, path = self.add_worker_tree("busy")
        self.with_panes([("otherfleet:worker-busy", str(path))])
        _, out = self.down()
        self.assertTrue(path.exists())
        self.assertIn("in use by otherfleet:worker-busy", out)

    def test_force_takes_it_and_names_who_it_took_it_from(self):
        branch, path = self.add_worker_tree("busy")
        self.with_panes([("otherfleet:worker-busy", str(path))])
        _, out = self.down(force=True)
        self.assertFalse(path.exists())
        self.assertIn("forced: taken from otherfleet:worker-busy", out)

    def test_this_fleets_own_windows_do_not_block_its_own_teardown(self):
        """`down` kills the session first, so by the time occupancy is read
        the only panes left belong to somebody else."""
        branch, path = self.add_worker_tree("done")
        self.with_panes([])                  # the kill already happened
        self.down()
        self.assertFalse(path.exists())


class TestStatus(Base):
    def status(self, as_json=False):
        args = argparse.Namespace(session="fleet", repo=str(self.repo),
                                  base=None, json=as_json)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            fleet.cmd_status(args)
        return buf.getvalue()

    def test_json_reports_every_branch_with_what_removing_it_would_cost(self):
        self.add_worker_tree("clean")
        self.add_worker_tree("ahead", commits=2, dirty=True)
        data = json.loads(self.status(as_json=True))
        rows = {r["branch"]: r for r in data["branches"]}
        self.assertEqual(set(rows), {"fleet/worker-clean", "fleet/worker-ahead"})
        self.assertEqual(rows["fleet/worker-clean"],
                         {"branch": "fleet/worker-clean",
                          "worktree": str(self.fleet_dir / "worker-clean"),
                          "dirty": False, "ahead": 0, "merged": True,
                          "in_use": []})
        ahead = rows["fleet/worker-ahead"]
        self.assertTrue(ahead["dirty"])
        self.assertEqual(ahead["ahead"], 2)
        self.assertFalse(ahead["merged"])
        self.assertFalse(data["session_exists"])
        self.assertEqual(data["repo"], str(self.repo))
        self.assertEqual(data["base"]["sha"], self.base_sha)

    def test_a_branch_whose_worktree_is_gone_is_still_reported(self):
        _, path = self.add_worker_tree("orphan", commits=1)
        git(self.repo, "worktree", "remove", "--force", str(path))
        data = json.loads(self.status(as_json=True))
        row = data["branches"][0]
        self.assertEqual(row["branch"], "fleet/worker-orphan")
        self.assertIsNone(row["worktree"])
        self.assertIsNone(row["dirty"])

    def test_a_dead_member_is_named_dead_with_its_exit_code(self):
        fleet.session_panes = lambda name: [
            {"index": "0", "window": "orch", "command": "claude",
             "pid": "1", "cwd": str(self.repo), "dead": False,
             "exit_status": None},
            {"index": "1", "window": "worker-work", "command": "cca",
             "pid": "2", "cwd": str(self.repo), "dead": True,
             "exit_status": "1"},
        ]
        out = self.status()
        self.assertIn("dead (exit 1)", out)
        self.assertIn("respawn-pane", out)          # how to put it back

    def test_a_live_member_is_named_by_the_agent_it_runs(self):
        fleet.session_panes = lambda name: [
            {"index": "0", "window": "orch", "command": "claude", "pid": "1",
             "cwd": str(self.repo), "dead": False, "exit_status": None}]
        out = self.status()
        self.assertIn("claude", out)
        self.assertNotIn("dead", out)

    def test_text_status_says_when_nothing_is_running(self):
        out = self.status()
        self.assertIn("not running", out)
        self.assertIn("no fleet/ branches", out)

    def test_only_fleet_branches_are_reported(self):
        git(self.repo, "branch", "someone-elses-work")
        self.add_worker_tree("mine")
        data = json.loads(self.status(as_json=True))
        self.assertEqual([r["branch"] for r in data["branches"]],
                         ["fleet/worker-mine"])


class TestRepoResolution(Base):
    def test_a_fleet_run_from_inside_a_worker_worktree_finds_the_main_repo(self):
        _, path = self.add_worker_tree("inside")
        self.assertEqual(fleet.main_worktree(path), self.repo)

    def test_the_fleet_directory_sits_beside_the_repo_not_inside_it(self):
        self.assertEqual(fleet.fleet_dir_for(self.repo), self.fleet_dir)
        self.assertNotIn(str(self.repo), str(self.fleet_dir.relative_to(self.tmp)))

    def test_base_can_be_an_older_commit(self):
        (self.repo / "later.txt").write_text("later\n")
        git(self.repo, "add", "later.txt")
        git(self.repo, "commit", "-q", "-m", "later")
        _, out = self.up(workers=["work"], base=self.base_sha)
        add = next(c for c in self.commands(out) if "add" in c)
        self.assertEqual(add[-1], self.base_sha)

    def test_an_unknown_base_is_refused(self):
        self.refuses("no such commit-ish", workers=["work"], base="no-such-ref")


class TestPaneParsing(unittest.TestCase):
    """tmux output with empty trailing fields, as it really arrives."""

    def panes(self, stdout):
        saved_probe, saved_which = fleet.probe, cca.which
        cca.which = lambda binary: "/usr/bin/tmux"
        # probe() strips its output, which is where the trailing tabs go.
        fleet.probe = lambda argv, cwd=None: (0, stdout.strip())
        try:
            return fleet.session_panes("fleet")
        finally:
            fleet.probe, cca.which = saved_probe, saved_which

    def test_the_last_pane_is_not_lost_to_an_empty_final_field(self):
        rows = self.panes(
            "0\torch\tclaude\t101\t/repo\t0\t\n"
            "1\tworker-probe\tclaude\t102\t/repo-fleet/worker-probe\t0\t\n")
        self.assertEqual([r["window"] for r in rows], ["orch", "worker-probe"])
        self.assertFalse(any(r["dead"] for r in rows))
        self.assertIsNone(rows[-1]["exit_status"])

    def test_a_dead_pane_has_no_cwd_and_keeps_its_exit_status(self):
        rows = self.panes("1\tworker-probe\tcca\t102\t\t1\t127\n")
        self.assertEqual(rows, [{"index": "1", "window": "worker-probe",
                                 "command": "cca", "pid": "102", "cwd": "",
                                 "dead": True, "exit_status": "127"}])


class TestMemberArgv(unittest.TestCase):
    def test_a_prompt_stays_one_argument_whatever_is_in_it(self):
        prompt = "read 'this' and \"that\"; rm -rf /"
        self.assertEqual(
            fleet.member_argv({"launcher": "/bin/cca", "account": "work",
                               "prompt": prompt}),
            ["/bin/cca", "work", prompt])

    def test_no_prompt_means_no_third_argument(self):
        self.assertEqual(
            fleet.member_argv({"launcher": "/bin/cca", "account": "work"}),
            ["/bin/cca", "work"])


class TestManual(unittest.TestCase):
    def test_the_manual_the_orchestrator_is_pointed_at_exists(self):
        self.assertTrue(fleet.MANUAL.is_file(), f"{fleet.MANUAL} is missing")

    def test_it_documents_both_channels(self):
        text = fleet.MANUAL.read_text()
        for needle in ("ListAgents", "SendMessage", "send-keys", "capture-pane",
                       "ccfleet status", "ccfleet down"):
            self.assertIn(needle, text)


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2)
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
