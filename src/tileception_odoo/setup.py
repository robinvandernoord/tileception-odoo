"""Connection setup shared by the terminal (configure) and Tileception's launcher prompt."""

from __future__ import annotations

import json
import os
import subprocess
import tomllib
from dataclasses import replace
from pathlib import Path

from .config import (
    DEFAULT_WEB_URL,
    Config,
    Paths,
    base_web_url,
    default_json2_url,
    preserved_assignments,
    private_directory,
    validate_url,
    write_private,
)

SERVICE_NAME = "tileception-odoo-sync.service"
TIMER_NAME = "tileception-odoo-sync.timer"
# A connection check must answer before Tileception's prompt deadline (45 s here, 60 s at most).
CHECK_TIMEOUT = 20


def units_directory(paths: Paths) -> Path:
    return paths.config.parent / "systemd/user"


def manifest_path(paths: Paths) -> Path:
    return paths.data.parent / "tileception/plugins/odoo.toml"


def start_background_sync():
    """Queue a sync on the user service without waiting for it."""
    subprocess.run(["systemctl", "--user", "start", "--no-block", SERVICE_NAME], check=True, timeout=10)


def configuration_state(paths: Paths) -> tuple[bool, str]:
    """Whether the user service could synchronize, judged without this shell's environment."""
    try:
        Config.load(paths, use_environment=False).api_key(use_environment=False)
    except FileNotFoundError:
        return False, "Enter your Odoo URL and API key"
    except (OSError, ValueError, tomllib.TOMLDecodeError) as error:
        return False, str(error)
    return True, "Odoo connection is set up"


def has_api_key(paths: Paths) -> bool:
    try:
        Config.load(paths, use_environment=False).api_key(use_environment=False)
    except OSError, ValueError, tomllib.TOMLDecodeError:
        return False
    return True


def current_settings(paths: Paths) -> tuple[str, str, str]:
    """Web URL, explicit JSON-2 URL (empty when derived) and database, for prefilling."""
    try:
        config = Config.load(paths, use_environment=False)
    except OSError, ValueError, tomllib.TOMLDecodeError:
        return DEFAULT_WEB_URL, "", ""
    endpoint = "" if config.json2_url == default_json2_url(config.web_url) else config.json2_url
    return config.web_url, endpoint, config.database


def setup_prompt(paths: Paths) -> dict:
    """The launcher prompt for Tileception (see its docs/plugins.md, Setting up in the launcher)."""
    web, endpoint, database = current_settings(paths)
    keep_key = has_api_key(paths)
    return {
        "title": "Set up Odoo search",
        "detail": "Read-only access to projects and tasks",
        "icon": "glyph:globe",
        "steps": [
            {
                "id": "connect",
                "kind": "fields",
                "text": "The key is stored privately in ~/.config/tileception-odoo/api-key",
                "tool": "setup",
                "timeout": 45,
                "submit": "Connect",
                "fields": [
                    {"name": "web_url", "label": "Odoo web URL", "value": web},
                    {"name": "json2_url", "label": "JSON-2 URL (empty: derived)", "value": endpoint},
                    {"name": "database", "label": "Database (optional)", "value": database},
                    {
                        "name": "api_key",
                        "label": "API key (empty: keep current)" if keep_key else "Read-only API key",
                        "secret": True,
                    },
                ],
            }
        ],
    }


def check_outside_repositories(paths: Paths):
    for parent in (paths.config.resolve(), *paths.config.resolve().parents):
        if (parent / ".git").exists() or (parent / "pyproject.toml").exists():
            raise ValueError("Store configuration and credentials outside repositories")


def connection(paths: Paths, web_url: str, json2_url: str, database: str) -> Config:
    """Validated settings as they would be saved, keeping everything else from config.toml."""
    web = validate_url(web_url or DEFAULT_WEB_URL, "Odoo web URL", DEFAULT_WEB_URL)
    endpoint = validate_url(
        json2_url or default_json2_url(web), "JSON-2 URL", default_json2_url(DEFAULT_WEB_URL)
    )
    database = database.strip()
    if len(database) > 255 or any(ord(letter) < 32 for letter in database):
        raise ValueError("Database must be a short name on one line")
    try:
        base = Config.load(paths, use_environment=False)
    except OSError, ValueError, tomllib.TOMLDecodeError:
        base = Config(DEFAULT_WEB_URL, default_json2_url(DEFAULT_WEB_URL), "", paths.config / "api-key")
    return replace(base, web_url=base_web_url(web), json2_url=endpoint, database=database)


def save_settings(paths: Paths, config: Config, key: str | None):
    """Write the connection settings, and the API key when one is given."""
    private_directory(paths.config)
    check_outside_repositories(paths)
    if key is not None:
        key_path = config.api_key_file
        if key_path.is_symlink():
            raise ValueError("API key path must not be a symlink")
        with key_path.open("w") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(key + "\n")
    filename = paths.config / "config.toml"
    # Keep the project selection and a custom key location; neither is asked for here.
    text = filename.read_text() if filename.exists() else ""
    kept = preserved_assignments(text, keys=("include_projects", "exclude_projects", "api_key_file"))
    write_private(
        filename,
        f"web_url = {json.dumps(config.web_url)}\njson2_url = {json.dumps(config.json2_url)}\n"
        f"database = {json.dumps(config.database)}\nthreads = {config.threads}\n"
        f"debounce_ms = {config.debounce_ms}\n" + "".join(line + "\n" for line in kept),
    )


def setup_from_prompt(paths: Paths, arguments: dict) -> str:
    """The launcher prompt's tool: check the connection, save it and start the first sync."""
    from .odoo import OdooClient

    values = {}
    for name in ("web_url", "json2_url", "database", "api_key"):
        value = arguments.get(name, "")
        if not isinstance(value, str) or len(value) > 4096:
            raise ValueError(f"{name} must be text")
        values[name] = value.strip()
    config = connection(paths, values["web_url"], values["json2_url"], values["database"])
    key = values["api_key"] or None
    if key is None and not has_api_key(paths):
        raise ValueError("Enter a read-only Odoo API key")
    client = OdooClient(config, key=key, timeout=CHECK_TIMEOUT)
    client.check()
    save_settings(paths, config, key)
    if (units_directory(paths) / SERVICE_NAME).exists():
        try:
            start_background_sync()
        except OSError, subprocess.SubprocessError:
            return "Odoo is set up; start a sync with tileception-odoo sync"
        return "Odoo is set up; the first sync runs in the background"
    return "Odoo is set up; run tileception-odoo install to synchronize"
