"""SQLite WAL cache. Only sync opens a writable connection."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import numpy as np
from rapidfuzz import fuzz

from .embeddings import IDENTITY

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS records (
    model TEXT NOT NULL, id INTEGER NOT NULL, title TEXT NOT NULL,
    project TEXT NOT NULL, stage TEXT NOT NULL, tags TEXT NOT NULL,
    description TEXT NOT NULL, url TEXT NOT NULL, content_hash TEXT NOT NULL,
    write_date TEXT NOT NULL, embedding_complete INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY(model,id)
);
CREATE VIRTUAL TABLE IF NOT EXISTS records_fts USING fts5(
    title, project, tags, description, content='records', content_rowid='rowid',
    tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS records_ai AFTER INSERT ON records BEGIN
    INSERT INTO records_fts(rowid,title,project,tags,description)
    VALUES (new.rowid,new.title,new.project,new.tags,new.description);
END;
CREATE TRIGGER IF NOT EXISTS records_ad AFTER DELETE ON records BEGIN
    INSERT INTO records_fts(records_fts,rowid,title,project,tags,description)
    VALUES ('delete',old.rowid,old.title,old.project,old.tags,old.description);
END;
CREATE TRIGGER IF NOT EXISTS records_au AFTER UPDATE ON records BEGIN
    INSERT INTO records_fts(records_fts,rowid,title,project,tags,description)
    VALUES ('delete',old.rowid,old.title,old.project,old.tags,old.description);
    INSERT INTO records_fts(rowid,title,project,tags,description)
    VALUES (new.rowid,new.title,new.project,new.tags,new.description);
END;
CREATE TABLE IF NOT EXISTS chunks (
    model TEXT NOT NULL, record_id INTEGER NOT NULL, position INTEGER NOT NULL,
    text TEXT NOT NULL, embedding BLOB NOT NULL,
    PRIMARY KEY(model,record_id,position),
    FOREIGN KEY(model,record_id) REFERENCES records(model,id) ON DELETE CASCADE
);
"""


class Store:
    def __init__(self, filename: Path, writable: bool = False):
        if writable:
            self.db = sqlite3.connect(filename, timeout=5)
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA foreign_keys=ON")
            self.db.executescript(SCHEMA)
            columns = {row[1] for row in self.db.execute("PRAGMA table_info(records)")}
            if "embedding_complete" not in columns:
                # Older caches committed whole runs, so every existing record is complete.
                self.db.execute(
                    "ALTER TABLE records ADD COLUMN embedding_complete INTEGER NOT NULL DEFAULT 1"
                )
                self.db.commit()
            filename.chmod(0o600)
        else:
            self.db = sqlite3.connect(f"{filename.as_uri()}?mode=ro", uri=True, timeout=0.2)
        self.db.row_factory = sqlite3.Row

    def close(self):
        self.db.close()

    @contextmanager
    def transaction(self):
        with self.db:
            yield

    def get(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set(self, key: str, value: str):
        self.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, value))

    def put(self, record: dict, embedder, on_progress=None) -> tuple[int, int]:
        """Save metadata and each inference batch; reuse committed chunks on retry.

        Returns newly embedded and reused chunk counts. Callers must not wrap
        this in a larger transaction: every completed batch is durable here.
        """
        # Stage, tags and project are searchable metadata but do not change title/description embeddings.
        digest = hashlib.sha256(
            json.dumps([IDENTITY, record["title"], record["description"]], ensure_ascii=False).encode()
        ).hexdigest()
        key = (record["model"], record["id"])
        old = self.db.execute(
            "SELECT content_hash,embedding_complete FROM records WHERE model=? AND id=?",
            key,
        ).fetchone()
        unchanged = old is not None and old["content_hash"] == digest
        complete = unchanged and bool(old["embedding_complete"])
        with self.transaction():
            self.db.execute(
                """
                INSERT INTO records (
                    model,id,title,project,stage,tags,description,url,content_hash,write_date,embedding_complete
                ) VALUES (
                    :model,:id,:title,:project,:stage,:tags,:description,:url,:hash,:write_date,:complete
                )
                ON CONFLICT(model,id) DO UPDATE SET title=excluded.title, project=excluded.project,
                stage=excluded.stage, tags=excluded.tags, description=excluded.description,
                url=excluded.url, content_hash=excluded.content_hash, write_date=excluded.write_date,
                embedding_complete=excluded.embedding_complete
                """,
                {**record, "hash": digest, "complete": int(complete)},
            )
            if not unchanged:
                self.db.execute("DELETE FROM chunks WHERE model=? AND record_id=?", key)
        existing = {
            row[0]
            for row in self.db.execute(
                "SELECT position FROM chunks WHERE model=? AND record_id=?",
                key,
            )
        }
        if complete:
            if on_progress:
                on_progress(len(existing), len(existing), 0)
            return 0, len(existing)
        chunks = embedder.chunks(record["title"], record["description"])
        missing = [(position, text) for position, text in enumerate(chunks) if position not in existing]
        created = 0
        if on_progress:
            on_progress(len(existing), len(chunks), created)
        for start in range(0, len(missing), 4):
            batch = missing[start : start + 4]
            vectors = embedder.encode([text for _, text in batch])
            with self.transaction():
                self.db.executemany(
                    "INSERT INTO chunks VALUES (?,?,?,?,?)",
                    [
                        (*key, position, text, vector.tobytes())
                        for (position, text), vector in zip(batch, vectors, strict=True)
                    ],
                )
                created += len(batch)
                if len(existing) + created == len(chunks):
                    self.db.execute("UPDATE records SET embedding_complete=1 WHERE model=? AND id=?", key)
            if on_progress:
                on_progress(len(existing) + created, len(chunks), created)
        if not missing:
            with self.transaction():
                self.db.execute("UPDATE records SET embedding_complete=1 WHERE model=? AND id=?", key)
        return created, len(existing)

    def remove(self, model: str, record_id: int):
        self.db.execute("DELETE FROM records WHERE model=? AND id=?", (model, record_id))

    def reconcile(self, model: str, visible: set[int]):
        for row in self.db.execute("SELECT id FROM records WHERE model=?", (model,)).fetchall():
            if row[0] not in visible:
                self.remove(model, row[0])

    def text_search(self, query: str, limit: int = 24) -> list[dict]:
        query = query.strip()
        exact = exact_record(query)
        ranked = {}
        if exact:
            model, record_id = exact
            for row in self.db.execute(
                "SELECT * FROM records WHERE id=? AND (? IS NULL OR model=?)",
                (record_id, model, model),
            ):
                ranked[(row["model"], row["id"])] = (1000, dict(row))
        if not query:
            return [
                dict(row)
                for row in self.db.execute(
                    "SELECT * FROM records ORDER BY write_date DESC,model,id LIMIT ?",
                    (limit,),
                )
            ]
        tokens = re.findall(r"\w+", query, re.UNICODE)[:24]
        if tokens:
            expression = " OR ".join('"' + word + '"*' for word in tokens)
            rows = self.db.execute(
                """
                SELECT r.*, bm25(records_fts,8,4,3,1) AS rank FROM records_fts
                JOIN records r ON r.rowid=records_fts.rowid WHERE records_fts MATCH ? ORDER BY rank LIMIT 100
            """,
                (expression,),
            )
            for index, row in enumerate(rows):
                key = (row["model"], row["id"])
                ranked.setdefault(key, (200 - index / 100, dict(row)))
        # A simple metadata scan supplies substrings and typo tolerance; descriptions use FTS.
        for row in self.db.execute("SELECT model,id,title,project,tags FROM records"):
            text = " ".join((row["title"], row["project"], row["tags"])).casefold()
            score = fuzz.WRatio(query.casefold(), text)
            if query.casefold() in text:
                score += 100
            if score >= 65:
                key = (row["model"], row["id"])
                candidate = (score, dict(row))
                if key not in ranked or score > ranked[key][0]:
                    ranked[key] = candidate
        ordered = sorted(ranked.values(), key=lambda item: (-item[0], item[1]["model"], item[1]["id"]))
        return [
            dict(
                self.db.execute(
                    "SELECT * FROM records WHERE model=? AND id=?",
                    (record["model"], record["id"]),
                ).fetchone()
            )
            for _, record in ordered[:limit]
        ]

    def semantic_search(self, vector: np.ndarray, limit: int = 16) -> list[dict]:
        if self.get("embedding_identity") != IDENTITY:
            return []
        scores = {}
        cursor = self.db.execute("SELECT model,record_id,text,embedding FROM chunks")
        while batch := cursor.fetchmany(512):
            matrix = np.stack([np.frombuffer(row["embedding"], dtype="<f4") for row in batch])
            similarities = matrix @ vector
            for row, similarity in zip(batch, similarities, strict=True):
                key = (row["model"], row["record_id"])
                if similarity >= 0.70 and (key not in scores or similarity > scores[key][0]):
                    scores[key] = (float(similarity), row["text"])
        output = []
        # This model has a high cosine baseline. Drop weak matches relative to the best hit too.
        threshold = max(0.70, max((value[0] for value in scores.values()), default=0.70) - 0.10)
        for key, (score, excerpt) in sorted(scores.items(), key=lambda item: -item[1][0])[:limit]:
            if score < threshold:
                continue
            row = self.db.execute("SELECT * FROM records WHERE model=? AND id=?", key).fetchone()
            if row:
                output.append({**dict(row), "excerpt": excerpt, "similarity": score})
        return output


def exact_record(query: str) -> tuple[str | None, int] | None:
    if re.fullmatch(r"#?\d+", query):
        return None, int(query.lstrip("#"))
    url = urlsplit(query)
    if url.scheme not in {"http", "https"}:
        return None
    params = {**parse_qs(url.query), **parse_qs(url.fragment)}
    model = params.get("model", [None])[0]
    record_id = params.get("id", [""])[0]
    if model in {"project.task", "project.project"} and record_id.isdigit():
        return model, int(record_id)
    # Odoo short URLs, including /odoo/project/4/tasks/123.
    matches = list(re.finditer(r"/(project\.task|tasks?|project\.project|projects?)/(\d+)(?=/|$)", url.path))
    if matches:
        last = matches[-1]
        return ("project.task" if last[1] in {"project.task", "task", "tasks"} else "project.project"), int(
            last[2]
        )
    return None


def launcher_row(record: dict, query: str = "") -> dict:
    description = record.get("excerpt", record["description"])
    if query:
        tokens = re.findall(r"\w+", query.casefold())
        positions = [description.casefold().find(token) for token in tokens]
        position = min((value for value in positions if value >= 0), default=0)
        start = max(0, position - 60)
        description = ("…" if start else "") + description[start:]
    excerpt = " ".join(description.split())[:230]
    kind = "Task" if record["model"] == "project.task" else "Project"
    return {
        "title": record["title"],
        "detail": " · ".join(
            filter(
                None,
                [
                    f"{kind} #{record['id']}",
                    record["project"],
                    record["stage"],
                    excerpt,
                ],
            )
        ),
        "icon": "glyph:globe",
        "actions": [{"title": "Open in Odoo", "type": "open", "target": record["url"]}],
    }
