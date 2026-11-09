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
from pathlib import Path

from .config import DEFAULT_WEB_URL, Config, Paths, default_json2_url, private_directory, validate_url

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


def install(paths: Paths, enable_timer: bool):
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
    try:
        # The user service cannot rely on this terminal's Odoo environment variables.
        settings = Config.load(paths, use_environment=False)
        settings.api_key(use_environment=False)
    except OSError, ValueError:
        if not sys.stdin.isatty():
            raise ValueError(
                "Odoo configuration is incomplete. Run install in a terminal to enter your URLs and API key, "
                "or provision the private configuration and key file before unattended installation."
            ) from None
        print("Set up your Odoo connection first.", flush=True)
        configure(paths)
    else:
        print("Using your existing Odoo configuration", flush=True)
    setup_model(paths)
    units = paths.config.parent / "systemd/user"
    units.mkdir(parents=True, exist_ok=True)
    (units / "tileception-odoo-sync.service").write_text(SERVICE.format(command=unit_quote(command)))
    (units / "tileception-odoo-sync.timer").write_text(TIMER)
    manifests = paths.data.parent / "tileception/plugins"
    manifests.mkdir(parents=True, exist_ok=True)
    (manifests / "odoo.toml").write_text(
        f'command = {json.dumps(command)}\nargs = ["serve"]\n[launcher]\nprefixes = ["odoo"]\n'
    )
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    print("Running the initial Odoo synchronization; this may take a while.", flush=True)
    try:
        subprocess.run(["systemctl", "--user", "start", "tileception-odoo-sync.service"], check=True)
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
    if not enable_timer and sys.stdin.isatty():
        enable_timer = input("Enable the 15-minute Odoo sync timer now? [y/N] ").strip().lower() in {
            "y",
            "yes",
        }
    if enable_timer:
        subprocess.run(["systemctl", "--user", "enable", "--now", "tileception-odoo-sync.timer"], check=True)
    print("Odoo search is ready. Restart Tileception to discover the plugin.")


def prompt_url(label: str, example: str, default: str = "") -> str:
    while True:
        try:
            prompt = f"{label} [{default}]: " if default else f"{label} ({example}): "
            return validate_url(input(prompt).strip() or default, label, example)
        except ValueError as error:
            print(error, file=sys.stderr)


def configure(paths: Paths):
    private_directory(paths.config)
    for parent in (paths.config.resolve(), *paths.config.resolve().parents):
        if (parent / ".git").exists() or (parent / "pyproject.toml").exists():
            raise ValueError("Store configuration and credentials outside repositories")
    web = prompt_url("Odoo web URL", DEFAULT_WEB_URL, default=DEFAULT_WEB_URL)
    endpoint = prompt_url("JSON-2 URL", default_json2_url(web), default=default_json2_url(web))
    database = input("Odoo database (optional): ").strip()
    key = getpass.getpass("Odoo read-only API key: ", echo_char="*").strip()
    if not key:
        raise ValueError("API key must not be empty")
    key_path = paths.config / "api-key"
    if key_path.is_symlink():
        raise ValueError("API key path must not be a symlink")
    with key_path.open("w") as handle:
        os.fchmod(handle.fileno(), 0o600)
        handle.write(key + "\n")
    config = paths.config / "config.toml"
    config.write_text(
        f"web_url = {json.dumps(web)}\njson2_url = {json.dumps(endpoint)}\n"
        f"database = {json.dumps(database)}\nthreads = 2\ndebounce_ms = 180\n"
    )
    config.chmod(0o600)
    print(f"Saved private configuration in {paths.config}")


def main():
    os.umask(0o077)
    # Bound numerical-library pools as well as ONNX Runtime's own inference pools.
    for variable in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
        os.environ[variable] = "1"
    parser = argparse.ArgumentParser(description="Private local Odoo launcher search")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("serve", "sync", "configure", "setup-model", "status"):
        sub.add_parser(name)
    installer = sub.add_parser("install")
    installer.add_argument("--enable-timer", action="store_true")
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
            install(paths, enable_timer=args.enable_timer)
        elif args.command == "configure":
            configure(paths)
        elif args.command == "status":
            from .server import Server

            print(json.dumps(Server(paths).status(), indent=2))
    except EOFError, KeyboardInterrupt:
        print("Configuration cancelled", file=sys.stderr)
        raise SystemExit(130) from None
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"tileception-odoo: {error}", file=sys.stderr)
        raise SystemExit(1) from None
