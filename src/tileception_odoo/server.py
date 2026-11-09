"""Newline-delimited MCP, with one bounded, debounced semantic worker."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime

from . import __version__
from .config import Config, Paths
from .embeddings import Embedder
from .progress import read_progress
from .store import Store, launcher_row
from .sync import scope


def tool(value: dict) -> dict:
    return {"structuredContent": value, "content": [{"type": "text", "text": json.dumps(value)}]}


class Server:
    def __init__(self, paths: Paths):
        self.paths = paths
        self.output = threading.Lock()
        self.condition = threading.Condition()
        self.pending = None
        self.serial = 0
        self.closed = False
        self.semantic_error = ""
        self.worker = threading.Thread(target=self.semantic_worker, name="odoo-semantic", daemon=True)
        self.worker.start()

    def send(self, value: dict):
        with self.output:
            print(json.dumps(value, ensure_ascii=False), flush=True)

    def cancel(self):
        with self.condition:
            self.serial += 1
            self.pending = None
            self.condition.notify()

    def cache(self) -> Store | None:
        if not self.paths.database.exists():
            return None
        config = Config.load(self.paths)
        store = Store(self.paths.database)
        if store.get("scope") != scope(config):
            store.close()
            return None
        return store

    def status(self) -> dict:
        status = {
            "last_success": None,
            "last_error": "",
            "semantic_error": self.semantic_error,
            "records": 0,
            "sync_progress": read_progress(self.paths),
        }
        try:
            config = Config.load(self.paths)
            if self.paths.database.exists():
                store = Store(self.paths.database)
                try:
                    if store.get("scope") in {None, scope(config)}:
                        status.update(
                            {
                                "last_success": store.get("last_success"),
                                "last_error": store.get("last_error"),
                                "records": store.db.execute("SELECT count(*) FROM records").fetchone()[0],
                            }
                        )
                    else:
                        status["last_error"] = "Odoo configuration changed; synchronize to replace the cache"
                finally:
                    store.close()
        except (ValueError, OSError, sqlite3.Error) as error:
            status["last_error"] = str(error)
        return status

    def status_rows(self) -> list[dict]:
        status = self.status()
        last_success = status["last_success"]
        if last_success:
            last_success = datetime.fromisoformat(last_success).astimezone().strftime("%d %b %Y %H:%M %Z")
        rows = [
            {
                "title": "Sync Odoo now",
                "detail": "Refresh the local task and project cache",
                "icon": "glyph:globe",
                "actions": [{"title": "Sync Odoo now", "type": "tool", "name": "sync_now", "arguments": {}}],
            },
            {
                "title": f"Last successful sync: {last_success or 'never'}",
                "detail": f"{status['records']} cached records",
                "actions": [],
            },
        ]
        progress = status["sync_progress"]
        if progress and progress.get("state") != "complete":
            state = progress.get("state")
            rows.insert(
                1,
                {
                    "title": {"running": "Sync in progress", "failed": "Sync failed"}.get(
                        state, "Sync interrupted"
                    ),
                    "detail": progress["message"],
                    "actions": [],
                },
            )
        for key in ("last_error", "semantic_error"):
            if key == "last_error" and progress and progress.get("state") == "running":
                continue
            if status[key]:
                rows.append(
                    {
                        "title": "Odoo sync error" if key == "last_error" else "Semantic search unavailable",
                        "detail": status[key],
                        "actions": [],
                    }
                )
        return rows

    def search(self, arguments: dict) -> tuple[dict, tuple | None]:
        self.cancel()
        query = arguments.get("query", "")
        if not isinstance(query, str) or len(query) > 4096:
            raise ValueError("Query must be text of at most 4096 characters")
        if not query.strip():
            return tool({"rows": self.status_rows()}), None
        store = self.cache()
        records = []
        if store:
            try:
                records = store.text_search(query)
            finally:
                store.close()
        rows = [launcher_row(row, query=query) for row in records]
        if not store:
            rows += self.status_rows()
        generation = arguments.get("_meta", {}).get("tileception/generation")
        pending = None
        if generation is not None and store:
            config = Config.load(self.paths)
            pending = (
                self.serial,
                time.monotonic() + config.debounce_ms / 1000,
                generation,
                query,
                {(row["model"], row["id"]) for row in records},
                config,
            )
        return tool({"rows": rows}), pending

    def semantic_worker(self):
        embedder = None
        while True:
            with self.condition:
                self.condition.wait_for(lambda: self.closed or self.pending is not None)
                if self.closed:
                    return
                job = self.pending
                remaining = job[1] - time.monotonic()
                if remaining > 0:
                    self.condition.wait(timeout=remaining)
                    continue
                self.pending = None
            serial, _, generation, query, existing, config = job
            try:
                if embedder is None:
                    embedder = Embedder(self.paths.data / "model", threads=config.threads)
                vector = embedder.encode([query])[0]
                with self.condition:
                    if serial != self.serial:
                        continue
                store = self.cache()
                if not store:
                    continue
                try:
                    records = store.semantic_search(vector)
                finally:
                    store.close()
                rows = [
                    launcher_row(row, query=query)
                    for row in records
                    if (row["model"], row["id"]) not in existing
                ]
                self.semantic_error = ""
            except Exception as error:
                self.semantic_error = str(error) if isinstance(error, ValueError) else type(error).__name__
                rows = [
                    {"title": "Semantic search unavailable", "detail": self.semantic_error, "actions": []}
                ]
            with self.condition:
                if serial == self.serial and not self.closed:
                    try:
                        self.send(
                            {
                                "jsonrpc": "2.0",
                                "method": "notifications/tileception/launcher_append",
                                "params": {"generation": generation, "rows": rows},
                            }
                        )
                    except BrokenPipeError:
                        return

    def dispatch(self, request: dict) -> tuple[dict, tuple | None]:
        method = request.get("method")
        if method == "initialize":
            return {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "tileception-odoo", "version": __version__},
            }, None
        if method == "ping":
            return {}, None
        if method == "tools/list":
            return {
                "tools": [
                    {"name": name, "description": description, "inputSchema": schema}
                    for name, description, schema in [
                        ("launcher_info", "Odoo search metadata", {"type": "object"}),
                        (
                            "launcher_search",
                            "Search cached tasks and projects",
                            {
                                "type": "object",
                                "properties": {"prefix": {"type": "string"}, "query": {"type": "string"}},
                                "required": ["prefix", "query"],
                            },
                        ),
                        ("sync_now", "Start local background Odoo synchronization", {"type": "object"}),
                        ("status", "Last synchronization time and errors", {"type": "object"}),
                    ]
                ]
            }, None
        if method != "tools/call":
            raise LookupError("Method not found")
        params = request.get("params", {})
        name = params.get("name")
        if name == "launcher_info":
            return tool(
                {
                    "name": "Odoo",
                    "description": "Local task and project search",
                    "prefixes": ["odoo"],
                    "icon": "glyph:globe",
                }
            ), None
        if name == "launcher_search":
            return self.search(params.get("arguments", {}))
        if name == "status":
            return tool(self.status()), None
        if name == "sync_now":
            result = subprocess.run(
                ["systemctl", "--user", "start", "--no-block", "tileception-odoo-sync.service"],
                capture_output=True,
                timeout=1.5,
            )
            if result.returncode:
                raise ValueError(
                    "Could not start sync; install the user service with tileception-odoo install"
                )
            return tool(
                {"text": "Odoo synchronization started. Type odoo followed by a space for status."}
            ), None
        raise ValueError("Unknown tool")

    def run(self):
        try:
            while line := sys.stdin.buffer.readline(1024 * 1024 + 1):
                if len(line) > 1024 * 1024:
                    raise ValueError("MCP message exceeded 1 MiB")
                request = None
                try:
                    request = json.loads(line)
                    if not isinstance(request, dict):
                        raise ValueError("MCP request must be an object")
                    if "id" not in request:
                        if request.get("method") == "notifications/cancelled":
                            self.cancel()
                        continue
                    result, pending = self.dispatch(request)
                    self.send({"jsonrpc": "2.0", "id": request["id"], "result": result})
                    # Guarantee text response precedes any append, even when inference is already warm.
                    if pending:
                        with self.condition:
                            self.pending = pending
                            self.condition.notify()
                except LookupError:
                    self.send(
                        {
                            "jsonrpc": "2.0",
                            "id": request.get("id"),
                            "error": {"code": -32601, "message": "Method not found"},
                        }
                    )
                except Exception as error:
                    message = str(error) if isinstance(error, (ValueError, OSError)) else type(error).__name__
                    if isinstance(request, dict) and request.get("method") == "tools/call":
                        self.send(
                            {
                                "jsonrpc": "2.0",
                                "id": request.get("id"),
                                "result": {
                                    "isError": True,
                                    "content": [{"type": "text", "text": message}],
                                },
                            }
                        )
                    else:
                        self.send(
                            {
                                "jsonrpc": "2.0",
                                "id": request.get("id") if isinstance(request, dict) else None,
                                "error": {"code": -32600, "message": "Invalid request"},
                            }
                        )
        finally:
            with self.condition:
                self.closed = True
                self.condition.notify()
