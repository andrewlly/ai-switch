#!/usr/bin/env python3
"""Hermetic tests for cca.

Runs against a temporary CCA_HOME and never touches the real registry, the
real spaces, or any real account. Run directly:

    python3 ~/.local/share/cc-accounts/test_cca.py
"""

import argparse
import base64
import contextlib
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent

# CCA_HOME is read at import time, so it must be set before loading the module.
_TMP = Path(tempfile.mkdtemp(prefix="cca-test-"))
os.environ["CCA_HOME"] = str(_TMP / "accounts-home")

_spec = importlib.util.spec_from_file_location("cca", HERE / "cca.py")
cca = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cca)


def fake_jwt(claims: dict) -> str:
    """A signature-less JWT; cca only ever decodes the payload."""
    body = base64.urlsafe_b64encode(
        json.dumps(claims).encode()).decode().rstrip("=")
    return f"header.{body}.signature"


def make_registry(tmp: Path) -> dict:
    """One legacy-default Claude account, one normal Claude space, one Codex."""
    legacy = tmp / "legacy-space"
    normal = tmp / "normal-space"
    codex = tmp / "codex-space"
    for d in (legacy, normal, codex):
        d.mkdir(parents=True, exist_ok=True)

    (normal / ".claude.json").write_text(
        json.dumps({"oauthAccount": {"emailAddress": "n@example.com",
                                     "organizationName": "NormalOrg"},
                    "projects": {"a": {}}}))
    (codex / "auth.json").write_text(json.dumps({
        "auth_mode": "chatgpt",
        "tokens": {
            "access_token": "xxx",
            "id_token": fake_jwt({
                "email": "cx@example.com",
                "exp": 4102444800,           # 2100-01-01, comfortably future
                "https://api.openai.com/auth": {
                    "chatgpt_plan_type": "plus",
                    "organizations": [
                        {"id": "org-1", "is_default": False, "title": "Other"},
                        {"id": "org-2", "is_default": True, "title": "Personal"},
                    ],
                },
            }),
        },
    }))

    return {
        "version": 3,
        "defaults": {"claude": "legacy", "codex": "cx"},
        "accounts": {
            "legacy": {"tool": "claude", "config_dir": str(legacy),
                       "legacy_default": True, "description": "original install",
                       "env": {}, "created": ""},
            "normal": {"tool": "claude", "config_dir": str(normal),
                       "legacy_default": False, "description": "n@example.com",
                       "env": {"FOO": "bar"}, "created": ""},
            "cx": {"tool": "codex", "config_dir": str(codex),
                   "legacy_default": False, "description": "cx@example.com",
                   "env": {}, "created": ""},
        },
    }


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cca-case-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        cca.ROOT = self.tmp / "home"
        cca.REGISTRY = cca.ROOT / "accounts.json"
        cca.SPACES = cca.ROOT / "spaces"

        # Point every real-home reference at a fake home, so account detection
        # can never see - or adopt - the operator's actual accounts.
        claude, codex = cca.TOOLS["claude"], cca.TOOLS["codex"]
        self._saved = {
            "HOME": cca.HOME, "LEGACY_DIR": cca.LEGACY_DIR,
            "LEGACY_CONFIG": cca.LEGACY_CONFIG, "CODEX_DIR": cca.CODEX_DIR,
            "BIN_DIR": cca.BIN_DIR,
            "claude_dir": claude.default_dir, "codex_dir": codex.default_dir,
        }
        self.addCleanup(self._restore)
        fake = self.tmp / "home-root"
        fake.mkdir(parents=True, exist_ok=True)
        cca.HOME = fake
        cca.LEGACY_DIR = claude.default_dir = fake / ".claude"
        cca.LEGACY_CONFIG = fake / ".claude.json"
        cca.CODEX_DIR = codex.default_dir = fake / ".codex"
        # Shortcut symlinks are written to BIN_DIR on every save. Without this
        # the suite would rewrite - and prune - the operator's real ~/.local/bin.
        cca.BIN_DIR = self.tmp / "bin"
        cca.BIN_DIR.mkdir(parents=True, exist_ok=True)

        cca.write_json_atomic(cca.REGISTRY, make_registry(self.tmp))
        self.reg = cca.Registry.load()

    def _restore(self):
        cca.HOME = self._saved["HOME"]
        cca.LEGACY_DIR = self._saved["LEGACY_DIR"]
        cca.LEGACY_CONFIG = self._saved["LEGACY_CONFIG"]
        cca.CODEX_DIR = self._saved["CODEX_DIR"]
        cca.BIN_DIR = self._saved["BIN_DIR"]
        cca.TOOLS["claude"].default_dir = self._saved["claude_dir"]
        cca.TOOLS["codex"].default_dir = self._saved["codex_dir"]

    def run_cmd(self, func, **kwargs) -> str:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            func(argparse.Namespace(**kwargs))
        return buf.getvalue()


class TestLaunchEnv(Base):
    """The invariant the whole tool exists for."""

    def test_legacy_account_unsets_config_dir(self):
        env = self.reg.get("legacy").launch_env(
            base={"CLAUDE_CONFIG_DIR": "/somewhere/else", "PATH": "/usr/bin"})
        self.assertNotIn("CLAUDE_CONFIG_DIR", env)
        self.assertEqual(env["CCA_ACCOUNT"], "legacy")

    def test_normal_account_sets_config_dir(self):
        acct = self.reg.get("normal")
        env = acct.launch_env(base={"PATH": "/usr/bin"})
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], str(acct.config_dir))
        self.assertEqual(env["CCA_ACCOUNT"], "normal")

    def test_per_account_env_is_applied(self):
        env = self.reg.get("normal").launch_env(base={"PATH": "/usr/bin"})
        self.assertEqual(env["FOO"], "bar")

    def test_calling_session_env_is_stripped(self):
        base = {"PATH": "/usr/bin", "CLAUDECODE": "1",
                "CLAUDE_CODE_SESSION_ID": "abc", "CLAUDE_PID": "42"}
        env = self.reg.get("normal").launch_env(base=base)
        for key in ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CLAUDE_PID"):
            self.assertNotIn(key, env)

    def test_keep_env_preserves_session_vars(self):
        env = self.reg.get("normal").launch_env(
            base={"PATH": "/usr/bin", "CLAUDECODE": "1"}, clean=False)
        self.assertEqual(env["CLAUDECODE"], "1")


class TestConfigFileResolution(Base):
    def test_legacy_config_is_home_root(self):
        self.assertEqual(self.reg.get("legacy").config_file, cca.LEGACY_CONFIG)

    def test_normal_config_is_inside_the_space(self):
        acct = self.reg.get("normal")
        self.assertEqual(acct.config_file, acct.config_dir / ".claude.json")

    def test_identity_reads_from_the_right_file(self):
        ident = self.reg.get("normal").identity()
        self.assertEqual(ident["email"], "n@example.com")
        self.assertEqual(ident["org"], "NormalOrg")
        self.assertEqual(ident["projects"], 1)


class TestLifecycle(Base):
    def test_add_creates_a_locked_down_space(self):
        self.run_cmd(cca.cmd_add, name="fresh", dir=None, description="d",
                     login=False, alias=None, tool=None, entry='cca', entry_tool='claude')
        reg = cca.Registry.load()
        acct = reg.get("fresh")
        self.assertTrue(acct.config_dir.is_dir())
        self.assertEqual(os.stat(acct.config_dir).st_mode & 0o777, 0o700)
        self.assertFalse(acct.legacy_default)
        self.assertEqual(acct.tool_name, "claude")   # the default tool

    def test_add_rejects_invalid_names(self):
        for bad in ("has space", "../escape", "-leading", ""):
            with self.assertRaises(SystemExit, msg=bad):
                self.run_cmd(cca.cmd_add, name=bad, dir=None, description=None,
                             login=False, alias=None, tool=None, entry='cca', entry_tool='claude')

    def test_add_rejects_subcommand_names(self):
        with self.assertRaises(SystemExit):
            self.run_cmd(cca.cmd_add, name="status", dir=None, description=None,
                         login=False, alias=None, tool=None, entry='cca', entry_tool='claude')

    def test_add_rejects_a_duplicate_space(self):
        with self.assertRaises(SystemExit):
            self.run_cmd(cca.cmd_add, name="clash",
                         dir=str(self.reg.get("normal").config_dir),
                         description=None, login=False, alias=None, tool=None, entry='cca', entry_tool='claude')

    def test_add_refuses_the_legacy_directory(self):
        with self.assertRaises(SystemExit):
            self.run_cmd(cca.cmd_add, name="trap", dir=str(cca.LEGACY_DIR),
                         description=None, login=False, alias=None, tool=None, entry='cca', entry_tool='claude')

    def test_rename_keeps_the_space(self):
        before = self.reg.get("normal").config_dir
        self.run_cmd(cca.cmd_rename, old="normal", new="renamed")
        reg = cca.Registry.load()
        self.assertEqual(reg.get("renamed").config_dir, before)
        self.assertNotIn("normal", reg.accounts)

    def test_rm_without_purge_keeps_the_space(self):
        path = self.reg.get("normal").config_dir
        self.run_cmd(cca.cmd_rm, name="normal", purge=False, yes=True)
        self.assertTrue(path.is_dir())
        self.assertNotIn("normal", cca.Registry.load().accounts)

    def test_purge_deletes_the_space(self):
        path = self.reg.get("normal").config_dir
        self.run_cmd(cca.cmd_rm, name="normal", purge=True, yes=True)
        self.assertFalse(path.exists())

    def test_purge_refuses_the_legacy_account(self):
        path = self.reg.get("legacy").config_dir
        with self.assertRaises(SystemExit):
            self.run_cmd(cca.cmd_rm, name="legacy", purge=True, yes=True)
        self.assertTrue(path.is_dir())

    def test_default_follows_a_removal(self):
        self.run_cmd(cca.cmd_rm, name="legacy", purge=False, yes=True)
        # falls back to the alphabetically first survivor of {cx, normal}
        self.assertEqual(cca.Registry.load().default_for("codex"), "cx")


class TestLinking(Base):
    def test_link_and_unlink_a_shareable_asset(self):
        src = self.reg.get("legacy").config_dir / "skills"
        src.mkdir()
        self.run_cmd(cca.cmd_link, name="normal", asset="skills",
                     source="legacy", force=False)
        dst = self.reg.get("normal").config_dir / "skills"
        self.assertTrue(dst.is_symlink())
        self.assertEqual(dst.resolve(), src.resolve())

        self.run_cmd(cca.cmd_unlink, name="normal", asset="skills")
        self.assertFalse(dst.exists())

    def test_link_refuses_credentials(self):
        with self.assertRaises(SystemExit):
            self.run_cmd(cca.cmd_link, name="normal", asset=".credentials.json",
                         source="legacy", force=False)

    def test_link_refuses_to_clobber_real_data(self):
        (self.reg.get("legacy").config_dir / "skills").mkdir()
        real = self.reg.get("normal").config_dir / "skills"
        real.mkdir()
        (real / "keep.md").write_text("important")
        with self.assertRaises(SystemExit):
            self.run_cmd(cca.cmd_link, name="normal", asset="skills",
                         source="legacy", force=False)
        self.assertTrue((real / "keep.md").exists())

    def test_link_refuses_self(self):
        with self.assertRaises(SystemExit):
            self.run_cmd(cca.cmd_link, name="normal", asset="skills",
                         source="normal", force=False)


class TestResolution(Base):
    def test_marker_wins(self):
        os.environ["CCA_ACCOUNT"] = "normal"
        self.addCleanup(os.environ.pop, "CCA_ACCOUNT", None)
        self.assertEqual(cca.current_account_name(self.reg), "normal")

    def test_config_dir_is_matched(self):
        os.environ.pop("CCA_ACCOUNT", None)
        os.environ["CLAUDE_CONFIG_DIR"] = str(self.reg.get("normal").config_dir)
        self.addCleanup(os.environ.pop, "CLAUDE_CONFIG_DIR", None)
        self.assertEqual(cca.current_account_name(self.reg), "normal")

    def test_unset_means_the_legacy_account(self):
        os.environ.pop("CCA_ACCOUNT", None)
        os.environ.pop("CLAUDE_CONFIG_DIR", None)
        self.assertEqual(cca.current_account_name(self.reg), "legacy")


class TestCodex(Base):
    """Codex uses a different variable, a different token file, and - crucially -
    has no legacy-default quirk: CODEX_HOME=~/.codex selects the real account."""

    def test_codex_uses_its_own_config_variable(self):
        acct = self.reg.get("cx")
        env = acct.launch_env(base={"PATH": "/usr/bin"})
        self.assertEqual(env["CODEX_HOME"], str(acct.config_dir))
        self.assertNotIn("CLAUDE_CONFIG_DIR", env)
        self.assertEqual(env["CCA_TOOL"], "codex")

    def test_claude_account_does_not_set_codex_home(self):
        env = self.reg.get("normal").launch_env(base={"PATH": "/usr/bin"})
        self.assertNotIn("CODEX_HOME", env)
        self.assertEqual(env["CCA_TOOL"], "claude")

    def test_codex_has_no_legacy_quirk(self):
        self.assertFalse(cca.TOOLS["codex"].supports_legacy_default)
        self.assertTrue(cca.TOOLS["claude"].supports_legacy_default)

    def test_identity_comes_from_the_jwt(self):
        ident = self.reg.get("cx").identity()
        self.assertEqual(ident["email"], "cx@example.com")
        self.assertEqual(ident["plan"], "plus")
        self.assertEqual(ident["org"], "Personal")     # the is_default org
        self.assertTrue(ident["logged_in"])

    def test_expired_id_token_is_not_reported_as_expired(self):
        """Codex renews via refresh_token, so a stale id_token means nothing.

        Reporting `exp` as an expiry produced a false "token-expired" state on
        a session Codex itself considered perfectly live.
        """
        space = self.tmp / "stale-codex"
        space.mkdir()
        (space / "auth.json").write_text(json.dumps({
            "auth_mode": "chatgpt",
            "last_refresh": "2026-08-06T21:36:53.311651469Z",  # 9-digit fraction
            "tokens": {"access_token": "xxx", "refresh_token": "yyy",
                       "id_token": fake_jwt({"email": "old@example.com",
                                             "exp": 1000000000})},  # 2001
        }))
        ident = cca.Account("stale", {"tool": "codex",
                                      "config_dir": str(space)}).identity()
        self.assertTrue(ident["logged_in"])
        self.assertIsNone(ident["expires_at"])
        self.assertIsNotNone(ident["refreshed_at"])

    def test_parse_iso_handles_nanosecond_fractions(self):
        self.assertIsNotNone(cca.parse_iso("2026-08-06T21:36:53.311651469Z"))
        self.assertIsNotNone(cca.parse_iso("2026-08-06T21:36:53Z"))
        self.assertIsNone(cca.parse_iso(None))
        self.assertIsNone(cca.parse_iso("not a date"))

    def test_credentials_and_config_paths(self):
        acct = self.reg.get("cx")
        self.assertEqual(acct.credentials_file, acct.config_dir / "auth.json")
        self.assertEqual(acct.config_file, acct.config_dir / "config.toml")
        # config.toml is optional for Codex, so its absence is not a fault
        self.assertFalse(acct.tool.requires_config_file)

    def test_logged_out_codex_space(self):
        empty = self.tmp / "empty-codex"
        empty.mkdir()
        acct = cca.Account("e", {"tool": "codex", "config_dir": str(empty)})
        ident = acct.identity()
        self.assertFalse(ident["logged_in"])
        self.assertIsNone(ident["email"])

    def test_add_a_codex_account(self):
        self.run_cmd(cca.cmd_add, name="cx2", dir=None, description=None,
                     login=False, alias=None, tool='codex', entry='cxa', entry_tool='codex')
        acct = cca.Registry.load().get("cx2")
        self.assertEqual(acct.tool_name, "codex")
        self.assertEqual(acct.tool.config_env, "CODEX_HOME")

    def test_add_rejects_an_unknown_tool(self):
        with self.assertRaises(SystemExit):
            self.run_cmd(cca.cmd_add, name="nope", dir=None, description=None,
                         login=False, alias=None, tool='cursor', entry='cca', entry_tool='claude')

    def test_link_refuses_to_cross_tools(self):
        (self.reg.get("normal").config_dir / "skills").mkdir()
        with self.assertRaises(SystemExit):
            self.run_cmd(cca.cmd_link, name="cx", asset="skills",
                         source="normal", force=False)

    def test_link_uses_the_tools_own_asset_list(self):
        # "rules" is a Codex asset, not a Claude one
        self.assertIn("rules", cca.TOOLS["codex"].linkable)
        self.assertNotIn("rules", cca.TOOLS["claude"].linkable)
        self.assertIn("agents", cca.TOOLS["claude"].linkable)
        self.assertNotIn("agents", cca.TOOLS["codex"].linkable)

    def test_codex_space_is_resolved_from_codex_home(self):
        os.environ.pop("CCA_ACCOUNT", None)
        os.environ.pop("CLAUDE_CONFIG_DIR", None)
        os.environ["CODEX_HOME"] = str(self.reg.get("cx").config_dir)
        self.addCleanup(os.environ.pop, "CODEX_HOME", None)
        self.assertEqual(cca.current_account_name(self.reg), "cx")


class TestRegistryUpgrade(Base):
    def test_v1_entries_default_to_claude(self):
        """A registry written before Codex support must keep working."""
        cca.write_json_atomic(cca.REGISTRY, {
            "version": 1,
            "default": "old",
            "accounts": {
                "old": {"config_dir": str(self.tmp / "old")},
            },
        })
        acct = cca.Registry.load().get("old")
        self.assertEqual(acct.tool_name, "claude")
        self.assertEqual(acct.tool.config_env, "CLAUDE_CONFIG_DIR")


class TestJSONExtraction(unittest.TestCase):
    def test_pulls_json_out_of_noisy_output(self):
        noisy = ('Claude configuration file not found at: /x/.claude.json\n'
                 'You can restore it by running: cp "{a}" "{b}"\n\n'
                 '{"loggedIn": true, "email": "e@x.com"}\n')
        self.assertEqual(cca.extract_json(noisy),
                         {"loggedIn": True, "email": "e@x.com"})

    def test_returns_none_when_there_is_no_json(self):
        self.assertIsNone(cca.extract_json("command not found\n"))



class TestEntryPoints(Base):
    """`cca` drives claude, `cxa` drives codex. One script, two names."""

    def _capture_run(self):
        """Swap cmd_run for a recorder so nothing is ever exec'd."""
        seen = {}

        def fake_run(args):
            seen["name"] = args.name
            seen["entry"] = args.entry
            seen["entry_tool"] = args.entry_tool
            return 0

        real = cca.cmd_run
        cca.cmd_run = fake_run
        self.addCleanup(lambda: setattr(cca, "cmd_run", real))
        return seen

    def test_bare_cca_launches_the_claude_default(self):
        seen = self._capture_run()
        with contextlib.redirect_stdout(io.StringIO()):
            cca.main([], entry="cca")
        self.assertEqual(seen["name"], "legacy")
        self.assertEqual(seen["entry_tool"], "claude")

    def test_bare_cxa_launches_the_codex_default(self):
        seen = self._capture_run()
        with contextlib.redirect_stdout(io.StringIO()):
            cca.main([], entry="cxa")
        self.assertEqual(seen["name"], "cx")
        self.assertEqual(seen["entry_tool"], "codex")

    def test_bare_name_is_rewritten_to_run(self):
        seen = self._capture_run()
        with contextlib.redirect_stdout(io.StringIO()):
            cca.main(["normal", "-c"], entry="cca")
        self.assertEqual(seen["name"], "normal")

    def test_cca_refuses_a_codex_account(self):
        with self.assertRaises(SystemExit):
            self.run_cmd(cca.cmd_run, name="cx", dry_run=True, keep_env=False,
                         tool_args=[], entry="cca", entry_tool="claude")

    def test_cxa_refuses_a_claude_account(self):
        with self.assertRaises(SystemExit):
            self.run_cmd(cca.cmd_run, name="normal", dry_run=True, keep_env=False,
                         tool_args=[], entry="cxa", entry_tool="codex")

    def test_add_follows_the_entry_point(self):
        self.run_cmd(cca.cmd_add, name="viacxa", dir=None, description=None,
                     login=False, alias=None, tool=None, entry="cxa", entry_tool="codex")
        self.assertEqual(cca.Registry.load().get("viacxa").tool_name, "codex")

    def test_explicit_tool_overrides_the_entry_point(self):
        self.run_cmd(cca.cmd_add, name="override", dir=None, description=None,
                     login=False, alias=None, tool="claude", entry="cxa", entry_tool="codex")
        self.assertEqual(cca.Registry.load().get("override").tool_name, "claude")

    def test_entry_for_maps_both_ways(self):
        self.assertEqual(cca.entry_for("claude"), "cca")
        self.assertEqual(cca.entry_for("codex"), "cxa")


class TestAliases(Base):
    """Short per-account commands: cc/cx + a letter, as symlinks to the script."""

    def test_every_account_gets_a_prefixed_shortcut(self):
        reg = cca.Registry.load()
        self.assertTrue(reg.get("legacy").alias.startswith("cc"))
        self.assertTrue(reg.get("normal").alias.startswith("cc"))
        self.assertTrue(reg.get("cx").alias.startswith("cx"))

    def test_shortcut_uses_a_letter_from_the_name(self):
        alias = cca.derive_alias("client", "claude", set())
        self.assertEqual(alias, "ccc")

    def test_collision_falls_through_to_the_next_letter(self):
        first = cca.derive_alias("client", "claude", set())
        second = cca.derive_alias("carol", "claude", {first})
        self.assertNotEqual(first, second)
        self.assertTrue(second.startswith("cc"))

    def test_entry_point_names_are_never_taken(self):
        # "adam" would want "cca", which is the claude entry point
        self.assertNotIn(cca.derive_alias("adam", "claude", set()),
                         cca.RESERVED_ALIASES)

    def test_symlinks_are_created_in_bin_dir(self):
        reg = cca.Registry.load()
        for acct in reg.accounts.values():
            link = cca.BIN_DIR / acct.alias
            self.assertTrue(link.is_symlink(), acct.alias)
            self.assertEqual(Path(os.readlink(link)).resolve(), cca.SCRIPT)

    def test_removing_an_account_removes_its_shortcut(self):
        alias = cca.Registry.load().get("normal").alias
        self.assertTrue((cca.BIN_DIR / alias).is_symlink())
        self.run_cmd(cca.cmd_rm, name="normal", purge=False, yes=True)
        self.assertFalse((cca.BIN_DIR / alias).exists())

    def test_a_foreign_file_is_never_clobbered_or_removed(self):
        intruder = cca.BIN_DIR / "ccz"
        intruder.write_text("#!/bin/sh\necho not ours\n")
        reg = cca.Registry.load()
        reg.sync_alias_links()
        self.assertTrue(intruder.exists())
        self.assertEqual(intruder.read_text(), "#!/bin/sh\necho not ours\n")

    def test_invoking_by_alias_launches_that_account(self):
        seen = {}

        def fake_run(args):
            seen.update(name=args.name, tool_args=list(args.tool_args))
            return 0

        real = cca.cmd_run
        cca.cmd_run = fake_run
        self.addCleanup(lambda: setattr(cca, "cmd_run", real))

        alias = cca.Registry.load().get("normal").alias
        with contextlib.redirect_stdout(io.StringIO()):
            cca.main(["-c", "--model", "opus"], entry=alias)
        self.assertEqual(seen["name"], "normal")
        # every argument belongs to the agent, none to cca
        self.assertEqual(seen["tool_args"], ["-c", "--model", "opus"])

    def test_a_new_account_gets_a_shortcut(self):
        """Regression: `add` stored "" (= deliberately none), so newly created
        accounts silently never got a shortcut at all."""
        self.run_cmd(cca.cmd_add, name="demo", dir=None, description=None,
                     login=False, alias=None, tool=None,
                     entry="cca", entry_tool="claude")
        acct = cca.Registry.load().get("demo")
        self.assertEqual(acct.alias, "ccd")           # first letter of the name
        self.assertEqual(acct.launch_cmd, "ccd")
        self.assertTrue((cca.BIN_DIR / "ccd").is_symlink())

    def test_doctor_reports_an_account_with_no_shortcut(self):
        """Nothing else ever revisits a cleared/failed alias, so doctor must say so."""
        self.run_cmd(cca.cmd_alias, name="normal", set="-", repair=False)
        out = self.run_cmd(cca.cmd_doctor, entry="cca", entry_tool="claude")
        self.assertIn("no shortcut command", out)

    def test_explicit_alias_is_honoured(self):
        self.run_cmd(cca.cmd_add, name="zed", dir=None, description=None,
                     login=False, alias="ccq", tool=None,
                     entry="cca", entry_tool="claude")
        self.assertEqual(cca.Registry.load().get("zed").alias, "ccq")

    def test_explicit_alias_cannot_take_an_entry_point(self):
        with self.assertRaises(SystemExit):
            self.run_cmd(cca.cmd_add, name="zed", dir=None, description=None,
                         login=False, alias="cca", tool=None,
                         entry="cca", entry_tool="claude")

    def test_cleared_shortcut_stays_cleared(self):
        self.run_cmd(cca.cmd_alias, name="normal", set="-", repair=False)
        self.assertEqual(cca.Registry.load().get("normal").alias, "")
        # a later load must not silently re-assign one
        cca.Registry.load()
        self.assertEqual(cca.Registry.load().get("normal").alias, "")

    def test_repair_regenerates_everything(self):
        self.run_cmd(cca.cmd_alias, name="normal", set="-", repair=False)
        self.run_cmd(cca.cmd_alias, name=None, set=None, repair=True)
        self.assertTrue(cca.Registry.load().get("normal").alias)


class TestDefaults(Base):
    def test_a_lone_account_needs_no_configuration(self):
        reg = cca.Registry.load()
        reg.defaults = {}                      # nothing chosen at all
        self.assertEqual(reg.default_for("codex"), "cx")   # the only codex one

    def test_ambiguous_tool_has_no_implicit_default(self):
        reg = cca.Registry.load()
        reg.defaults = {}
        self.assertIsNone(reg.default_for("claude"))       # legacy + normal

    def test_explicit_default_wins(self):
        self.run_cmd(cca.cmd_default, name="normal")
        self.assertEqual(cca.Registry.load().default_for("claude"), "normal")

    def test_removing_the_default_clears_it(self):
        self.run_cmd(cca.cmd_rm, name="legacy", purge=False, yes=True)
        reg = cca.Registry.load()
        self.assertEqual(reg.default_for("claude"), "normal")  # now the only one


class TestNoDuplicateAdoption(Base):
    def test_v2_registry_is_not_re_detected(self):
        """A v2->v3 upgrade must not adopt ~/.codex a second time.

        Detection dedupes by account *name*, so re-running it on a registry
        that already had a codex account produced a second entry pointing at
        the same directory - two accounts fighting over one space.
        """
        codex_dir = cca.CODEX_DIR
        codex_dir.mkdir(parents=True, exist_ok=True)
        (codex_dir / "auth.json").write_text(json.dumps(
            {"auth_mode": "chatgpt", "tokens": {"access_token": "x"}}))

        cca.write_json_atomic(cca.REGISTRY, {
            "version": 2,
            "default": "codex",
            "accounts": {
                "codex": {"tool": "codex", "config_dir": str(codex_dir),
                          "legacy_default": False, "env": {}, "created": ""},
            },
        })
        reg = cca.Registry.load()
        self.assertEqual(list(reg.accounts), ["codex"])
        dirs = [a.config_dir for a in reg.accounts.values()]
        self.assertEqual(len(dirs), len(set(dirs)))

    def test_v1_registry_still_adopts_codex(self):
        codex_dir = cca.CODEX_DIR
        codex_dir.mkdir(parents=True, exist_ok=True)
        (codex_dir / "auth.json").write_text(json.dumps(
            {"auth_mode": "chatgpt", "tokens": {"access_token": "x"}}))

        cca.write_json_atomic(cca.REGISTRY, {
            "version": 1,
            "default": "work",
            "accounts": {"work": {"config_dir": str(self.tmp / "w")}},
        })
        reg = cca.Registry.load()
        self.assertIn("codex", reg.accounts)
        self.assertEqual(reg.accounts["codex"].tool_name, "codex")


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2)
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
