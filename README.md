# tileception-odoo

A separately installed Tileception launcher plugin for Odoo tasks and projects.
Type `odoo ` for synchronization status and **Sync Odoo now**, or `odoo query`
to search the local cache. Enter opens a result in Odoo.

Requires Linux, Python 3.14+, systemd user services, and Odoo's JSON-2 API
(Odoo 19+). The API key needs read access to `project.task`, `project.project`
and `project.tags`; use a dedicated account with the minimum required access.
No write operations are implemented. Tileception and the Codex Odoo skill are
not package dependencies.

## Install

```sh
uv pip install .
tileception-odoo install
```

`install` guides you through the complete setup. It asks for Odoo configuration
when needed, prepares the local embedding model, installs the launcher manifest
and systemd user units, and runs the initial synchronization through the service
with its CPU limits. It then offers to enable the 15-minute timer. Restart
Tileception when installation finishes to discover the plugin.

Existing configuration is reused, and valid model files are reused without
downloading them again. If synchronization fails, fix the reported problem and
rerun `install`; configuration and model files are kept. For unattended setup,
provision the private configuration and key file first, then use
`install --enable-timer`.

The web URL defaults to `https://educationwarehouse.nl`; the JSON-2 URL is
derived from it. Press Enter to accept either default or type an override.
The API key prompt displays asterisks while you type or paste. Invalid URLs are
explained and prompted again before asking for the key. The key is saved in a
separate owned file with mode 0600 outside the source repository. Never put
credentials in this checkout.

The individual `configure`, `setup-model` and `sync` commands remain available
for changing credentials, repairing model files or refreshing the cache later.

The package can also be built with `uv build` and the resulting wheel installed
with `uv tool install dist/tileception_odoo-0.0.0-py3-none-any.whl`.

The default paths follow XDG conventions:

| Content | Default location |
| :--- | :--- |
| Configuration | `~/.config/tileception-odoo/config.toml` |
| API key | `~/.config/tileception-odoo/api-key` |
| SQLite cache and writer lock | `~/.cache/tileception-odoo/` |
| Model files | `~/.local/share/tileception-odoo/model/` |
| Plugin manifest | `~/.local/share/tileception/plugins/odoo.toml` |
| User units | `~/.config/systemd/user/tileception-odoo-sync.{service,timer}` |

Configuration example, without secrets:

```toml
web_url = "https://educationwarehouse.nl"
json2_url = "https://educationwarehouse.nl/json/2"
database = "example"
threads = 2
debounce_ms = 180
# api_key_file = "/absolute/private/path/to/api-key"
```

The web URL defaults to `https://educationwarehouse.nl`, including in the
interactive prompt where Enter accepts it. `json2_url` is optional: when omitted or empty, it defaults to the web base
address followed by `/json/2`. Backend entry points such as `/odoo` and `/web`
are stripped before deriving this address; installation path prefixes are kept.

`ODOO_WEB_URL`, `ODOO_JSON2_URL`, `ODOO_DB` and `ODOO_API_KEY` override file
configuration. The timer uses the configuration file rather than an interactive
shell's environment. HTTPS canonical redirects between a host and its `www`
alias are followed automatically while retaining the JSON-2 POST. Redirects to
other domains, ports, paths or HTTP are rejected to avoid forwarding credentials.
For other deployments, configure the final HTTPS API endpoint explicitly.
The web URL may include an installation path;
trailing `/odoo` or `/web` backend entry points are normalized for record links.

## Search and privacy

SQLite FTS5, partial matching and RapidFuzz cover titles, projects, tags and
descriptions. Numeric IDs and standard Odoo record URLs receive exact matches.
For short Odoo URLs, task/project path segments identify the record type.
An ID alone can match both a task and a project. Unsupported action-only URLs
do not guess the record model.

Text results return immediately. A single worker debounces query embeddings,
keeps only the latest pending query and appends distinct semantic results.
Existing rows keep their order, so the selected row does not move. This requires
the Tileception `notifications/tileception/launcher_append` extension documented
in its `docs/plugins.md`. Older hosts still receive text results, but cannot
display later semantic results.

Embeddings use IBM's Apache-2.0 licensed
[granite-embedding-97m-multilingual-r2](https://huggingface.co/ibm-granite/granite-embedding-97m-multilingual-r2),
revision `835ad14087e140460703cf0fae09f97d469d65c2`, the published float32 ONNX
export, CLS pooling, L2 normalization and 384 dimensions. ONNX Runtime uses only
its CPU provider, at most four inference threads (two by default) and one
inter-op thread. Long descriptions use overlapping 320-token chunks; each
includes up to 96 title tokens. A batched linear vector scan picks each record's
best chunk. Semantic scores below 0.70 or more than 0.10 below the best hit are
omitted; these are initial heuristics rather than calibrated probabilities.
No specialized vector index or GPU support is included.

`install` and `setup-model` prepare model files, checking pinned SHA-256 checksums
and downloading only missing or invalid files. `serve`
does not contact Odoo or a model service. Search queries and task text are never
sent to an embedding service. `sync` contacts only the configured Odoo endpoint;
all text extraction, chunking and embedding happens locally. Cache directories
are 0700 and files are 0600; embeddings are treated as sensitive cached content.
Local filesystem encryption and backups follow your machine's configuration.

## Synchronization and maintenance

```sh
tileception-odoo sync
tileception-odoo status
systemctl --user enable --now tileception-odoo-sync.timer
systemctl --user list-timers tileception-odoo-sync.timer
journalctl --user -u tileception-odoo-sync.service
```

The persistent calendar timer runs every 15 minutes with up to two minutes of
random delay. The service has low CPU/I/O priority and a 50% CPU quota. Catch-up
occurs when the user manager starts; running while logged out additionally
requires systemd lingering, which this installer does not enable.

Synchronization pages by ID and filters on `write_date` with a five-minute
overlap. It advances the checkpoint only after the complete transaction succeeds.
The checkpoint uses the local UTC start time, so keep the Odoo server and client
clocks synchronized. Once daily, it reads all accessible records, including
archived ones, refreshes metadata and removes archived, deleted and inaccessible
records. Permission revocations can therefore remain cached until the next
successful daily reconciliation. Changing the endpoint, database or key forces
a full reconciliation. Metadata-only changes update FTS and presentation without
recomputing title/description vectors. A model/chunking identity change forces
reindexing. SQLite WAL permits readers during updates; a nonblocking `flock`
prevents overlapping sync and model setup processes.

A failed sync rolls back record changes and preserves the previous checkpoint
and cache, then records an error for the plugin's status view. **Sync Odoo now**
starts the installed user service without waiting for indexing. Reopen `odoo `
to see current status. Keep the previous cache available during outages.

To uninstall integration:

```sh
systemctl --user disable --now tileception-odoo-sync.timer
systemctl --user stop tileception-odoo-sync.service
rm ~/.local/share/tileception/plugins/odoo.toml
rm ~/.config/systemd/user/tileception-odoo-sync.{service,timer}
systemctl --user daemon-reload
uv tool uninstall tileception-odoo
```

Remove the three private `tileception-odoo` directories separately if you also
want to erase credentials, cached records and model files. Restart Tileception
to unload the manifest.

## Development

```sh
uv venv --python 3.14 venv
uv pip install --python venv/bin/python -e '.[dev]'
venv/bin/ruff check src
venv/bin/ruff format --check src
venv/bin/python -m compileall -q src
uv build
```

The read-only JSON-2 request structure and relation metadata handling were
adapted from the Odoo skill's client. No skill paths, repository mappings,
credentials or runtime imports are used. No Git initialization, commits or
host installation are required to develop this package.

## Releases

The project is unreleased; its initial version is `0.0.0`. The package uses
`uv_build`, and the MCP server reads its version from installed package metadata.

Vommit is included in the `dev` extra. Run
`venv/bin/vommit check` to inspect the release configuration. When ready to cut
a release from `master`, with a clean working tree and `origin` configured, run:

```sh
venv/bin/vommit release
```

Vommit derives the version bump from Conventional Commits, updates `pyproject.toml`
and `CHANGELOG.md`, builds source and wheel distributions with `uv build`, and
commits, tags and pushes the release. Breaking changes bump the major version,
features the minor version, and fixes or performance changes the patch version.
PyPI integration and both publishing commands are disabled for now, so release
artifacts remain local in `dist/` and nothing is uploaded to PyPI.
