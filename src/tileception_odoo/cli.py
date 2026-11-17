"""Command line and user installation, with no Tileception build dependency."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from .config import (
    DEFAULT_WEB_URL,
    NO_PROJECT,
    Config,
    Paths,
    default_json2_url,
    format_project_list,
    private_directory,
    replace_assignment,
    validate_url,
    write_private,
)
from .setup import (
    SERVICE_NAME,
    TIMER_NAME,
    check_outside_repositories,
    configuration_state,
    connection,
    current_settings,
    has_api_key,
    manifest_path,
    save_settings,
    setup_prompt,
    start_background_sync,
    units_directory,
)

SERVICE = """[Unit]
Description=Update private Odoo search cache
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
ExecStart={command} sync
Nice=15
IOSchedulingClass=idle
CPUQuota=50%
CPUWeight=10
UMask=0077
NoNewPrivileges=true
TimeoutStartSec=2h
"""

TIMER = """[Unit]
Description=Refresh Odoo launcher search every 15 minutes

[Timer]
OnCalendar=*:0/15
RandomizedDelaySec=2m
Persistent=true
Unit=tileception-odoo-sync.service

[Install]
WantedBy=timers.target
"""

CONFIG_EXAMPLE = f"""# API key lives in the separate, private api-key file in this directory.
web_url = "{DEFAULT_WEB_URL}"
json2_url = "{default_json2_url(DEFAULT_WEB_URL)}"
database = ""
threads = 2
debounce_ms = 180
# Odoo project IDs; 0 means tasks without a project. Edit with: tileception-odoo projects
# include_projects = []  # empty: all projects
# exclude_projects = []  # always wins over include_projects
"""


def unit_quote(value: str) -> str:
    # ExecStart is parsed by systemd, including specifier and environment expansion.
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$") + '"'


def setup_model(paths: Paths):
    from .embeddings import download
    from .sync import writer_lock

    print("Preparing the local embedding model; the first download is about 400 MB.", flush=True)
    with writer_lock(paths):
        download(private_directory(paths.data) / "model")
    print("Local embedding model is ready", flush=True)


def initial_sync(paths: Paths):
    """Wait for the limited user service while forwarding its private progress."""
    from .progress import read_progress

    command = ["systemctl", "--user", "start", "tileception-odoo-sync.service"]
    previous = read_progress(paths)
    previous_update = previous.get("updated") if previous else None
    previous_phase = previous.get("phase") if previous else None
    last_output = 0.0
    with subprocess.Popen(command) as process:
        while True:
            try:
                result = process.wait(timeout=0.25)
            except subprocess.TimeoutExpired:
                result = None
            progress = read_progress(paths)
            now = time.monotonic()
            if (
                progress
                and progress.get("updated") != previous_update
                and (result is not None or progress.get("phase") != previous_phase or now - last_output >= 2)
            ):
                print(progress["message"], flush=True)
                previous_update = progress.get("updated")
                previous_phase = progress.get("phase")
                last_output = now
            if result is not None:
                if result:
                    raise subprocess.CalledProcessError(result, command)
                return


def install(paths: Paths, enable_timer: bool, non_interactive: bool = False):
    interactive = sys.stdin.isatty() and not non_interactive
    command = shutil.which("tileception-odoo")
    if not command:
        raise ValueError("Install the package first with uv tool install .")
    command = str(Path(command).absolute())
    for directory in (paths.config, paths.cache, paths.data):
        private_directory(directory)
    config = paths.config / "config.toml"
    if not config.exists():
        config.write_text(CONFIG_EXAMPLE)
        config.chmod(0o600)
    # The user service cannot rely on this terminal's Odoo environment variables.
    configured, detail = configuration_state(paths)
    if configured:
        print("Using your existing Odoo configuration", flush=True)
    elif interactive:
        print("Set up your Odoo connection first.", flush=True)
        configure(paths)
        configured = True
    setup_model(paths)
    units = units_directory(paths)
    units.mkdir(parents=True, exist_ok=True)
    (units / SERVICE_NAME).write_text(SERVICE.format(command=unit_quote(command)))
    (units / TIMER_NAME).write_text(TIMER)
    manifest = manifest_path(paths)
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        f'command = {json.dumps(command)}\nargs = ["serve"]\n[launcher]\nprefixes = ["odoo"]\n'
    )
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    if not interactive:
        if enable_timer:
            subprocess.run(["systemctl", "--user", "enable", "--now", TIMER_NAME], check=True)
        if not configured:
            # Tileception reads `status` next and asks for the connection; configure starts the sync.
            print(f"Odoo search is installed but not set up yet: {detail}.", flush=True)
            print("Set it up with: tileception-odoo configure", flush=True)
            return
        start_background_sync()
        print(
            "Initial Odoo synchronization started in the background; "
            "check progress with tileception-odoo status.",
            flush=True,
        )
        print("Odoo search is installed. Restart Tileception to discover the plugin.")
        return
    print("Running the initial Odoo synchronization; this may take a while.", flush=True)
    try:
        initial_sync(paths)
    except subprocess.CalledProcessError:
        detail = ""
        if paths.database.exists():
            from .store import Store

            try:
                store = Store(paths.database)
                try:
                    if error := store.get("last_error"):
                        detail = f" Last recorded sync error: {error}."
                finally:
                    store.close()
            except OSError, sqlite3.Error:
                pass
        raise ValueError(
            f"Initial synchronization failed.{detail} Check tileception-odoo status or "
            "journalctl --user -u tileception-odoo-sync.service, then rerun install. "
            "Your configuration and downloaded model have been kept."
        ) from None
    if not enable_timer:
        enable_timer = input("Enable the 15-minute Odoo sync timer now? [y/N] ").strip().lower() in {
            "y",
            "yes",
        }
    if enable_timer:
        subprocess.run(["systemctl", "--user", "enable", "--now", TIMER_NAME], check=True)
    print("Odoo search is ready. Restart Tileception to discover the plugin.")


def prompt_url(label: str, example: str, default: str = "") -> str:
    while True:
        try:
            prompt = f"{label} [{default}]: " if default else f"{label} ({example}): "
            return validate_url(input(prompt).strip() or default, label, example)
        except ValueError as error:
            print(error, file=sys.stderr)


def configure(paths: Paths):
    from .odoo import OdooClient

    private_directory(paths.config)
    check_outside_repositories(paths)
    web, endpoint, database = current_settings(paths)
    keep_key = has_api_key(paths)
    while True:
        web = prompt_url("Odoo web URL", DEFAULT_WEB_URL, default=web)
        derived = default_json2_url(web)
        endpoint = prompt_url("JSON-2 URL", derived, default=endpoint or derived)
        database = input(f"Odoo database (optional) [{database}]: ").strip() or database
        label = "Odoo read-only API key" + (" (Enter keeps the current one)" if keep_key else "") + ": "
        key = getpass.getpass(label, echo_char="*").strip() or None
        if key is None and not keep_key:
            print("API key must not be empty", file=sys.stderr)
            continue
        print("Checking the connection...", flush=True)
        try:
            config = connection(paths, web, endpoint, database)
            OdooClient(config, key=key).check()
        except ValueError as error:
            print(f"{error}. Try again.", file=sys.stderr)
            continue
        break
    save_settings(paths, config, key)
    print(f"Saved private configuration in {paths.config}")


def configure_command(paths: Paths):
    """`configure` on its own, as Tileception runs it: also start the first sync."""
    configure(paths)
    if (units_directory(paths) / SERVICE_NAME).exists():
        start_background_sync()
        print("Odoo synchronization started in the background; check it with tileception-odoo status.")
    else:
        print("Run tileception-odoo install to install the sync service.")


def status_command(paths: Paths) -> dict:
    """Tileception's `status`: whether setup is needed, and the launcher prompt that does it."""
    from .server import Server

    configured, detail = configuration_state(paths)
    status = {"configured": configured, "detail": detail, "setup": setup_prompt(paths)}
    status.update(Server(paths).status())
    return status


def uninstall(paths: Paths, purge: bool):
    """Undo install: stop and remove the user units and the Tileception manifest."""
    units = units_directory(paths)
    if (units / TIMER_NAME).exists():
        subprocess.run(["systemctl", "--user", "disable", "--now", TIMER_NAME], check=False, timeout=30)
    if (units / SERVICE_NAME).exists():
        subprocess.run(["systemctl", "--user", "stop", "--no-block", SERVICE_NAME], check=False, timeout=30)
    for unit in (TIMER_NAME, SERVICE_NAME):
        (units / unit).unlink(missing_ok=True)
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=False, timeout=30)
    manifest_path(paths).unlink(missing_ok=True)
    if purge:
        for directory in (paths.config, paths.cache, paths.data):
            if directory.is_dir() and not directory.is_symlink():
                shutil.rmtree(directory)
        print("Removed Odoo search, its configuration, API key, cache and model.")
    else:
        print(f"Removed Odoo search; configuration stays in {paths.config} (remove it with --purge).")


def choose_projects(paths: Paths):
    from .odoo import OdooClient
    from .picker import Entry, pick

    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise ValueError(
            "The project picker needs an interactive terminal; edit include_projects in config.toml"
        )
    settings = Config.load(paths)
    print("Fetching projects from Odoo...", flush=True)
    rows = OdooClient(settings).projects()
    names = {row["id"]: str(row.get("name") or "Untitled") for row in rows}
    entries = [Entry(NO_PROJECT, "(no project)", "tasks without a project")]
    entries += [Entry(project, name) for project, name in names.items()]
    # Keep IDs that Odoo no longer lists, so saving never drops them silently.
    entries += [
        Entry(project, "Unknown project", "archived or inaccessible")
        for project in sorted(settings.include_projects - names.keys() - {NO_PROJECT})
    ]
    names[NO_PROJECT] = "tasks without a project"
    chosen = pick(entries, set(settings.include_projects), set(settings.exclude_projects))
    if chosen is None or chosen == settings.include_projects:
        print("Project selection unchanged")
        return
    filename = paths.config / "config.toml"
    text = filename.read_text() if filename.exists() else CONFIG_EXAMPLE
    write_private(
        filename,
        replace_assignment(text, "include_projects", format_project_list("include_projects", chosen, names)),
    )
    if chosen:
        print(f"Saved {len(chosen)} project(s) to synchronize in {filename}")
    else:
        print(f"Saved: all projects will be synchronized ({filename})")
    if settings.exclude_projects & chosen:
        print("Note: some selected projects are also in exclude_projects, which wins.")
    if input("Synchronize now to apply the selection? [Y/n] ").strip().lower() not in {"", "y", "yes"}:
        print("The next sync applies the new selection.")
        return
    if (paths.config.parent / "systemd/user/tileception-odoo-sync.service").exists():
        initial_sync(paths)
    else:
        from .sync import synchronize

        synchronize(paths, Config.load(paths))


def main():
    os.umask(0o077)
    # Bound numerical-library pools as well as ONNX Runtime's own inference pools.
    for variable in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
        os.environ[variable] = "1"
    parser = argparse.ArgumentParser(description="Private local Odoo launcher search")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("serve", "sync", "configure", "setup-model", "status"):
        sub.add_parser(name)
    remover = sub.add_parser("uninstall", help="stop and remove the sync service and plugin manifest")
    remover.add_argument("--purge", action="store_true", help="also remove configuration, API key and cache")
    sub.add_parser("projects", help="choose which Odoo projects to synchronize")
    installer = sub.add_parser("install")
    installer.add_argument("--enable-timer", action="store_true")
    installer.add_argument(
        "--non-interactive",
        action="store_true",
        help="never prompt, and start the first sync in the background instead of waiting for it",
    )
    args = parser.parse_args()
    paths = Paths.discover()
    try:
        if args.command == "serve":
            from .server import Server

            Server(paths).run()
        elif args.command == "sync":
            from .sync import synchronize

            os.nice(10)
            synchronize(paths, Config.load(paths))
        elif args.command == "setup-model":
            setup_model(paths)
        elif args.command == "install":
            install(paths, enable_timer=args.enable_timer, non_interactive=args.non_interactive)
        elif args.command == "configure":
            configure_command(paths)
        elif args.command == "uninstall":
            uninstall(paths, purge=args.purge)
        elif args.command == "projects":
            choose_projects(paths)
        elif args.command == "status":
            print(json.dumps(status_command(paths), indent=2))
    except EOFError, KeyboardInterrupt:
        print("Configuration cancelled", file=sys.stderr)
        raise SystemExit(130) from None
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"tileception-odoo: {error}", file=sys.stderr)
        raise SystemExit(1) from None
