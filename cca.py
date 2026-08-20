#!/usr/bin/env python3
"""cca / cxa - coding-agent account manager.

Runs any number of Claude Code and Codex accounts side by side, each in its
own config directory ("space"): separate credentials, projects, history,
sessions, skills and settings.

One script, two entry points. The name it is invoked as picks the agent:

    cca [account] [args]   ->  claude   (CLAUDE_CONFIG_DIR, default ~/.claude)
    cxa [account] [args]   ->  codex    (CODEX_HOME,        default ~/.codex)

With no account, each entry launches its own tool's default account, so `cca`
and `cxa` are the only two commands needed day to day. Management subcommands
(list, add, status, doctor, ...) work identically from either entry and act on
the whole registry.

Codex is symmetric - CODEX_HOME=~/.codex selects the original account
correctly. Claude is not: the account Claude Code installs first keeps its
main config at ~/.claude.json (home root) rather than inside ~/.claude, so
passing CLAUDE_CONFIG_DIR=~/.claude breaks it - Claude looks for
~/.claude/.claude.json, finds nothing, and comes up amnesiac. Such an account
is flagged `legacy_default` and is launched with the variable *unset* instead.

Stdlib only. No configuration required: first run adopts whatever is already
on the machine.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HOME = Path.home()
ROOT = Path(os.environ.get("CCA_HOME", HOME / ".claude-accounts"))
REGISTRY = ROOT / "accounts.json"
SPACES = ROOT / "spaces"

LEGACY_DIR = HOME / ".claude"
LEGACY_CONFIG = HOME / ".claude.json"
CODEX_DIR = HOME / ".codex"

REGISTRY_VERSION = 3
NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$")

# The command name -> which agent it drives. Both are symlinks to this file.
ENTRYPOINTS = {"cca": "claude", "cxa": "codex"}
BIN_DIR = HOME / ".local" / "bin"
SCRIPT = Path(__file__).resolve()

# Per-account shortcuts are symlinks to this same script, named <prefix><letter>:
# ccw -> work, ccp -> personal, ccc -> client, cxc -> a codex account called
# codex. The entry-point names themselves are reserved.
ALIAS_PREFIX = {"claude": "cc", "codex": "cx"}
RESERVED_ALIASES = set(ENTRYPOINTS)

# Env that identifies the *calling* agent session. A launched account must not
# inherit it or the child mistakes itself for a nested session.
SESSION_ENV = (
    "CLAUDECODE",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_MESSAGING_SOCKET",
    "CLAUDE_CODE_MESSAGING_TOKEN",
    "CLAUDE_CODE_SSE_PORT",
    "CLAUDE_PID",
    "CLAUDE_EFFORT",
    "CODEX_SANDBOX",
    "CODEX_SANDBOX_NETWORK_DISABLED",
)


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def _use_color() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    return sys.stdout.isatty()


class C:
    """Lazily-resolved ANSI codes; empty strings when not a tty."""

    def __getattr__(self, name: str) -> str:
        codes = {
            "dim": "\033[2m", "bold": "\033[1m", "red": "\033[31m",
            "green": "\033[32m", "yellow": "\033[33m", "blue": "\033[34m",
            "cyan": "\033[36m", "reset": "\033[0m",
        }
        return codes.get(name, "") if _use_color() else ""


c = C()


def die(msg: str, code: int = 1):
    print(f"{c.red}error:{c.reset} {msg}", file=sys.stderr)
    sys.exit(code)


def warn(msg: str) -> None:
    print(f"{c.yellow}warning:{c.reset} {msg}", file=sys.stderr)


def info(msg: str) -> None:
    print(f"{c.green}ok:{c.reset} {msg}")


def read_json(path: Path) -> dict | None:
    try:
        with open(path, "rb") as fh:
            return json.loads(fh.read().decode("utf-8", "replace"))
    except (OSError, ValueError):
        return None


def write_json_atomic(path: Path, data: dict, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def rel_time(ts: float) -> str:
    """Human delta for an epoch-seconds timestamp, past or future."""
    delta = ts - time.time()
    future = delta > 0
    delta = abs(delta)
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if delta >= size:
            n = int(delta // size)
            return f"in {n}{unit}" if future else f"{n}{unit} ago"
    return "in <1m" if future else "just now"


def human_bytes(n: float) -> str:
    for unit in ("B", "K", "M", "G", "T"):
        if n < 1024 or unit == "T":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024.0
    return f"{n:.1f}T"


def extract_json(text: str) -> dict | None:
    """Pull the last JSON object out of noisy CLI output.

    `claude auth status` prints recovery advice before its JSON when a config
    file is missing, so a plain json.loads of the whole stream fails.
    """
    depth, start = 0, None
    best = None
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth:
            depth -= 1
            if depth == 0 and start is not None:
                try:
                    best = json.loads(text[start:i + 1])
                except ValueError:
                    pass
    return best


def parse_iso(text: str | None) -> float | None:
    """Epoch seconds from an ISO-8601 string, or None.

    Tolerates the 9-digit fractional seconds Codex writes, which
    datetime.fromisoformat rejects (it accepts only 3 or 6).
    """
    if not text:
        return None
    cleaned = re.sub(r"\.(\d{6})\d+", r".\1", text.strip().replace("Z", "+00:00"))
    try:
        return datetime.fromisoformat(cleaned).timestamp()
    except ValueError:
        return None


def jwt_claims(token: str) -> dict:
    """Decode a JWT payload. Signature is not checked - this is display data."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except (AttributeError, IndexError, ValueError, TypeError):
        return {}


def dir_size(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path, onerror=lambda e: None):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return total


def tilde(path: Path | str) -> str:
    s = str(path)
    return s.replace(str(HOME), "~", 1) if s.startswith(str(HOME)) else s


def sibling(name: str):
    """Load `<name>.py` from beside this script.

    cca is one script plus two modules in one directory, reached through
    symlinks from ~/.local/bin. Importing by name would depend on which of
    those paths Python decided sys.path[0] was, so the path is spelled out.
    """
    import importlib.util
    path = SCRIPT.parent / f"{name}.py"
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location(f"cca_{name}", path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def which(binary: str) -> str | None:
    return shutil.which(binary)


def entry_for(tool_name: str) -> str:
    """The command a user types to drive this tool."""
    for entry, tool in ENTRYPOINTS.items():
        if tool == tool_name:
            return entry
    return "cca"


def owned_symlink(path: Path) -> bool:
    """True if `path` is a shortcut this tool created (and may remove)."""
    try:
        return path.is_symlink() and Path(os.readlink(path)).resolve() == SCRIPT
    except OSError:
        return False


def alias_available(candidate: str, taken: set[str]) -> bool:
    if candidate in RESERVED_ALIASES or candidate in taken:
        return False
    found = which(candidate)
    # Our own leftover symlink is fair game; anything else on PATH is not.
    return not found or owned_symlink(Path(found))


def derive_alias(name: str, tool_name: str, taken: set[str]) -> str:
    """A short command for this account: `cc`/`cx` plus a letter from its name.

    Tries each letter of the account name in turn, then digits, so `client`
    becomes `ccc` and a second c-account falls through to `ccl`, `cci`, ...
    Returns "" when nothing is free, rather than shadowing a real command.
    """
    prefix = ALIAS_PREFIX.get(tool_name, "cc")
    seen = set()
    for ch in name.lower():
        if not ch.isalnum() or ch in seen:
            continue
        seen.add(ch)
        if alias_available(prefix + ch, taken):
            return prefix + ch
    for ch in "23456789":
        if alias_available(prefix + ch, taken):
            return prefix + ch
    return ""


# --------------------------------------------------------------------------
# tools
# --------------------------------------------------------------------------

class Tool:
    """Everything that differs between the coding agents cca can drive."""

    name = ""
    binary = ""
    config_env = ""            # the env var that relocates the config dir
    credentials_name = ""      # token file, relative to the space
    linkable: tuple = ()       # assets `link` may share between spaces
    supports_legacy_default = False
    requires_config_file = True
    default_dir: Path = Path()

    # -- paths ------------------------------------------------------------

    def config_file(self, acct: "Account") -> Path:
        raise NotImplementedError

    def credentials_file(self, acct: "Account") -> Path:
        raise NotImplementedError

    # -- identity ---------------------------------------------------------

    def identity(self, acct: "Account") -> dict:
        """email/org/plan/expiry read from disk. Never returns secrets."""
        raise NotImplementedError

    def auth_status(self, acct: "Account") -> dict:
        """Authoritative check - shells out to the tool itself."""
        raise NotImplementedError

    def login_argv(self) -> list[str]:
        raise NotImplementedError

    def logout_argv(self) -> list[str]:
        raise NotImplementedError

    # -- helpers ----------------------------------------------------------

    def _run(self, acct: "Account", argv: list[str]) -> tuple[str, str]:
        exe = which(self.binary)
        if not exe:
            return "", f"`{self.binary}` not found on PATH"
        try:
            proc = subprocess.run([exe] + argv, env=acct.launch_env(),
                                  capture_output=True, text=True, timeout=90)
        except (OSError, subprocess.SubprocessError) as exc:
            return "", str(exc)
        return proc.stdout + proc.stderr, ""

    def detect(self, taken: set[str]) -> list[tuple[str, dict]]:
        """Accounts of this tool already present on the machine."""
        return []


class ClaudeTool(Tool):
    name = "claude"
    binary = "claude"
    config_env = "CLAUDE_CONFIG_DIR"
    credentials_name = ".credentials.json"
    linkable = ("skills", "agents", "commands", "output-styles", "plugins", "hooks")
    supports_legacy_default = True
    requires_config_file = True
    default_dir = LEGACY_DIR

    def config_file(self, acct: "Account") -> Path:
        # The whole reason legacy_default exists.
        return LEGACY_CONFIG if acct.legacy_default else acct.config_dir / ".claude.json"

    def credentials_file(self, acct: "Account") -> Path:
        return acct.config_dir / ".credentials.json"

    def identity(self, acct: "Account") -> dict:
        cfg = read_json(self.config_file(acct)) or {}
        oauth = cfg.get("oauthAccount") or {}
        creds = read_json(self.credentials_file(acct)) or {}
        oa = creds.get("claudeAiOauth") or {}
        exp = oa.get("expiresAt")
        return {
            "email": oauth.get("emailAddress"),
            "org": oauth.get("organizationName"),
            "plan": oa.get("subscriptionType"),
            "projects": len(cfg.get("projects") or {}),
            "config_ok": self.config_file(acct).exists(),
            "logged_in": bool(oa.get("accessToken")),
            "expires_at": exp / 1000.0 if isinstance(exp, (int, float)) else None,
            "refreshed_at": None,
        }

    def auth_status(self, acct: "Account") -> dict:
        out, err = self._run(acct, ["auth", "status"])
        if err:
            return {"error": err}
        return extract_json(out) or {"error": (out or "no output").strip()[:200]}

    def login_argv(self) -> list[str]:
        return ["auth", "login"]

    def logout_argv(self) -> list[str]:
        return ["auth", "logout"]

    def detect(self, taken: set[str]) -> list[tuple[str, dict]]:
        found = []
        if LEGACY_CONFIG.exists() or LEGACY_DIR.exists():
            cfg = read_json(LEGACY_CONFIG) or {}
            org = (cfg.get("oauthAccount") or {}).get("organizationName", "")
            found.append(("work", {
                "tool": self.name,
                "config_dir": str(LEGACY_DIR),
                "legacy_default": True,
                "description": f"{org} (original install)".strip(),
            }))
            taken.add("work")

        for path in sorted(HOME.glob(".claude-*")):
            if not path.is_dir() or path == ROOT or not (path / ".claude.json").exists():
                continue
            name = path.name[len(".claude-"):]
            if not NAME_RE.match(name) or name in taken:
                continue
            cfg = read_json(path / ".claude.json") or {}
            found.append((name, {
                "tool": self.name,
                "config_dir": str(path),
                "legacy_default": False,
                "description": (cfg.get("oauthAccount") or {}).get("emailAddress", ""),
            }))
            taken.add(name)
        return found


class CodexTool(Tool):
    name = "codex"
    binary = "codex"
    config_env = "CODEX_HOME"
    credentials_name = "auth.json"
    linkable = ("skills", "plugins", "rules", "themes", "prompts")
    supports_legacy_default = False
    # config.toml is optional for Codex - auth.json is what matters.
    requires_config_file = False
    default_dir = CODEX_DIR

    def config_file(self, acct: "Account") -> Path:
        return acct.config_dir / "config.toml"

    def credentials_file(self, acct: "Account") -> Path:
        return acct.config_dir / "auth.json"

    def identity(self, acct: "Account") -> dict:
        auth = read_json(self.credentials_file(acct)) or {}
        tokens = auth.get("tokens") or {}
        claims = jwt_claims(tokens.get("id_token") or "")
        oai = claims.get("https://api.openai.com/auth") or {}

        org = None
        for entry in oai.get("organizations") or []:
            if entry.get("is_default"):
                org = entry.get("title")
                break

        mode = auth.get("auth_mode")
        logged_in = bool(tokens.get("access_token")) or bool(auth.get("OPENAI_API_KEY"))
        return {
            "email": claims.get("email") or (mode if logged_in else None),
            "org": org,
            "plan": oai.get("chatgpt_plan_type"),
            "projects": 0,
            "config_ok": self.credentials_file(acct).exists(),
            "logged_in": logged_in,
            # Deliberately None. The id_token's `exp` is NOT a liveness signal
            # for Codex: it holds a refresh_token and renews on demand, so a
            # long-expired id_token still means a perfectly good session.
            # Reporting it as an expiry produced false "token-expired" states.
            "expires_at": None,
            "refreshed_at": parse_iso(auth.get("last_refresh")),
        }

    def auth_status(self, acct: "Account") -> dict:
        out, err = self._run(acct, ["login", "status"])
        if err:
            return {"error": err}
        low = out.lower()
        logged = "logged in" in low and "not logged in" not in low
        if "chatgpt" in low:
            method = "chatgpt"
        elif "api key" in low:
            method = "api key"
        else:
            method = "unknown" if logged else "none"
        ident = self.identity(acct)
        return {
            "loggedIn": logged,
            "authMethod": method,
            "email": ident["email"],
            "orgName": ident["org"],
            "subscriptionType": ident["plan"],
        }

    def login_argv(self) -> list[str]:
        return ["login"]

    def logout_argv(self) -> list[str]:
        return ["logout"]

    def detect(self, taken: set[str]) -> list[tuple[str, dict]]:
        found = []
        if (CODEX_DIR / "auth.json").exists():
            name = "codex" if "codex" not in taken else "codex-default"
            found.append((name, {
                "tool": self.name,
                "config_dir": str(CODEX_DIR),
                "legacy_default": False,
                "description": "original install",
            }))
            taken.add(name)

        for path in sorted(HOME.glob(".codex-*")):
            if not path.is_dir() or not (path / "auth.json").exists():
                continue
            name = path.name[len(".codex-"):]
            if not NAME_RE.match(name):
                continue
            if name in taken:
                name = f"{name}-cx"
            if name in taken:
                continue
            found.append((name, {
                "tool": self.name,
                "config_dir": str(path),
                "legacy_default": False,
                "description": "",
            }))
            taken.add(name)
        return found


TOOLS: dict[str, Tool] = {t.name: t for t in (ClaudeTool(), CodexTool())}
DEFAULT_TOOL = "claude"


def get_tool(name: str) -> Tool:
    if name not in TOOLS:
        die(f"unknown tool {name!r}. known: {', '.join(sorted(TOOLS))}")
    return TOOLS[name]


# --------------------------------------------------------------------------
# registry
# --------------------------------------------------------------------------

class Account:
    def __init__(self, name: str, data: dict):
        self.name = name
        # Registry v1 had no "tool" key; everything in it was Claude.
        self.tool_name = data.get("tool", DEFAULT_TOOL)
        self.config_dir = Path(data["config_dir"]).expanduser()
        self.legacy_default = bool(data.get("legacy_default", False))
        self.description = data.get("description", "")
        # None = never assigned yet; "" = deliberately none (nothing was free).
        self.alias = data.get("alias")
        self.env = dict(data.get("env") or {})
        self.created = data.get("created", "")

    @property
    def tool(self) -> Tool:
        return TOOLS.get(self.tool_name, TOOLS[DEFAULT_TOOL])

    def to_dict(self) -> dict:
        return {
            "tool": self.tool_name,
            "config_dir": str(self.config_dir),
            "legacy_default": self.legacy_default,
            "description": self.description,
            "alias": self.alias or "",
            "env": self.env,
            "created": self.created,
        }

    @property
    def launch_cmd(self) -> str:
        """What the user actually types to start this account."""
        return self.alias or f"{entry_for(self.tool_name)} {self.name}"

    @property
    def config_file(self) -> Path:
        return self.tool.config_file(self)

    @property
    def credentials_file(self) -> Path:
        return self.tool.credentials_file(self)

    def identity(self) -> dict:
        return self.tool.identity(self)

    def auth_status(self) -> dict:
        return self.tool.auth_status(self)

    def launch_env(self, base: dict | None = None, clean: bool = True) -> dict:
        env = dict(os.environ if base is None else base)
        if clean:
            for key in SESSION_ENV:
                env.pop(key, None)
        if self.legacy_default:
            # The whole point of the flag: this account only works unset.
            env.pop(self.tool.config_env, None)
        else:
            env[self.tool.config_env] = str(self.config_dir)
        env["CCA_ACCOUNT"] = self.name
        env["CCA_TOOL"] = self.tool_name
        env.update(self.env)
        return env


class Registry:
    def __init__(self, data: dict):
        self.version = data.get("version", REGISTRY_VERSION)
        self.accounts: dict[str, Account] = {
            name: Account(name, d) for name, d in (data.get("accounts") or {}).items()
        }
        self.defaults: dict[str, str] = dict(data.get("defaults") or {})

        # v1/v2 kept a single global default; file it under its own tool.
        old = data.get("default")
        if old and old in self.accounts and not self.defaults:
            self.defaults[self.accounts[old].tool_name] = old

    @classmethod
    def load(cls) -> "Registry":
        data = read_json(REGISTRY)
        if data is None:
            reg = cls(bootstrap())
            reg.save()
            print(f"{c.dim}(initialised {tilde(REGISTRY)} from what was already "
                  f"on this machine){c.reset}", file=sys.stderr)
            return reg

        reg = cls(data)
        version = data.get("version", 1)
        if version < REGISTRY_VERSION:
            added = []
            # Codex arrived in v2. Only a v1 registry can be missing it; re-running
            # detection on a later one would re-adopt ~/.codex under a second name
            # and leave two accounts fighting over one directory.
            if version < 2:
                known = {a.config_dir for a in reg.accounts.values()}
                for name, d in TOOLS["codex"].detect(set(reg.accounts)):
                    if Path(d["config_dir"]) in known:
                        continue
                    reg.accounts[name] = Account(name, dict(d, env={}, created=_now()))
                    added.append(name)
            reg.version = REGISTRY_VERSION
            reg.save()
            msg = f"registry upgraded to v{REGISTRY_VERSION}"
            if added:
                msg += f"; adopted codex account: {', '.join(added)}"
            print(f"{c.dim}({msg}){c.reset}", file=sys.stderr)
        elif any(a.alias is None for a in reg.accounts.values()):
            # Accounts that predate shortcuts get one on first sight.
            reg.save()
            made = [f"{a.alias}={n}" for n, a in sorted(reg.accounts.items()) if a.alias]
            if made:
                print(f"{c.dim}(shortcuts created: {', '.join(made)}){c.reset}",
                      file=sys.stderr)
        return reg

    def save(self) -> None:
        self.assign_aliases()
        write_json_atomic(REGISTRY, {
            "version": REGISTRY_VERSION,
            "defaults": dict(sorted(self.defaults.items())),
            "accounts": {n: a.to_dict() for n, a in sorted(self.accounts.items())},
        })
        self.sync_alias_links()

    def assign_aliases(self) -> bool:
        """Give unassigned accounts a short command. Returns True if anything changed.

        Only accounts whose alias is None are touched, so a shortcut the user
        cleared stays cleared and one that could not be allocated is not
        retried on every load.
        """
        taken = {a.alias for a in self.accounts.values() if a.alias}
        changed = False
        for name, acct in sorted(self.accounts.items()):
            if acct.alias is not None:
                continue
            acct.alias = derive_alias(name, acct.tool_name, taken)
            changed = True
            if acct.alias:
                taken.add(acct.alias)
        return changed

    def sync_alias_links(self) -> None:
        """Create a symlink per alias; remove ours that no longer apply.

        Only symlinks pointing at this script are ever removed, so a real
        program that happens to share a name is never touched.
        """
        wanted = {a.alias for a in self.accounts.values() if a.alias}
        try:
            BIN_DIR.mkdir(parents=True, exist_ok=True)
            for path in BIN_DIR.iterdir():
                if path.name in RESERVED_ALIASES or path.name in wanted:
                    continue
                if owned_symlink(path):
                    path.unlink()
            for alias in sorted(wanted):
                link = BIN_DIR / alias
                if owned_symlink(link):
                    continue
                if link.exists() or link.is_symlink():
                    continue          # something else owns the name; leave it
                link.symlink_to(SCRIPT)
        except OSError as exc:
            warn(f"could not update shortcuts in {tilde(BIN_DIR)}: {exc}")

    def by_alias(self, alias: str) -> Account | None:
        for acct in self.accounts.values():
            if acct.alias and acct.alias == alias:
                return acct
        return None

    def get(self, name: str) -> Account:
        if name not in self.accounts:
            known = ", ".join(sorted(self.accounts)) or "(none)"
            die(f"no account named {name!r}. known: {known}")
        return self.accounts[name]

    def for_tool(self, tool_name: str) -> dict[str, Account]:
        return {n: a for n, a in sorted(self.accounts.items())
                if a.tool_name == tool_name}

    def default_for(self, tool_name: str) -> str | None:
        """The account `cca`/`cxa` launches with no arguments.

        An explicit choice wins; otherwise a tool with exactly one account
        needs no configuration at all.
        """
        chosen = self.defaults.get(tool_name)
        if chosen and chosen in self.accounts:
            return chosen
        mine = self.for_tool(tool_name)
        return next(iter(mine)) if len(mine) == 1 else None


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def bootstrap() -> dict:
    """Detect accounts already present so first run is zero-setup."""
    accounts: dict[str, dict] = {}
    taken: set[str] = set()
    for tool in (TOOLS["claude"], TOOLS["codex"]):
        for name, data in tool.detect(taken):
            accounts[name] = dict(data, env={}, created=_now())

    defaults = {}
    if "work" in accounts:
        defaults["claude"] = "work"
    return {"version": REGISTRY_VERSION, "defaults": defaults, "accounts": accounts}


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def cmd_list(args) -> int:
    reg = Registry.load()
    wanted = getattr(args, "tool", None)
    items = [(n, a) for n, a in sorted(reg.accounts.items())
             if not wanted or a.tool_name == wanted]
    if not items:
        print(f"no accounts registered. add one with: "
              f"{args.entry} add <name>")
        return 0

    active = current_account_name(reg)
    rows = []
    for name, acct in items:
        ident = acct.identity()
        state = []
        if acct.tool.requires_config_file and not ident["config_ok"]:
            state.append("no-config")
        if not ident["logged_in"]:
            state.append("logged-out")
        elif ident["expires_at"] and ident["expires_at"] < time.time():
            state.append("token-expired")
        if acct.legacy_default:
            state.append("legacy")
        size = human_bytes(dir_size(acct.config_dir)) \
            if args.du and acct.config_dir.is_dir() else "-"
        is_default = reg.default_for(acct.tool_name) == name
        rows.append({
            "mark": "*" if name == active else (">" if is_default else " "),
            "launch": acct.launch_cmd,
            "cmd": acct.alias or "-",
            "name": name,
            "tool": acct.tool_name,
            "email": ident["email"] or "-",
            "plan": ident["plan"] or "-",
            "space": tilde(acct.config_dir),
            "size": size,
            "state": ",".join(state) or "ok",
        })

    if args.json:
        print(json.dumps(rows, indent=2))
        return 0

    cols = [("mark", ""), ("name", "ACCOUNT"), ("launch", "LAUNCH WITH"),
            ("email", "EMAIL"), ("plan", "PLAN"), ("space", "SPACE"),
            ("state", "STATE")]
    if args.du:
        cols.insert(-1, ("size", "SIZE"))
    widths = {k: max(len(h), *(len(str(r[k])) for r in rows)) for k, h in cols}

    print(f"{c.bold}{'  '.join(h.ljust(widths[k]) for k, h in cols)}{c.reset}")
    for r in rows:
        line = "  ".join(str(r[k]).ljust(widths[k]) for k, _ in cols)
        color = c.green if r["mark"] == "*" else ""
        if r["state"] not in ("ok", "legacy"):
            color = c.yellow
        print(f"{color}{line}{c.reset}")

    print(f"\n{c.dim}* = this shell   > = that tool's default, launched by bare "
          f"`cca` / `cxa`{c.reset}")
    return 0


def cmd_run(args) -> int:
    reg = Registry.load()
    acct = reg.get(args.name)

    # `cca` drives claude accounts, `cxa` drives codex ones. Say so plainly.
    if acct.tool_name != args.entry_tool:
        right = entry_for(acct.tool_name)
        die(f"{acct.name!r} is a {acct.tool_name} account; launch it with "
            f"`{right} {acct.name}` (you used `{args.entry}`)")

    exe = which(acct.tool.binary)
    if not exe:
        die(f"`{acct.tool.binary}` not found on PATH")
    if not acct.config_dir.is_dir():
        die(f"space {tilde(acct.config_dir)} does not exist (run: {args.entry} doctor)")
    if acct.tool.requires_config_file and not acct.config_file.exists():
        warn(f"{acct.name}: {tilde(acct.config_file)} missing - {acct.tool_name} "
             f"may treat this as a fresh account. See: {args.entry} doctor")

    env = acct.launch_env(clean=not args.keep_env)
    argv = [exe] + list(args.tool_args)
    if args.dry_run:
        var = acct.tool.config_env
        shown = f"{var} unset" if acct.legacy_default else f"{var}={env[var]}"
        print(f"{shown} \\\n  {' '.join(argv)}")
        return 0
    os.execve(exe, argv, env)  # replaces this process; never returns


def launch_default(entry: str, entry_tool: str, extra: list[str]) -> int:
    """Bare `cca` / `cxa`: launch that tool's default account."""
    reg = Registry.load()
    name = reg.default_for(entry_tool)
    if not name:
        mine = reg.for_tool(entry_tool)
        if not mine:
            die(f"no {entry_tool} accounts yet. create one with: "
                f"{entry} add <name>")
        # Several accounts and nothing chosen: that is the picker's question,
        # so ask it rather than printing an error about a flag.
        if sys.stdin.isatty() and sys.stdout.isatty():
            return cmd_pick(argparse.Namespace(
                tool=entry_tool, cached=False, timeout=None, dry_run=False,
                tool_args=extra, entry=entry, entry_tool=entry_tool))
        die(f"several {entry_tool} accounts ({', '.join(mine)}) and no default. "
            f"pick one with: {entry} default <name>, or run `{entry} pick`")
    return cmd_run(argparse.Namespace(
        name=name, dry_run=False, keep_env=False, tool_args=extra,
        entry=entry, entry_tool=entry_tool))


def cmd_exec(args) -> int:
    """Run an arbitrary command inside an account's environment."""
    reg = Registry.load()
    acct = reg.get(args.name)
    if not args.command:
        die(f"nothing to run: {args.entry} exec <account> -- <command> [args]")
    exe = which(args.command[0])
    if not exe:
        die(f"command not found: {args.command[0]}")
    os.execve(exe, list(args.command), acct.launch_env(clean=not args.keep_env))


def cmd_add(args) -> int:
    reg = Registry.load()
    name = args.name
    tool = get_tool(args.tool or args.entry_tool)

    if not NAME_RE.match(name):
        die(f"invalid account name {name!r}: use letters, digits, '-' and '_'")
    if name in reg.accounts:
        die(f"account {name!r} already exists")
    if name in SUBCOMMANDS:
        die(f"{name!r} is a subcommand; pick another name")

    config_dir = Path(args.dir).expanduser().resolve() if args.dir else SPACES / name
    for other in reg.accounts.values():
        if other.config_dir == config_dir:
            die(f"{tilde(config_dir)} is already the space for {other.name!r}")
    if tool.supports_legacy_default and config_dir == tool.default_dir:
        die(f"{tilde(tool.default_dir)} is the original install's directory and "
            f"cannot be used as an isolated space (see `{args.entry} doctor`)")

    # None, not "": "" means "deliberately no shortcut" and would stop
    # assign_aliases() from ever giving this account one.
    alias = args.alias or None
    if alias:
        taken = {a.alias for a in reg.accounts.values() if a.alias}
        if not alias_available(alias, taken):
            die(f"shortcut {alias!r} is reserved, already used, or shadows an "
                f"existing command")

    existing = config_dir.exists() and any(config_dir.iterdir())
    config_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(config_dir, 0o700)

    reg.accounts[name] = Account(name, {
        "tool": tool.name,
        "config_dir": str(config_dir),
        "legacy_default": False,
        "description": args.description or "",
        "alias": alias,
        "env": {},
        "created": _now(),
    })
    reg.defaults.setdefault(tool.name, name)
    reg.save()

    acct = reg.accounts[name]
    entry = entry_for(tool.name)
    info(f"added {name!r} ({tool.name}) -> {tilde(config_dir)}"
         + (" (reusing existing directory)" if existing else ""))
    if acct.alias:
        print(f"   shortcut: {c.bold}{acct.alias}{c.reset}")
    if args.login:
        return cmd_login(argparse.Namespace(name=name, entry=entry))
    print(f"\nnext: {c.bold}{entry} login {name}{c.reset}"
          f"   then: {c.bold}{acct.launch_cmd}{c.reset}")
    return 0


def cmd_alias(args) -> int:
    """Show, set or repair the short per-account commands."""
    reg = Registry.load()
    if args.name:
        acct = reg.get(args.name)
        if args.set:
            taken = {a.alias for a in reg.accounts.values()
                     if a.alias and a.name != acct.name}
            if args.set != "-" and not alias_available(args.set, taken):
                die(f"shortcut {args.set!r} is reserved, already used, or "
                    f"shadows an existing command")
            acct.alias = "" if args.set == "-" else args.set
            reg.save()
            info(f"{acct.name} -> {acct.launch_cmd}")
            return 0
        print(acct.launch_cmd)
        return 0

    if args.repair:
        for acct in reg.accounts.values():
            acct.alias = None
        reg.save()
        info("shortcuts regenerated")

    reg.sync_alias_links()
    for name, acct in sorted(reg.accounts.items()):
        if not acct.alias:
            entry = entry_for(acct.tool_name)
            mark = f"{c.yellow}!{c.reset}"
            note = f"none set - use `{entry} alias {name} --set cc<letter>`"
        else:
            link = BIN_DIR / acct.alias
            if owned_symlink(link):
                mark, note = f"{c.green}v{c.reset}", tilde(link)
            else:
                mark, note = f"{c.red}x{c.reset}", f"{tilde(link)} missing"
        print(f"{mark} {(acct.alias or '-'):<5} -> {name:<12} {c.dim}{note}{c.reset}")
    return 0


def cmd_rm(args) -> int:
    reg = Registry.load()
    acct = reg.get(args.name)
    if args.purge and (acct.legacy_default or acct.config_dir == acct.tool.default_dir):
        die(f"refusing to purge {tilde(acct.config_dir)}: it is the original "
            f"{acct.tool_name} install, shared with bare `{acct.tool.binary}`. "
            f"Remove the registry entry without --purge if you really want it gone.")

    if args.purge and not args.yes:
        size = human_bytes(dir_size(acct.config_dir)) if acct.config_dir.is_dir() else "0B"
        print(f"{c.red}This deletes {tilde(acct.config_dir)} ({size}) including "
              f"credentials, history and projects.{c.reset}")
        try:
            if input(f"Type the account name to confirm [{acct.name}]: ").strip() != acct.name:
                die("aborted", 2)
        except (EOFError, KeyboardInterrupt):
            die("aborted", 2)

    del reg.accounts[args.name]
    for tool_name, chosen in list(reg.defaults.items()):
        if chosen == args.name:
            del reg.defaults[tool_name]
    reg.save()

    if args.purge and acct.config_dir.is_dir():
        shutil.rmtree(acct.config_dir)
        info(f"removed {args.name!r} and deleted {tilde(acct.config_dir)}")
    else:
        info(f"removed {args.name!r} from the registry "
             f"(space kept at {tilde(acct.config_dir)})")
    return 0


def cmd_rename(args) -> int:
    reg = Registry.load()
    acct = reg.get(args.old)
    if not NAME_RE.match(args.new):
        die(f"invalid account name {args.new!r}")
    if args.new in reg.accounts:
        die(f"account {args.new!r} already exists")
    del reg.accounts[args.old]
    acct.name = args.new
    reg.accounts[args.new] = acct
    for tool_name, chosen in list(reg.defaults.items()):
        if chosen == args.old:
            reg.defaults[tool_name] = args.new
    reg.save()
    info(f"renamed {args.old!r} -> {args.new!r} "
         f"{c.dim}(space unchanged: {tilde(acct.config_dir)}){c.reset}")
    return 0


def cmd_status(args) -> int:
    reg = Registry.load()
    names = [args.name] if args.name else sorted(reg.accounts)
    active = current_account_name(reg)
    out = {}
    for name in names:
        acct = reg.get(name)
        st = acct.auth_status()
        ident = acct.identity()
        out[name] = {**st, "tool": acct.tool_name, "space": str(acct.config_dir),
                     "config_file": str(acct.config_file),
                     "expires": ident["expires_at"]}
        if args.json:
            continue
        tag = f" {c.green}(this shell){c.reset}" if name == active else ""
        print(f"{c.bold}{name}{c.reset} "
              f"{c.dim}[{entry_for(acct.tool_name)} {name}]{c.reset}{tag}")
        if "error" in st:
            print(f"  {c.red}{st['error']}{c.reset}")
        else:
            print(f"  logged in : {st.get('loggedIn')}  via {st.get('authMethod')}")
            print(f"  account   : {st.get('email') or '-'}")
            print(f"  org       : {st.get('orgName') or '-'}  "
                  f"plan={st.get('subscriptionType') or '-'}")
        exp, refreshed = ident["expires_at"], ident.get("refreshed_at")
        if exp:
            colour = c.red if exp < time.time() else c.dim
            print(f"  token     : {colour}expires {rel_time(exp)}{c.reset}")
        elif refreshed:
            print(f"  token     : {c.dim}auto-refreshing, last renewed "
                  f"{rel_time(refreshed)}{c.reset}")
        print(f"  space     : {tilde(acct.config_dir)}"
              + ("  (legacy default)" if acct.legacy_default else ""))
        print()
    if args.json:
        print(json.dumps(out, indent=2))
    return 0


def cmd_which(args) -> int:
    reg = Registry.load()
    name = current_account_name(reg)
    if not name:
        for tool in TOOLS.values():
            cfg = os.environ.get(tool.config_env)
            if cfg:
                print(f"unregistered {tool.name} space: {cfg}")
                return 1
        print("default (no config-dir variable set)")
        return 1
    acct = reg.get(name)
    ident = acct.identity()
    print(f"{name}  [{entry_for(acct.tool_name)}]  {ident['email'] or '-'}  "
          f"{tilde(acct.config_dir)}")
    return 0


def cmd_default(args) -> int:
    """Get or set the account that bare `cca` / `cxa` launches."""
    reg = Registry.load()
    if not args.name:
        for tool_name in sorted(TOOLS):
            chosen = reg.default_for(tool_name) or "(none)"
            print(f"{entry_for(tool_name):<4} -> {chosen}")
        return 0
    acct = reg.get(args.name)
    reg.defaults[acct.tool_name] = acct.name
    reg.save()
    info(f"`{entry_for(acct.tool_name)}` with no arguments now launches "
         f"{acct.name!r}")
    return 0


def cmd_login(args) -> int:
    reg = Registry.load()
    acct = reg.get(args.name)
    exe = which(acct.tool.binary)
    if not exe:
        die(f"`{acct.tool.binary}` not found on PATH")
    acct.config_dir.mkdir(parents=True, exist_ok=True)
    print(f"{c.dim}logging in to the {acct.name!r} {acct.tool_name} space "
          f"({tilde(acct.config_dir)}){c.reset}")
    return subprocess.call([exe] + acct.tool.login_argv(), env=acct.launch_env())


def cmd_logout(args) -> int:
    reg = Registry.load()
    acct = reg.get(args.name)
    exe = which(acct.tool.binary)
    if not exe:
        die(f"`{acct.tool.binary}` not found on PATH")
    return subprocess.call([exe] + acct.tool.logout_argv(), env=acct.launch_env())


def cmd_link(args) -> int:
    reg = Registry.load()
    target = reg.get(args.name)
    source = reg.get(args.source)
    if target.name == source.name:
        die("cannot link an account to itself")
    if target.tool_name != source.tool_name:
        die(f"cannot share between different tools: {target.name!r} is "
            f"{target.tool_name}, {source.name!r} is {source.tool_name}")
    if args.asset not in target.tool.linkable and not args.force:
        die(f"{args.asset!r} is not a shareable {target.tool_name} asset. "
            f"choose from: {', '.join(target.tool.linkable)} (or pass --force)")

    src = source.config_dir / args.asset
    dst = target.config_dir / args.asset
    if not src.exists():
        die(f"{tilde(src)} does not exist in account {source.name!r}")
    if dst.is_symlink():
        dst.unlink()
    elif dst.exists():
        die(f"{tilde(dst)} already exists and is not a symlink; move it aside first")

    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.symlink_to(src)
    info(f"{target.name}:{args.asset} -> {tilde(src)}")
    warn("shared assets cross the account boundary; keep work-only skills unshared")
    return 0


def cmd_unlink(args) -> int:
    reg = Registry.load()
    acct = reg.get(args.name)
    path = acct.config_dir / args.asset
    if not path.is_symlink():
        die(f"{tilde(path)} is not a symlink (nothing to unlink)")
    path.unlink()
    info(f"unlinked {acct.name}:{args.asset}")
    return 0


def cmd_doctor(args) -> int:
    reg = Registry.load()
    problems = 0
    seen_dirs: dict[Path, str] = {}

    print(f"{c.bold}registry{c.reset}  {tilde(REGISTRY)}  (v{reg.version})")
    for entry, tool_name in sorted(ENTRYPOINTS.items()):
        tool = TOOLS[tool_name]
        path = which(entry)
        binary = which(tool.binary)
        ok = path and binary
        mark = f"{c.green}v{c.reset}" if ok else f"{c.red}x{c.reset}"
        print(f"{mark} {c.bold}{entry}{c.reset} -> {tool_name}  "
              f"{binary or c.yellow + tool.binary + ' not on PATH' + c.reset}")
        if not path:
            print(f"    {c.dim}fix: ln -s {Path(__file__).resolve()} "
                  f"{BIN_DIR / entry}{c.reset}")
            problems += 1
    print()

    for name, acct in sorted(reg.accounts.items()):
        tool = acct.tool
        entry = entry_for(acct.tool_name)
        print(f"{c.bold}{name}{c.reset} {c.dim}[{acct.launch_cmd}]{c.reset}  "
              f"{tilde(acct.config_dir)}")
        issues: list[str] = []
        fixes: list[str] = []

        if acct.alias:
            link = BIN_DIR / acct.alias
            if not owned_symlink(link):
                what = "is not ours" if link.exists() else "is missing"
                issues.append(f"shortcut {acct.alias!r} {what} ({tilde(link)})")
                fixes.append(f"{entry} alias --repair")
        else:
            # Nothing else ever revisits this, so say so rather than leaving the
            # account quietly without a short command.
            issues.append(f"no shortcut command (launch is `{entry} {name}`)")
            fixes.append(f"{entry} alias {name} --set cc<letter>")

        if acct.tool_name not in TOOLS:
            issues.append(f"unknown tool {acct.tool_name!r}")
        if not which(tool.binary):
            issues.append(f"`{tool.binary}` is not on PATH")

        if not acct.config_dir.is_dir():
            issues.append("space directory is missing")
            fixes.append(f"mkdir -p {acct.config_dir} && {entry} login {name}")
        elif not os.access(acct.config_dir, os.W_OK):
            issues.append("space directory is not writable")

        prev = seen_dirs.get(acct.config_dir)
        if prev:
            issues.append(f"shares its space with account {prev!r} - "
                          f"they will overwrite each other")
        seen_dirs[acct.config_dir] = name

        if (tool.supports_legacy_default and acct.config_dir == tool.default_dir
                and not acct.legacy_default):
            issues.append(f"points at {tilde(tool.default_dir)} but is not flagged "
                          f"legacy_default; {tool.binary} will not find "
                          f"{tilde(LEGACY_CONFIG)}")
            fixes.append(f"set legacy_default for {name} in {tilde(REGISTRY)}")
        if acct.legacy_default and not tool.supports_legacy_default:
            issues.append(f"flagged legacy_default, but {tool.name} has no such "
                          f"quirk - the config-dir variable would be dropped for "
                          f"no reason")

        if tool.requires_config_file and not acct.config_file.exists():
            issues.append(f"config file missing: {tilde(acct.config_file)}")
            fixes.append(f"{entry} login {name}")

        ident = acct.identity()
        if not ident["logged_in"]:
            issues.append("no stored credentials")
            fixes.append(f"{entry} login {name}")
        elif ident["expires_at"] and ident["expires_at"] < time.time():
            issues.append(f"token expired {rel_time(ident['expires_at'])}")
            fixes.append(f"{entry} login {name}")

        # Two distinct exposures: the token file, and everything else in the
        # space (transcripts, project state). Report them separately so the
        # severity is honest.
        for path, what in ((acct.credentials_file, "credentials"),
                           (acct.config_file, "account config")):
            if path.exists() and os.stat(path).st_mode & 0o077:
                issues.append(f"{tilde(path)} is readable by other local users "
                              f"({what})")
                fixes.append(f"chmod 600 {path}")

        if acct.config_dir.is_dir():
            mode = os.stat(acct.config_dir).st_mode & 0o777
            if mode & 0o077:
                issues.append(f"space is group/world accessible (mode {mode:o}); "
                              f"other local users can read session transcripts "
                              f"and project state")
                fixes.append(f"chmod 700 {acct.config_dir}")

        if issues:
            problems += len(issues)
            for i in issues:
                print(f"  {c.red}x{c.reset} {i}")
            for f in dict.fromkeys(fixes):
                print(f"    {c.dim}fix: {f}{c.reset}")
        else:
            print(f"  {c.green}v{c.reset} healthy  {ident['email'] or '-'}  "
                  f"plan={ident['plan'] or '?'}")
        print()

    for tool_name in sorted(TOOLS):
        if reg.for_tool(tool_name) and not reg.default_for(tool_name):
            print(f"{c.yellow}!{c.reset} `{entry_for(tool_name)}` with no arguments "
                  f"has no account to launch")
            print(f"  {c.dim}fix: {entry_for(tool_name)} default <name>{c.reset}")
            problems += 1

    print(f"{c.bold}{problems} problem(s){c.reset}" if problems
          else f"{c.green}all accounts healthy{c.reset}")
    return 1 if problems else 0


# --------------------------------------------------------------------------
# usage
# --------------------------------------------------------------------------

def usage_spec(acct: Account) -> dict:
    """What `usage.py` needs to probe one account, as plain data.

    Deliberately not an Account: the usage module is importable on its own so
    the perf-ai console can load it by path, and a module that takes registry
    objects could not be.
    """
    ident = acct.identity()
    return {
        "name": acct.name,
        "tool": acct.tool_name,
        "config_dir": str(acct.config_dir),
        "config_file": str(acct.config_file),
        "credentials_file": str(acct.credentials_file),
        "plan": ident.get("plan"),
        "email": ident.get("email"),
    }


def usage_reports(names=None, tool: str | None = None, *, live: bool = True,
                  timeout: float | None = None, ranked: bool = False) -> list[dict]:
    """Every account's usage, in registry order (or best-first if `ranked`).

    The one function the console calls. Returns plain dicts and never raises
    for a single bad account — an account that could not be measured comes
    back with `ok: false` and the reason, because a fleet view that drops a
    row is a fleet view that lies about how many accounts there are.
    """
    module = sibling("usage")
    if module is None:
        raise RuntimeError("usage.py is missing from beside cca.py")
    reg = Registry.load()
    chosen = [a for n, a in sorted(reg.accounts.items())
              if (not names or n in set(names)) and (not tool or a.tool_name == tool)]
    kwargs = {"live": live}
    if timeout is not None:
        kwargs["timeout"] = timeout
    reports = module.collect([usage_spec(a) for a in chosen], **kwargs)
    return module.rank(reports) if ranked else reports


def _usage_row(module, report: dict, bar_width: int = 0) -> dict:
    windows = report.get("windows") or {}
    binding = windows.get(report.get("binding"))

    def cell(name):
        """One window, drawn the same way the picker draws it."""
        window = windows.get(name)
        if not window:
            return "-".rjust(bar_width + 5) if bar_width else "-"
        text = f"{window['used_pct']:.0f}%".rjust(4)
        if not bar_width:
            return text.strip()
        return f"{module.bar(window['used_pct'], bar_width)} {text}"

    note = ""
    if not report.get("ok"):
        note = report.get("error") or "unavailable"
    elif report.get("source") == "rollout":
        note = f"from last session, {module.fmt_age(report.get('stale_seconds'))}"
    elif report.get("source") != "live":
        note = f"last reading {module.fmt_age(report.get('stale_seconds'))}"
        if report.get("error"):
            note += f" ({report['error']})"
    return {
        "name": report.get("account") or "?",
        "tool": report.get("tool") or "-",
        "plan": report.get("plan") or "-",
        "five": cell("five_hour"),
        "week": cell("seven_day"),
        "free": "-" if report.get("free_pct") is None else f"{report['free_pct']:.0f}%",
        "binding": module.SHORT.get(report.get("binding"), "-"),
        "resets": module.fmt_duration(binding["resets_in"] if binding else None),
        "note": note,
    }


def _usage_bar_width(module, rows: list[dict]) -> int:
    """Whatever the terminal has left once the other columns are satisfied.

    A bar of width *b* replaces a bare `19%` with `<bar> 19%`, costing `b + 1`
    columns in each of the two window columns. Measured off the rendered rows
    rather than assumed from the terminal width, because the account names and
    the note column are what actually vary.
    """
    keys = ("name", "plan", "five", "week", "free", "binding", "resets", "note")
    heads = {"name": 7, "plan": 4, "five": 6, "week": 4, "free": 4,
             "binding": 2, "resets": 6, "note": 0}
    laid_out = 2 + sum(max(heads[k], *(len(r[k]) for r in rows)) for k in keys) \
        + 2 * (len(keys) - 1)
    spare = shutil.get_terminal_size((80, 24)).columns - laid_out
    return module.bar_width_for(spare // 2 - 1)


def cmd_usage(args) -> int:
    module = sibling("usage")
    if module is None:
        die("usage.py is missing from beside cca.py")
    reports = usage_reports(
        [args.name] if args.name else None,
        getattr(args, "tool", None),
        live=not args.cached,
        timeout=args.timeout,
    )
    if not reports:
        print("no accounts to measure")
        return 0
    if args.json:
        print(json.dumps(reports, indent=2, sort_keys=True))
        return 0

    top = module.best(reports)
    best_name = (top or {}).get("account")
    ordered = module.rank(reports) if args.sort else reports
    # Lay the table out once with no bars to learn what the other columns
    # need, then spend what is left over on the two bars.
    rows = [_usage_row(module, r) for r in ordered]
    bar_width = 0 if args.no_bars else _usage_bar_width(module, rows)
    if bar_width:
        rows = [_usage_row(module, r, bar_width) for r in ordered]

    cols = [("name", "ACCOUNT"), ("plan", "PLAN"), ("five", "5-HOUR"),
            ("week", "WEEK"), ("free", "FREE"), ("binding", "IN"),
            ("resets", "RESETS"), ("note", "")]
    # Percentages are read by comparing them down the column, which only works
    # if their digits line up. A bar is read from its left edge instead, so a
    # column that has one is left-aligned and its header sits over the bar.
    right = {"free", "resets"} | ({"five", "week"} if not bar_width else set())
    widths = {k: max(len(h), *(len(r[k]) for r in rows)) for k, h in cols}

    def fit(key, text):
        return text.rjust(widths[key]) if key in right else text.ljust(widths[key])

    print(f"{c.bold}  {'  '.join(fit(k, h) for k, h in cols).rstrip()}{c.reset}")
    for row in rows:
        line = "  ".join(fit(k, row[k]) for k, _ in cols).rstrip()
        mark = "*" if row["name"] == best_name else " "
        color = c.green if row["name"] == best_name else (c.yellow if row["note"] else "")
        print(f"{color}{mark} {line}{c.reset}")
    print(f"\n{c.dim}FREE = headroom in the tighter window (IN), which is the one "
          f"that will stop you.\n* = most headroom; launch it with "
          f"`{args.entry} best --launch`{c.reset}")
    return 0


def cmd_best(args) -> int:
    """The account with the most headroom — printed, or launched."""
    module = sibling("usage")
    if module is None:
        die("usage.py is missing from beside cca.py")
    reports = usage_reports(tool=args.tool or (args.entry_tool if args.launch else None),
                            live=not args.cached, timeout=args.timeout)
    top = module.best(reports)
    if top is None:
        die("no account could be measured — try `%s usage` to see why" % args.entry)

    if not args.launch:
        if args.json:
            print(json.dumps(top, indent=2, sort_keys=True))
        elif args.quiet:
            print(top["account"])
        else:
            window = module.SHORT.get(top["binding"], "?")
            print(f"{top['account']}  ({top['free_pct']:.0f}% free in the {window} "
                  f"window, the tighter one)")
        return 0

    reg = Registry.load()
    acct = reg.get(top["account"])
    return cmd_run(argparse.Namespace(
        name=acct.name, dry_run=args.dry_run, keep_env=False,
        tool_args=list(args.tool_args), entry=entry_for(acct.tool_name),
        entry_tool=acct.tool_name))


def cmd_pick(args) -> int:
    """The chooser: every account with its usage, arrow keys, Enter launches."""
    module, ui = sibling("usage"), sibling("picker")
    if module is None or ui is None:
        die("usage.py / picker.py are missing from beside cca.py")

    def load():
        return usage_reports(tool=args.tool, live=not args.cached, timeout=args.timeout)

    try:
        chosen = ui.run(load, active=current_account_name(Registry.load()))
    except RuntimeError as exc:
        die(str(exc))
    if chosen is None:
        return 130

    acct = Registry.load().get(chosen)
    return cmd_run(argparse.Namespace(
        name=acct.name, dry_run=args.dry_run, keep_env=False,
        tool_args=list(args.tool_args), entry=entry_for(acct.tool_name),
        entry_tool=acct.tool_name))


def cmd_env(args) -> int:
    """Emit shell exports so an account can be activated in the current shell."""
    reg = Registry.load()
    acct = reg.get(args.name)
    var = acct.tool.config_env
    if acct.legacy_default:
        print(f"unset {var}")
    else:
        print(f"export {var}={acct.config_dir}")
    print(f"export CCA_ACCOUNT={acct.name}")
    print(f"export CCA_TOOL={acct.tool_name}")
    for k, v in acct.env.items():
        print(f"export {k}={v}")
    return 0


def cmd_tools(args) -> int:
    for name, tool in sorted(TOOLS.items()):
        print(f"{c.bold}{entry_for(name)}{c.reset}  ->  {name}")
        print(f"  binary       : {which(tool.binary) or c.yellow + 'not on PATH' + c.reset}")
        print(f"  config var   : {tool.config_env}")
        print(f"  default dir  : {tilde(tool.default_dir)}")
        print(f"  credentials  : <space>/{tool.credentials_name}")
        print(f"  shareable    : {', '.join(tool.linkable)}")
        print(f"  legacy quirk : "
              f"{'yes - config lives at ~/.claude.json' if tool.supports_legacy_default else 'no'}")
        print()
    return 0


# --------------------------------------------------------------------------
# plumbing
# --------------------------------------------------------------------------

def current_account_name(reg: Registry) -> str | None:
    """Which registered account backs this shell?"""
    marked = os.environ.get("CCA_ACCOUNT")
    if marked and marked in reg.accounts:
        return marked

    for tool in TOOLS.values():
        cfg = os.environ.get(tool.config_env)
        if not cfg:
            continue
        path = Path(cfg).expanduser()
        for name, acct in reg.accounts.items():
            if (acct.tool_name == tool.name and acct.config_dir == path
                    and not acct.legacy_default):
                return name
        return None

    for name, acct in reg.accounts.items():
        if acct.legacy_default:
            return name
    return None


SUBCOMMANDS = {
    "list", "ls", "run", "exec", "add", "rm", "remove", "rename", "status",
    "which", "default", "login", "logout", "link", "unlink", "doctor",
    "env", "tools", "alias", "help", "usage", "best", "pick",
}


def build_parser(entry: str, entry_tool: str) -> argparse.ArgumentParser:
    other = "cxa" if entry == "cca" else "cca"
    p = argparse.ArgumentParser(
        prog=entry,
        description=f"Launch and manage {entry_tool} accounts "
                    f"(`{other}` does the same for the other agent).",
        epilog=(
            f"{entry}                      launch the default {entry_tool} account\n"
            f"{entry} <account> [args]     launch that account, args go to {entry_tool}\n"
            f"{entry} list                 every account, both agents\n"
            f"{entry} pick                 choose one from a list, with live usage\n"
            f"{entry} usage                what each account has left\n"
            f"{entry} best --launch        start whichever has the most left\n"
            f"\ncca's own flags go before the account name: "
            f"{entry} run --dry-run <account>"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("list", aliases=["ls"], help="list accounts")
    s.add_argument("--json", action="store_true")
    s.add_argument("--du", action="store_true", help="measure disk usage (slow)")
    s.add_argument("--tool", choices=sorted(TOOLS), help="only this agent")
    s.set_defaults(func=cmd_list)

    s = sub.add_parser("usage", help="how much of each account's limits is spent")
    s.add_argument("name", nargs="?", help="only this account")
    s.add_argument("--json", action="store_true")
    s.add_argument("--tool", choices=sorted(TOOLS), help="only this agent")
    s.add_argument("--sort", action="store_true", help="most headroom first")
    s.add_argument("--no-bars", action="store_true",
                   help="percentages only, for a narrow terminal or a pipe")
    s.add_argument("--cached", action="store_true",
                   help="do not call the API; report the last known figures")
    s.add_argument("--timeout", type=float, default=None,
                   help="seconds to wait per account (default 6)")
    s.set_defaults(func=cmd_usage)

    s = sub.add_parser("best", help="the account with the most headroom left")
    s.add_argument("--launch", action="store_true", help="start it, don't just name it")
    s.add_argument("-q", "--quiet", action="store_true", help="print only the name")
    s.add_argument("--json", action="store_true")
    s.add_argument("--tool", choices=sorted(TOOLS), help="only this agent")
    s.add_argument("--cached", action="store_true", help="skip the API")
    s.add_argument("--timeout", type=float, default=None)
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("tool_args", nargs=argparse.REMAINDER)
    s.set_defaults(func=cmd_best)

    s = sub.add_parser("pick", help="choose an account from a list, with live usage")
    s.add_argument("--tool", choices=sorted(TOOLS), help="only this agent")
    s.add_argument("--cached", action="store_true", help="skip the API")
    s.add_argument("--timeout", type=float, default=None)
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("tool_args", nargs=argparse.REMAINDER)
    s.set_defaults(func=cmd_pick)

    s = sub.add_parser("run", help=f"launch a {entry_tool} account")
    s.add_argument("name")
    s.add_argument("--dry-run", action="store_true", help="print the command instead")
    s.add_argument("--keep-env", action="store_true",
                   help="keep inherited agent session vars")
    s.add_argument("tool_args", nargs=argparse.REMAINDER)
    s.set_defaults(func=cmd_run)

    s = sub.add_parser("exec", help="run any command in an account's environment")
    s.add_argument("name")
    s.add_argument("--keep-env", action="store_true")
    s.add_argument("command", nargs=argparse.REMAINDER)
    s.set_defaults(func=cmd_exec)

    s = sub.add_parser("add", help=f"create a new {entry_tool} account space")
    s.add_argument("name")
    s.add_argument("--tool", choices=sorted(TOOLS),
                   help=f"which agent (default {entry_tool}, from `{entry}`)")
    s.add_argument("--dir", help=f"space directory (default {tilde(SPACES)}/<name>)")
    s.add_argument("--description", help="free text shown in listings")
    s.add_argument("--alias", help="short command (default cc/cx + a letter)")
    s.add_argument("--login", action="store_true", help="log in right after creating")
    s.set_defaults(func=cmd_add)

    s = sub.add_parser("alias", help="show, set or repair the short commands")
    s.add_argument("name", nargs="?")
    s.add_argument("--set", help="use this shortcut ('-' to remove it)")
    s.add_argument("--repair", action="store_true",
                   help="regenerate every shortcut from scratch")
    s.set_defaults(func=cmd_alias)

    s = sub.add_parser("rm", aliases=["remove"], help="remove an account")
    s.add_argument("name")
    s.add_argument("--purge", action="store_true", help="also delete its space")
    s.add_argument("--yes", action="store_true", help="skip the purge confirmation")
    s.set_defaults(func=cmd_rm)

    s = sub.add_parser("rename", help="rename an account")
    s.add_argument("old")
    s.add_argument("new")
    s.set_defaults(func=cmd_rename)

    s = sub.add_parser("status", help="authoritative auth status")
    s.add_argument("name", nargs="?")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("which", help="which account backs this shell")
    s.set_defaults(func=cmd_which)

    s = sub.add_parser("default", help="account that bare `cca`/`cxa` launches")
    s.add_argument("name", nargs="?")
    s.set_defaults(func=cmd_default)

    s = sub.add_parser("login", help="log an account in")
    s.add_argument("name")
    s.set_defaults(func=cmd_login)

    s = sub.add_parser("logout", help="log an account out")
    s.add_argument("name")
    s.set_defaults(func=cmd_logout)

    s = sub.add_parser("link", help="share an asset from another account")
    s.add_argument("name")
    s.add_argument("asset")
    s.add_argument("--from", dest="source", required=True, help="source account")
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_link)

    s = sub.add_parser("unlink", help="remove a shared asset link")
    s.add_argument("name")
    s.add_argument("asset")
    s.set_defaults(func=cmd_unlink)

    s = sub.add_parser("doctor", help="health-check every account")
    s.set_defaults(func=cmd_doctor)

    s = sub.add_parser("env", help="print shell exports for an account")
    s.add_argument("name")
    s.set_defaults(func=cmd_env)

    s = sub.add_parser("tools", help="show what each entry point drives")
    s.set_defaults(func=cmd_tools)

    return p


def main(argv: list[str] | None = None, entry: str | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if entry is None:
        entry = Path(sys.argv[0]).name

    # Invoked through an account shortcut (ccw, ccp, ccc, ...): that account is
    # the whole command, and every argument belongs to the agent.
    if entry not in ENTRYPOINTS:
        acct = Registry.load().by_alias(entry)
        if acct:
            return cmd_run(argparse.Namespace(
                name=acct.name, dry_run=False, keep_env=False, tool_args=argv,
                entry=entry, entry_tool=acct.tool_name))

    entry_tool = ENTRYPOINTS.get(entry, DEFAULT_TOOL)
    if entry not in ENTRYPOINTS:
        entry = entry_for(entry_tool)

    # Bare `cca` / `cxa`: launch that agent's default account.
    if not argv:
        return launch_default(entry, entry_tool, [])

    # `cca personal -c` -> `cca run personal -c`
    if argv[0] not in SUBCOMMANDS and not argv[0].startswith("-"):
        reg_data = read_json(REGISTRY) or {}
        if argv[0] in (reg_data.get("accounts") or {}):
            argv = ["run"] + argv

    parser = build_parser(entry, entry_tool)
    args = parser.parse_args(argv)
    args.entry = entry
    args.entry_tool = entry_tool
    if not getattr(args, "cmd", None):
        return launch_default(entry, entry_tool, [])

    # argparse.REMAINDER keeps a leading "--"; the tool does not want it.
    for attr in ("tool_args", "command"):
        vals = getattr(args, attr, None)
        if vals and vals[0] == "--":
            setattr(args, attr, vals[1:])

    return args.func(args) or 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
