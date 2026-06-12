#!/usr/bin/env python3
"""ECHO interactive installer. Run via: curl -fsSL .../install.sh | bash"""

# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx"]
# ///

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

# ── Colors ──────────────────────────────────────────────────

GREEN = "\033[0;32m"
YELLOW = "\033[1;33m"
RED = "\033[0;31m"
BOLD = "\033[1m"
DIM = "\033[2m"
NC = "\033[0m"


def info(msg: str) -> None:
    print(f"  {msg}")


def ok(msg: str) -> None:
    print(f"  {GREEN}✓{NC} {msg}")


def warn(msg: str) -> None:
    print(f"  {YELLOW}⚠{NC} {msg}")


def die(msg: str) -> None:
    print(f"  {RED}Error: {msg}{NC}", file=sys.stderr)
    sys.exit(1)


def ask(prompt: str, default: str = "") -> str:
    display = f" [{default}]" if default else ""
    try:
        value = input(f"  {prompt}{display}: ").strip()
        return value if value else default
    except (EOFError, KeyboardInterrupt):
        print()
        return default


def ask_yn(prompt: str, default: bool = True) -> bool:
    hint = "Y/n" if default else "y/N"
    try:
        value = input(f"  {prompt} ({hint}): ").strip().lower()
        if not value:
            return default
        return value in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        print()
        return default


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, **kwargs)


def banner() -> None:
    print()
    print(f"  {BOLD}ECHO{NC} - Explore Calls, Hearings & Observations")
    print(f"  {DIM}Search your Zoom meeting transcripts from any MCP-compatible AI tool.{NC}")
    print()


# ── Step 1: Install location ────────────────────────────────

def step_install(home: Path) -> Path:
    print(f"{BOLD}Step 1: Install location{NC}")
    default = str(home / "echo-mcp")
    install_dir = Path(ask("Install directory", default))

    if (install_dir / ".git").exists():
        info(f"{YELLOW}Existing installation found. Updating...{NC}")
        run(["git", "-C", str(install_dir), "pull", "--ff-only"], capture_output=True)
    else:
        info("Cloning ECHO...")
        run(["git", "clone", "--quiet", "https://github.com/Percona-Lab/ECHO.git", str(install_dir)])

    ok(f"Installed at {install_dir}")
    return install_dir


# ── Step 2: Dependencies ────────────────────────────────────

def step_deps(install_dir: Path) -> None:
    print()
    print(f"{BOLD}Step 2: Dependencies{NC}")
    info("Installing Python dependencies...")
    run(["uv", "sync", "--quiet"], cwd=install_dir)
    ok("Dependencies installed")


# ── Step 3: Zoom OAuth ──────────────────────────────────────

def _resolve_registry(org_slug: str) -> dict | None:
    """Look up an org in the registry and return {client_id, bff_url} or None."""
    try:
        import httpx
        resp = httpx.get(
            "https://raw.githubusercontent.com/Percona-Lab/ECHO/main/client_registry.json",
            timeout=5,
        )
        if resp.status_code != 200:
            return None
        entry = resp.json().get("orgs", {}).get(org_slug)
        if entry is None:
            return None
        if isinstance(entry, str):
            return {"client_id": entry, "bff_url": None}
        return {
            "client_id": entry.get("client_id", ""),
            "bff_url": entry.get("bff_url"),
        }
    except Exception:
        return None


def _write_env(env_file: Path, settings: dict) -> None:
    """Write key=value lines to .env, preserving order."""
    env_file.write_text(
        "".join(f"{k}={v}\n" for k, v in settings.items() if v is not None)
    )


def step_zoom_oauth(install_dir: Path) -> str | None:
    print()
    print(f"{BOLD}Step 3: Zoom OAuth Setup{NC}")

    env_file = install_dir / ".env"

    # Check for existing config
    if env_file.exists():
        existing = {}
        for line in env_file.read_text().splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                existing[k.strip()] = v.strip()
        if existing.get("ZOOM_SUBDOMAIN") or (
            existing.get("ZOOM_CLIENT_ID")
            and not existing["ZOOM_CLIENT_ID"].startswith("your_")
        ):
            summary = existing.get("ZOOM_SUBDOMAIN") or (existing.get("ZOOM_CLIENT_ID", "")[:8] + "...")
            ok(f"ECHO already configured for: {DIM}{summary}{NC}")
            if ask_yn("Keep existing config?"):
                return existing.get("ZOOM_CLIENT_ID") or existing.get("ZOOM_SUBDOMAIN")

    # Ask for Zoom subdomain and look up in registry
    org_slug = ask("Your Zoom subdomain (the prefix before .zoom.us, e.g. acme)", "")

    settings: dict[str, str] = {}

    if org_slug:
        # Clean up in case they entered the full domain
        org_slug = org_slug.replace("https://", "").replace(".zoom.us", "").strip().lower()
        entry = _resolve_registry(org_slug)

        if entry and entry["client_id"]:
            ok(f"Found registered config for {BOLD}{org_slug}{NC}")
            # Store the subdomain; the server resolves client_id + bff_url
            # from the registry at runtime (so future registry updates flow
            # through without reinstall).
            settings["ZOOM_SUBDOMAIN"] = org_slug
            _write_env(env_file, settings)
            ok("Config saved to .env")
            return org_slug

        warn(f"No registered config found for {BOLD}{org_slug}{NC}")

    # Registry miss — ask if they have a Client ID
    print()
    if ask_yn("Do you have a Zoom OAuth Client ID?", default=False):
        client_id = ask("Zoom OAuth Client ID", "")
        if client_id:
            settings["ZOOM_CLIENT_ID"] = client_id
            # Without a registered BFF, the client will try to contact Zoom
            # directly which requires ZOOM_CLIENT_SECRET too.
            client_secret = ask(
                "Zoom OAuth Client Secret (required without a registered BFF)",
                "",
            )
            if client_secret:
                settings["ZOOM_CLIENT_SECRET"] = client_secret
            _write_env(env_file, settings)
            ok("Config saved to .env")
            return client_id

    print()
    info("You need a Client ID from a Zoom OAuth app before ECHO can work.")
    info("If you have Zoom admin access, create one yourself. Otherwise ask your Zoom admin.")
    print()
    info(f"{BOLD}How to create the Zoom OAuth app:{NC}")
    info(f"  1. Go to https://marketplace.zoom.us > Develop > Build App")
    info(f"  2. Select General App, set redirect URL: http://localhost:8090/callback")
    info(f"  3. Under Scopes, choose User-managed and add:")
    info(f"     cloud_recording:read:content")
    info(f"     cloud_recording:read:list_user_recordings")
    info(f"     meeting:read:summary")
    info(f"     meeting:read:list_meetings")
    info(f"     user:read:user")
    info(f"  4. Activate the app and copy the Client ID")
    print()
    info(f"Full instructions: https://github.com/Percona-Lab/ECHO#prerequisites")
    print()
    info("To register your org so others can skip this step,")
    info("submit a PR adding your Client ID to client_registry.json.")
    return None


# ── Step 4: Authenticate ────────────────────────────────────

def step_auth(install_dir: Path, client_id: str | None) -> None:
    print()
    print(f"{BOLD}Step 4: Zoom Authentication{NC}")

    token_file = Path.home() / ".echo" / "tokens.json"

    if token_file.exists():
        ok("Already authenticated (tokens found at ~/.echo/tokens.json)")
    elif client_id:
        if ask_yn("Log in to Zoom now?"):
            run(["uv", "run", "echo-login"], cwd=install_dir)
        else:
            info(f"{DIM}Run 'uv run echo-login' later to authenticate.{NC}")
    else:
        info(f"{DIM}Skipped (no Client ID configured yet).{NC}")
        info(f"{DIM}After adding your Client ID to .env, run: uv run echo-login{NC}")


# ── Step 5: Configure AI client ─────────────────────────────

def step_configure_client(install_dir: Path) -> None:
    print()
    print(f"{BOLD}Step 5: Configure AI Client{NC}")

    mcp_entry = {
        "type": "stdio",
        "command": "uv",
        "args": ["run", "--directory", str(install_dir), "echo-mcp"],
    }

    # Claude Code
    claude_settings = Path.home() / ".claude" / "settings.json"
    has_claude_code = claude_settings.exists() or _command_exists("claude")

    if has_claude_code:
        if ask_yn("Configure Claude Code?"):
            _merge_mcp_config(claude_settings, "echo", mcp_entry)
            _install_claude_code_commands(install_dir)
            ok("Claude Code configured")

    # Claude Desktop
    claude_desktop = _find_claude_desktop_config()
    if claude_desktop and claude_desktop.exists():
        if ask_yn("Configure Claude Desktop?"):
            _merge_mcp_config(claude_desktop, "echo", mcp_entry)
            ok("Claude Desktop configured (restart to apply)")

    if not has_claude_code and not (claude_desktop and claude_desktop.exists()):
        info("No AI clients detected. Add ECHO to your MCP client config manually:")
        info(f'{DIM}  "echo": {json.dumps(mcp_entry)}{NC}')


def _install_claude_code_commands(install_dir: Path) -> None:
    """Copy ECHO's slash-command wrappers into ~/.claude/commands/.

    MCP prompts don't surface as slash commands in Claude Code, so we
    install thin Markdown command wrappers that invoke the MCP tools.
    """
    src = install_dir / "commands"
    if not src.exists():
        return
    dst = Path.home() / ".claude" / "commands"
    dst.mkdir(parents=True, exist_ok=True)
    import shutil
    count = 0
    for cmd in src.glob("echo-*.md"):
        shutil.copy2(cmd, dst / cmd.name)
        count += 1
    if count:
        info(f"Installed {count} slash commands to {dst}")


def _merge_mcp_config(path: Path, name: str, entry: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        cfg = json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        cfg = {}
    cfg.setdefault("mcpServers", {})[name] = entry
    path.write_text(json.dumps(cfg, indent=2) + "\n")
    info(f"Updated {path}")


def _find_claude_desktop_config() -> Path | None:
    import platform

    if platform.system() == "Darwin":
        p = Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    else:
        p = Path.home() / ".config" / "Claude" / "claude_desktop_config.json"
    return p if p.exists() else None


def _command_exists(cmd: str) -> bool:
    try:
        run(["which", cmd], capture_output=True)
        return True
    except Exception:
        return False


# ── Done (no Client ID) ─────────────────────────────────────

def step_done_no_client(install_dir: Path) -> None:
    print()
    print(f"  {YELLOW}{BOLD}ECHO partially installed.{NC}")
    print()
    info("ECHO needs a Zoom OAuth Client ID to work.")
    info("Once you have one, re-run the installer:")
    print()
    info(f"  {BOLD}curl -fsSL https://raw.githubusercontent.com/Percona-Lab/ECHO/main/install.sh | bash{NC}")
    print()
    info(f"{DIM}How to create a Zoom OAuth app: https://github.com/Percona-Lab/ECHO#prerequisites{NC}")
    print()


# ── Done ────────────────────────────────────────────────────

def step_done(install_dir: Path) -> None:
    token_file = Path.home() / ".echo" / "tokens.json"

    print()
    print(f"  {GREEN}{BOLD}ECHO installed!{NC}")
    print()
    info(f"{DIM}Location:{NC}    {install_dir}")
    if token_file.exists():
        info(f"{DIM}Auth:{NC}        Authenticated")
    else:
        info(f"{DIM}Auth:{NC}        Run 'uv run echo-login' to connect your Zoom account")
    print()
    info(f'{DIM}Usage:{NC}       Ask your AI assistant about your Zoom meetings.')
    info(f'{DIM}Example:{NC}     "Search my Zoom calls for quarterly roadmap"')
    print()


# ── Main ────────────────────────────────────────────────────

def main() -> None:
    # Always reopen stdin from /dev/tty. When run via `curl | bash`,
    # stdin may be the pipe or an intermediary that doesn't block on input.
    # Opening /dev/tty directly guarantees we read from the real terminal.
    try:
        sys.stdin = open("/dev/tty")
    except OSError:
        pass  # Windows or no tty available, fall through to defaults

    banner()
    home = Path.home()
    install_dir = step_install(home)
    step_deps(install_dir)
    client_id = step_zoom_oauth(install_dir)

    if not client_id:
        # Can't continue without a Client ID. Tell them how to resume.
        step_done_no_client(install_dir)
        return

    step_auth(install_dir, client_id)
    step_configure_client(install_dir)
    step_done(install_dir)


if __name__ == "__main__":
    main()
