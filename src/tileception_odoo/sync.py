"""Single-writer incremental sync with durable embedding batches and progress."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from functools import partial
from html.parser import HTMLParser

from .config import Config, Paths, private_directory
from .embeddings import IDENTITY, Embedder
from .odoo import MODELS, OdooClient, relation_name
from .progress import Progress
from .store import Store


class PlainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1
        if tag in {"p", "br", "div", "li", "h1", "h2", "h3", "tr"}:
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)
        self.parts.append(" ")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def plain_text(value) -> str:
    parser = PlainText()
    parser.feed(value or "")
    return " ".join("".join(parser.parts).split())


def scope(config: Config) -> str:
    return json.dumps([config.json2_url, config.web_url, config.database])


@contextmanager
def writer_lock(paths: Paths):
    private_directory(paths.cache)
    with (paths.cache / "sync.lock").open("a") as handle:
        os.chmod(handle.name, 0o600)
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("Another Odoo synchronization is already running") from None
        yield


def report_chunks(completed, total, created, progress, label, processed, records_total, embedded, reused):
    progress.update(
        f"Indexing {label}: {processed}/{records_total} records complete; "
        f"current record {completed}/{total} embedding chunks saved "
        f"({embedded + created} new, {reused + completed - created} reused)",
        completed_chunks=completed,
        total_chunks=total,
        embedded_chunks=embedded + created,
        reused_chunks=reused + completed - created,
    )


def synchronize(paths: Paths, config: Config):
    with writer_lock(paths):
        store = Store(paths.database, writable=True)
        progress = Progress(paths)
        try:
            progress.update("Starting Odoo synchronization", force=True)
            client = OdooClient(config)
            started = datetime.now(UTC)
            auth = hashlib.sha256(client.key.encode()).hexdigest()
            changed_scope = store.get("scope") != scope(config) or store.get("auth") != auth
            checkpoint = None if changed_scope else store.get("checkpoint")
            previous_reconcile = store.get("reconciled")
            reconcile = (
                not checkpoint
                or not previous_reconcile
                or started - datetime.fromisoformat(previous_reconcile) >= timedelta(days=1)
                or store.get("embedding_identity") != IDENTITY
            )
            since = None
            if not reconcile:
                since = (datetime.fromisoformat(checkpoint) - timedelta(minutes=5)).strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
            labels = {"project.project": "projects", "project.task": "tasks"}
            fetched = {}
            for model in MODELS:
                label = labels[model]
                progress.update(f"Fetching Odoo {label}", phase="fetching", model=model, force=True)
                fetched[model] = []
                for row in client.records(model, since):
                    fetched[model].append(row)
                    if len(fetched[model]) % 200 == 0:
                        progress.update(
                            f"Fetched {len(fetched[model])} {label}", fetched_records=len(fetched[model])
                        )
                progress.update(
                    f"Fetched {len(fetched[model])} {label}",
                    fetched_records=len(fetched[model]),
                    force=True,
                )
            archived_projects = {
                row["id"] for row in fetched["project.project"] if not row.get("active", True)
            }
            tag_ids = {
                tag for records in fetched.values() for row in records for tag in row.get("tag_ids", [])
            }
            progress.update("Fetching tag names", phase="tags")
            tags = client.tags(tag_ids)
            progress.update("Loading the local embedding model", phase="loading_model")
            embedder = Embedder(paths.data / "model", threads=config.threads)
            with store.transaction():
                if changed_scope:
                    store.db.execute("DELETE FROM records")
                    store.db.execute(
                        "DELETE FROM meta WHERE key IN "
                        "('checkpoint','last_success','reconciled','embedding_identity')"
                    )
                # Persist cache ownership before batching. Retries must reuse the
                # new account's saved chunks instead of clearing them again.
                store.set("scope", scope(config))
                store.set("auth", auth)
                store.set("last_error", "")
                if not store.get("embedding_identity"):
                    store.set("embedding_identity", IDENTITY)
            total = sum(len(records) for records in fetched.values())
            processed = embedded = reused = 0
            visible = {model: set() for model in MODELS}
            for model, records in fetched.items():
                label = labels[model]
                progress.update(
                    f"Indexing {label}: {processed}/{total} records complete",
                    phase="indexing",
                    model=model,
                    total_records=total,
                    completed_chunks=0,
                    total_chunks=0,
                    force=True,
                )
                for row in records:
                    project = row.get("project_id")
                    archived_project = isinstance(project, list) and project[0] in archived_projects
                    if not row.get("active", True) or archived_project:
                        with store.transaction():
                            store.remove(model, row["id"])
                    else:
                        visible[model].add(row["id"])
                        record = {
                            "model": model,
                            "id": row["id"],
                            "title": row["name"] or "Untitled",
                            "project": relation_name(row.get("project_id"))
                            if model == "project.task"
                            else row["name"],
                            "stage": relation_name(row.get("stage_id")),
                            "tags": " ".join(tags.get(tag, "") for tag in row.get("tag_ids", [])),
                            "description": plain_text(row.get("description")),
                            "url": f"{config.web_url}/web#id={row['id']}&model={model}&view_type=form",
                            "write_date": row["write_date"] or "",
                        }
                        created, cached = store.put(
                            record,
                            embedder,
                            on_progress=partial(
                                report_chunks,
                                progress=progress,
                                label=label,
                                processed=processed,
                                records_total=total,
                                embedded=embedded,
                                reused=reused,
                            ),
                        )
                        embedded += created
                        reused += cached
                    processed += 1
                    progress.update(
                        f"Saved {processed}/{total} records; "
                        f"{embedded} embedding chunks new, {reused} reused",
                        processed_records=processed,
                        embedded_chunks=embedded,
                        reused_chunks=reused,
                    )
            progress.update("Reconciling removed records and saving the sync checkpoint", phase="finalizing")
            with store.transaction():
                # Prune only after both complete listings and indexing succeeded.
                # Neither a failed page nor an interrupted inference can look like deletion.
                if reconcile:
                    for model in MODELS:
                        store.reconcile(model, visible[model])
                store.set("checkpoint", started.isoformat())
                store.set("last_success", datetime.now(UTC).isoformat())
                store.set("last_error", "")
                store.set("embedding_identity", IDENTITY)
                if reconcile:
                    store.set("reconciled", started.isoformat())
            progress.update(
                f"Synchronization complete: {processed}/{total} records; "
                f"{embedded} embedding chunks new, {reused} reused",
                state="complete",
                phase="complete",
                force=True,
            )
        except (Exception, KeyboardInterrupt) as error:
            # Neither server responses nor task content belong in diagnostics.
            message = (
                "Synchronization interrupted"
                if isinstance(error, KeyboardInterrupt)
                else (
                    str(error) if isinstance(error, (ValueError, FileNotFoundError)) else type(error).__name__
                )
            )
            with store.transaction():
                store.set("last_error", f"{datetime.now(UTC).isoformat()}: {message}")
            progress.update(
                f"{message}. Saved chunks are retained; rerun sync to continue.",
                state="interrupted" if isinstance(error, KeyboardInterrupt) else "failed",
                force=True,
            )
            raise
        finally:
            store.close()
