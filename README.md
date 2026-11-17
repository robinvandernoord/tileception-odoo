# tileception-odoo

Local Odoo task and project search for Tileception. Type `odoo query` to search,
or `odoo ` for sync status and **Sync Odoo now**. Enter opens a record in Odoo.

Requires Linux, Python 3.14+, systemd user services and Odoo 19+ with JSON-2.
The API key needs read access to `project.task`, `project.project` and
`project.tags`. The plugin never writes to Odoo.

## Install

From Tileception, type `plugins add robinvandernoord/tileception-odoo` in the
launcher. Once installed, the launcher asks for the Odoo web URL, optional
JSON-2 URL and database, and the API key, checks that they can read projects,
saves them and starts the first sync in the background. Type
`plugins configure odoo` to change them later; leave the key empty to keep it.

By hand, in an activated Python 3.14+ virtual environment:

```sh
uv pip install .
tileception-odoo install
```

Installation configures Odoo, downloads the model (about 400 MB initially),
installs the manifest and user units, runs the first sync and offers the timer.
Rerunning it reuses configuration and valid model files.

Load the manifest with `tileception msg plugins reload`, or restart Tileception.
For unattended setup, run
`tileception-odoo install --non-interactive --enable-timer`. Without a terminal
(or with `--non-interactive`), install never prompts and starts the first sync
in the background instead of waiting for it; follow it with
`tileception-odoo status`. Without configuration it installs everything but
the sync and `status` reports `"configured": false`; then set it up with
`tileception-odoo configure`, in the launcher (`odoo ` shows **Set up Odoo
search**), or by providing the configuration and key file first.

## Configuration

The default web URL is `https://educationwarehouse.nl`. The JSON-2 URL is
derived automatically; Enter accepts both defaults during setup.

```toml
web_url = "https://educationwarehouse.nl"
database = "example"
threads = 2
debounce_ms = 180
# json2_url = "https://educationwarehouse.nl/json/2"
# api_key_file = "/absolute/private/path/to/api-key"
```

`ODOO_WEB_URL`, `ODOO_JSON2_URL`, `ODOO_DB` and `ODOO_API_KEY` override file
settings. The timer uses saved configuration, not your terminal's environment.
Keep credentials outside the repository; the key file must have mode 0600.

Paths follow XDG conventions:

| Purpose          | Default path                                                   |
| ---------------- | -------------------------------------------------------------- |
| Configuration    | `~/.config/tileception-odoo/config.toml`                       |
| API key          | `~/.config/tileception-odoo/api-key`                           |
| Cache / progress | `~/.cache/tileception-odoo/`                                   |
| Model            | `~/.local/share/tileception-odoo/model/`                       |
| Manifest         | `~/.local/share/tileception/plugins/odoo.toml`                 |
| Systemd units    | `~/.config/systemd/user/tileception-odoo-sync.{service,timer}` |

Use `tileception-odoo configure` to change credentials and
`tileception-odoo setup-model` to repair or download model files.

## Choosing projects

By default every project is synchronized. To limit this, list Odoo project IDs
in `config.toml`; `0` stands for tasks without a project:

```toml
include_projects = [12, 48, 0]  # empty or absent: all projects
exclude_projects = [7]          # always wins over include_projects
```

`tileception-odoo projects` opens a picker for `include_projects`: type to
filter, Space or Tab to toggle, Ctrl-A to toggle every shown project, Ctrl-X
to clear, Enter to save and Esc to cancel. Edit `exclude_projects` by hand.
`configure` keeps both lists.

Filtering happens in Odoo, so excluded records are not downloaded or embedded.
After the selection changes, the next sync fetches everything selected and
removes the rest from the cache. Records that stay selected keep their
embeddings.

## Search and privacy

Search combines exact IDs/URLs, partial and fuzzy text matching, and
[Granite multilingual embeddings][model] on CPU. Text results appear first;
semantic results append without moving the selected row. Older Tileception
builds without progressive plugin results provide text search only.

Text and embeddings stay local. Only sync contacts Odoo; model downloads happen
during setup. Private directories use mode 0700 and files use 0600.

## Sync and progress

Run in the foreground:

```sh
tileception-odoo sync
tileception-odoo status  # JSON status and progress counters
```

Or start the service and follow its progress:

```sh
systemctl --user start --no-block tileception-odoo-sync.service
journalctl --user -fu tileception-odoo-sync.service
```

Progress shows records processed and embedding chunks saved or reused.
Record totals describe changes fetched for this run, not the whole cache.
The launcher status refreshes when you reopen `odoo `.

Each batch of up to four embedding chunks is committed. Interrupted runs reuse
saved chunks; only the active batch needs recomputing. The sync checkpoint
advances after the full run succeeds. Network failures leave cached search
available. Daily reconciliation removes archived, deleted or inaccessible records.

The timer runs every 15 minutes with up to two minutes of jitter and catches up
after login. Background sync has low CPU/I/O priority and a 50% CPU quota.
Enable it manually with:

```sh
systemctl --user enable --now tileception-odoo-sync.timer
```

## Uninstall

From Tileception: `plugins remove odoo`. By hand:

```sh
tileception-odoo uninstall           # stops the timer, removes units and manifest
tileception msg plugins reload
uv pip uninstall tileception-odoo
```

Configuration, the API key, cache and model files stay unless you run
`tileception-odoo uninstall --purge`.

## Development

```sh
uv venv --python 3.14 venv
uv pip install --python venv/bin/python -e '.[dev]'
venv/bin/ruff check src
venv/bin/ruff format --check src
uv build
```

## Releases

Unreleased, version `0.0.0`. Vommit is included in the `dev` extra:

```sh
venv/bin/vommit check
venv/bin/vommit release
```

Release from a clean `master` with `origin` configured. Vommit uses Conventional
Commits to bump the version, update the changelog, build, commit, tag and push.
PyPI publishing is disabled.

[model]: https://huggingface.co/ibm-granite/granite-embedding-97m-multilingual-r2
