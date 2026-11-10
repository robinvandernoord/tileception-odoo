"""Read-only JSON-2 transport adapted from the standalone Odoo skill client."""

from __future__ import annotations

import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .config import NO_PROJECT, Config

MODELS = ("project.project", "project.task")


class CanonicalRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        before = urlsplit(req.full_url)
        after = urlsplit(newurl)
        if (
            code not in {301, 302, 307, 308}
            or before.scheme != "https"
            or after.scheme != "https"
            or not before.hostname
            or not after.hostname
            or before.hostname.removeprefix("www.") != after.hostname.removeprefix("www.")
            or (before.port or 443) != (after.port or 443)
            or after.username is not None
            or after.password is not None
            or before.path != after.path
            or before.query != after.query
            or after.fragment
        ):
            raise ValueError(
                "Odoo redirected outside its HTTPS www alias; configure the final JSON-2 endpoint"
            )
        # Canonical host redirects retain the JSON-2 POST and bearer authentication.
        # Never pass either to another domain, port, path or an HTTP endpoint.
        return Request(newurl, data=req.data, headers=dict(req.headers), method=req.get_method())


class OdooClient:
    def __init__(self, config: Config):
        self.config = config
        self.endpoint = config.json2_url
        self.key = config.api_key()
        self.opener = build_opener(CanonicalRedirect())

    def call(self, model: str, method: str, body: dict[str, Any]) -> Any:
        if model not in (*MODELS, "project.tags") or method not in {"fields_get", "read", "search_read"}:
            raise ValueError("Only read-only project/task/tag operations are allowed")
        headers = {"Content-Type": "application/json", "Authorization": f"bearer {self.key}"}
        if self.config.database:
            headers["X-Odoo-Database"] = self.config.database
        request = Request(
            f"{self.endpoint}/{model}/{method}",
            data=json.dumps(body).encode(),
            headers=headers,
            method="POST",
        )
        try:
            with self.opener.open(request, timeout=30) as response:
                self.endpoint = response.geturl().removesuffix(f"/{model}/{method}")
                raw = response.read(32 * 1024 * 1024 + 1)
                if len(raw) > 32 * 1024 * 1024:
                    raise ValueError("Odoo response exceeded 32 MiB")
                result = json.loads(raw)
        except HTTPError as error:
            raise ValueError(f"Odoo JSON-2 returned HTTP {error.code}") from None
        except URLError:
            raise ValueError("Could not reach Odoo JSON-2; cached search is still available") from None
        except UnicodeError, json.JSONDecodeError:
            raise ValueError("Odoo returned invalid JSON") from None
        if not isinstance(result, (list, dict)) or isinstance(result, dict) and "error" in result:
            raise ValueError("Odoo returned an API error or invalid response")
        return result

    def metadata(self, model: str) -> dict:
        result = self.call(model, "fields_get", {"attributes": ["type", "relation"]})
        if not isinstance(result, dict):
            raise ValueError(f"Invalid metadata for {model}")
        return result

    def project_domain(self, model: str) -> list:
        """Restrict a sync to the configured projects; the exclude list always wins."""
        field = "id" if model == "project.project" else "project_id"
        domain = []
        for ids, negate in ((self.config.include_projects, False), (self.config.exclude_projects, True)):
            if not ids:
                continue
            real = sorted(ids - {NO_PROJECT})
            if model == "project.project":
                terms = [[field, "in", real]]
            else:
                terms = [[field, "in", real]] if real else []
                if NO_PROJECT in ids:
                    terms.append([field, "=", False])
            if not terms:
                # Only the no-project pseudo-project is listed, which matches no real project.
                terms = [[field, "in", []]]
            # "!" keeps negation explicit, so tasks without a project are not silently included or dropped.
            expression = ["|"] * (len(terms) - 1) + terms
            domain += ["!", *expression] if negate else expression
        return domain

    def projects(self) -> list[dict]:
        """Active projects for the picker, by name."""
        result = self.call(
            "project.project",
            "search_read",
            {"domain": [], "fields": ["id", "name"], "order": "name asc, id asc"},
        )
        if not isinstance(result, list) or not all(
            isinstance(row, dict) and isinstance(row.get("id"), int) for row in result
        ):
            raise ValueError("Invalid project list from Odoo")
        return result

    def records(self, model: str, since: str | None):
        metadata = self.metadata(model)
        fields = ["id", "name", "write_date"]
        optional = ["description", "active", "stage_id", "tag_ids"]
        if model == "project.task":
            optional.append("project_id")
        fields += [name for name in optional if name in metadata]
        if not all(name in metadata for name in fields[:3]):
            raise ValueError(f"{model} must expose id, name and write_date")
        cursor = 0
        while True:
            domain = [["id", ">", cursor]]
            if since:
                domain.append(["write_date", ">=", since])
            domain += self.project_domain(model)
            page = self.call(
                model,
                "search_read",
                {
                    "domain": domain,
                    "fields": fields,
                    "order": "id asc",
                    "limit": 200,
                    "context": {"active_test": False},
                },
            )
            if not isinstance(page, list):
                raise ValueError(f"Invalid records for {model}")
            if not page:
                return
            for record in page:
                if not isinstance(record, dict) or not isinstance(record.get("id"), int):
                    raise ValueError(f"Invalid record in {model}")
                if record["id"] <= cursor:
                    raise ValueError("Odoo pagination did not advance")
                cursor = record["id"]
                yield record

    def tags(self, ids: set[int]) -> dict[int, str]:
        names = {}
        ordered = sorted(ids)
        for start in range(0, len(ordered), 200):
            result = self.call(
                "project.tags",
                "read",
                {
                    "ids": ordered[start : start + 200],
                    "fields": ["name"],
                    "context": {"active_test": False},
                },
            )
            if not isinstance(result, list):
                raise ValueError("Invalid project tag response")
            names.update({row["id"]: row["name"] for row in result})
        return names


def relation_name(value: Any) -> str:
    return str(value[1]) if isinstance(value, list) and len(value) == 2 else ""
