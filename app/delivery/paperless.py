"""Paperless-ngx upload through its REST API (not the consume folder).

Each user can store their own API token, so documents are owned by the right
Paperless user. PLATEN_PAPERLESS_TOKEN is the fallback.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

import httpx


class PaperlessError(Exception):
    pass


@dataclass
class UploadResult:
    task_id: str
    status: str                 # SUCCESS / FAILURE / PENDING / STARTED ...
    document_id: int | None = None
    message: str = ""


class PaperlessClient:
    def __init__(self, base_url: str, token: str, transport: httpx.BaseTransport | None = None,
                 timeout: float = 60.0):
        if not base_url:
            raise PaperlessError("Paperless is not configured (PLATEN_PAPERLESS_URL)")
        if not token:
            raise PaperlessError("No Paperless API token. Add one on your account page.")
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(
            base_url=self.base_url, transport=transport, timeout=timeout,
            headers={"Authorization": f"Token {token}", "Accept": "application/json"},
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> PaperlessClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        try:
            response = self._client.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            raise PaperlessError(f"Cannot reach Paperless: {exc}") from exc
        if response.status_code in (401, 403):
            raise PaperlessError("Paperless rejected the API token")
        if response.status_code >= 400:
            raise PaperlessError(f"Paperless returned {response.status_code}: "
                                 f"{response.text[:300]}")
        return response

    def _all(self, path: str) -> list[dict]:
        items, url = [], f"{path}?page_size=1000"
        while url:
            data = self._request("GET", url).json()
            items.extend(data.get("results", []))
            url = data.get("next")  # absolute URL; httpx uses it as-is
        return items

    def metadata(self) -> dict[str, list[dict]]:
        """Tags, correspondents and document types as [{id, name}] lists."""
        out = {}
        for key, path in (("tags", "/api/tags/"), ("correspondents", "/api/correspondents/"),
                          ("document_types", "/api/document_types/")):
            out[key] = sorted(
                ({"id": i["id"], "name": i["name"]} for i in self._all(path)),
                key=lambda i: i["name"].lower(),
            )
        return out

    def upload(self, pdf: Path, title: str = "", tags: list[int] | None = None,
               correspondent: int | None = None, document_type: int | None = None) -> str:
        data: dict[str, object] = {}
        if title:
            data["title"] = title
        if tags:
            data["tags"] = [str(t) for t in tags]
        if correspondent:
            data["correspondent"] = str(correspondent)
        if document_type:
            data["document_type"] = str(document_type)
        with pdf.open("rb") as fh:
            response = self._request(
                "POST", "/api/documents/post_document/",
                data=data, files={"document": (pdf.name, fh, "application/pdf")},
            )
        task_id = response.json()
        if isinstance(task_id, dict):  # some versions wrap it
            task_id = task_id.get("task_id") or task_id.get("id")
        if not task_id:
            raise PaperlessError("Paperless did not return a task id")
        return str(task_id)

    def task(self, task_id: str) -> UploadResult:
        data = self._request("GET", "/api/tasks/", params={"task_id": task_id}).json()
        results = data if isinstance(data, list) else data.get("results", [])
        if not results:
            return UploadResult(task_id, "PENDING")
        t = results[0]
        doc = t.get("related_document")
        return UploadResult(
            task_id=task_id, status=str(t.get("status", "PENDING")).upper(),
            document_id=int(doc) if doc not in (None, "") else None,
            message=t.get("result") or "",
        )

    def wait(self, task_id: str, timeout: float, interval: float = 3.0) -> UploadResult:
        deadline = time.monotonic() + timeout
        while True:
            result = self.task(task_id)
            if result.status in ("SUCCESS", "FAILURE", "REVOKED"):
                return result
            if time.monotonic() >= deadline:
                return result
            time.sleep(interval)

    def document_url(self, document_id: int) -> str:
        return f"{self.base_url}/documents/{document_id}/details"
