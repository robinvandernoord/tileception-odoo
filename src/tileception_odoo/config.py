"""Per-user paths and configuration, independent of the Codex Odoo skill."""

from __future__ import annotations

import json
import os
import re
import stat
import tomllib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

DEFAULT_WEB_URL = "https://educationwarehouse.nl"
# Pseudo-project ID for tasks without a project; Odoo record IDs start at 1.
NO_PROJECT = 0
PROJECT_LIST_KEYS = ("include_projects", "exclude_projects")


def validate_url(value: str, label: str, example: str) -> str:
    value = value.strip().rstrip("/")
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        _ = parsed.port  # Validate malformed or out-of-range ports too.
    except ValueError:
        raise ValueError(f"{label} is not a valid URL. Example: {example}") from None
    if parsed.scheme != "https" or not hostname:
        raise ValueError(f"{label} must start with https:// and include a hostname. Example: {example}")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(f"{label} must not include a username or password. Enter the API key separately.")
    if parsed.query or parsed.fragment:
        raise ValueError(f"{label} must be a base address without the ? or # part. Example: {example}")
    return value


def base_web_url(web: str) -> str:
    parts = urlsplit(web)
    path = parts.path.rstrip("/").removesuffix("/odoo").removesuffix("/web")
    return parts._replace(path=path).geturl()


def default_json2_url(web: str) -> str:
    return f"{base_web_url(web)}/json/2"


def private_directory(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or path.stat().st_uid != os.getuid():
        raise ValueError(f"Not an owned private directory: {path}")
    path.chmod(0o700)
    return path


@dataclass(frozen=True)
class Paths:
    config: Path
    cache: Path
    data: Path

    @classmethod
    def discover(cls) -> Paths:
        home = Path.home()
        return cls(
            Path(os.environ.get("XDG_CONFIG_HOME", home / ".config")) / "tileception-odoo",
            Path(os.environ.get("XDG_CACHE_HOME", home / ".cache")) / "tileception-odoo",
            Path(os.environ.get("XDG_DATA_HOME", home / ".local/share")) / "tileception-odoo",
        )

    @property
    def database(self) -> Path:
        return self.cache / "records.sqlite3"


@dataclass(frozen=True)
class Config:
    web_url: str
    json2_url: str
    database: str
    api_key_file: Path
    threads: int = 2
    debounce_ms: int = 180
    include_projects: frozenset[int] = frozenset()
    exclude_projects: frozenset[int] = frozenset()

    @classmethod
    def load(cls, paths: Paths, use_environment: bool = True) -> Config:
        filename = paths.config / "config.toml"
        values = tomllib.loads(filename.read_text()) if filename.exists() else {}
        environment = os.environ if use_environment else {}
        web = validate_url(
            environment.get("ODOO_WEB_URL", values.get("web_url", DEFAULT_WEB_URL)) or DEFAULT_WEB_URL,
            "Odoo web URL (web_url)",
            DEFAULT_WEB_URL,
        )
        endpoint = validate_url(
            environment.get("ODOO_JSON2_URL", values.get("json2_url", "")) or default_json2_url(web),
            "JSON-2 URL (json2_url)",
            default_json2_url(DEFAULT_WEB_URL),
        )
        return cls(
            web_url=base_web_url(web),
            json2_url=endpoint,
            database=environment.get("ODOO_DB", values.get("database", "")),
            api_key_file=Path(values.get("api_key_file", str(paths.config / "api-key"))).expanduser(),
            threads=max(1, min(4, int(values.get("threads", 2)))),
            debounce_ms=max(50, min(1000, int(values.get("debounce_ms", 180)))),
            include_projects=project_ids(values, "include_projects"),
            exclude_projects=project_ids(values, "exclude_projects"),
        )

    def project_filter(self) -> str:
        """Stable fingerprint of the project selection, stored with the cache."""
        return json.dumps([sorted(self.include_projects), sorted(self.exclude_projects)])

    def api_key(self, use_environment: bool = True) -> str:
        if use_environment and (value := os.environ.get("ODOO_API_KEY")):
            return value.strip()
        filename = self.api_key_file
        info = filename.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError(f"API key must be an owned regular file with mode 0600: {filename}")
        # Reject source-tree credentials even before this directory is put under Git.
        for parent in filename.resolve().parents:
            if (parent / ".git").exists() or (parent / "pyproject.toml").exists():
                raise ValueError("Store the API key outside repositories")
        value = filename.read_text().strip()
        if not value:
            raise ValueError("API key file is empty")
        return value


def project_ids(values: dict, key: str) -> frozenset[int]:
    value = values.get(key, [])
    if not isinstance(value, list) or not all(
        isinstance(item, int) and not isinstance(item, bool) and item >= 0 for item in value
    ):
        raise ValueError(
            f"{key} must be a list of Odoo project IDs; use {NO_PROJECT} for tasks without a project"
        )
    return frozenset(value)


def assignment_lines(lines: list[str], key: str) -> range | None:
    """Locate a top-level integer-array assignment, including multi-line arrays with comments."""
    pattern = re.compile(rf"^{re.escape(key)}\s*=")
    for start, line in enumerate(lines):
        if not pattern.match(line):
            continue
        depth = 0
        for end in range(start, len(lines)):
            # Integer arrays contain no strings, so everything after # is a comment.
            code = lines[end].split("#", 1)[0]
            depth += code.count("[") - code.count("]")
            if depth <= 0:
                return range(start, end + 1)
        raise ValueError(f"Unterminated {key} array in config.toml")
    return None


def format_project_list(key: str, ids: set[int], names: dict[int, str]) -> list[str]:
    if not ids:
        return [f"{key} = []"]
    lines = [f"{key} = ["]
    for project in sorted(ids, key=lambda item: (names.get(item, "").casefold(), item)):
        name = " ".join(names.get(project, "").split())
        lines.append(f"    {project},  # {name}" if name else f"    {project},")
    return [*lines, "]"]


def replace_assignment(text: str, key: str, replacement: list[str]) -> str:
    lines = text.splitlines()
    found = assignment_lines(lines, key)
    if found is None:
        # Top-level keys must precede the first [table] header to stay top-level.
        table = next((index for index, line in enumerate(lines) if line.lstrip().startswith("[")), len(lines))
        lines[table:table] = replacement
    else:
        lines[found.start : found.stop] = replacement
    return "\n".join(lines) + "\n"


def preserved_assignments(text: str, keys=PROJECT_LIST_KEYS) -> list[str]:
    """Raw assignment lines for keys, so rewriting other settings keeps hand-written comments."""
    lines = text.splitlines()
    output = []
    for key in keys:
        if (found := assignment_lines(lines, key)) is not None:
            output.extend(lines[found.start : found.stop])
    return output


def write_private(filename: Path, text: str):
    tomllib.loads(text)  # Never replace a working configuration with an unparsable one.
    temporary = filename.with_suffix(".tmp")
    with temporary.open("w") as handle:
        os.fchmod(handle.fileno(), 0o600)
        handle.write(text)
    temporary.replace(filename)
