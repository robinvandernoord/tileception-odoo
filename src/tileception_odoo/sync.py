"""Single-writer, transactional incremental synchronization."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser

from .config import Config, Paths, private_directory
from .embeddings import IDENTITY, Embedder
from .odoo import MODELS, OdooClient, relation_name
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


def synchronize(paths: Paths, config: Config):
    with writer_lock(paths):
        store = Store(paths.database, writable=True)
        try:
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
            fetched = {model: list(client.records(model, since)) for model in MODELS}
            archived_projects = {
                row["id"] for row in fetched["project.project"] if not row.get("active", True)
            }
            tag_ids = {
                tag for records in fetched.values() for row in records for tag in row.get("tag_ids", [])
            }
            tags = client.tags(tag_ids)
            embedder = Embedder(paths.data / "model", threads=config.threads)
            with store.transaction():
                if changed_scope:
                    store.db.execute("DELETE FROM records")
                for model, records in fetched.items():
                    visible = set()
                    for row in records:
                        project = row.get("project_id")
                        archived_project = isinstance(project, list) and project[0] in archived_projects
                        if not row.get("active", True) or archived_project:
                            store.remove(model, row["id"])
                            continue
                        visible.add(row["id"])
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
                        store.put(record, embedder)
                    if reconcile:
                        store.reconcile(model, visible)
                store.set("scope", scope(config))
                store.set("auth", auth)
                store.set("checkpoint", started.isoformat())
                store.set("last_success", datetime.now(UTC).isoformat())
                store.set("last_error", "")
                store.set("embedding_identity", IDENTITY)
                if reconcile:
                    store.set("reconciled", started.isoformat())
        except Exception as error:
            # Neither server responses nor task content belong in diagnostics.
            message = (
                str(error) if isinstance(error, (ValueError, FileNotFoundError)) else type(error).__name__
            )
            with store.transaction():
                store.set("last_error", f"{datetime.now(UTC).isoformat()}: {message}")
            raise
        finally:
            store.close()
