"""Tiny GitHub Contents API client on the standard library (injectable transport for tests)."""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
from typing import Any, Callable

# transport(method, url, headers, body_bytes_or_None) -> (status_code, response_bytes)
Transport = Callable[[str, str, dict, "bytes | None"], "tuple[int, bytes]"]


class ConflictError(Exception):
    """HTTP 409/422: the file changed (or already exists) since it was read."""


class GitHubError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"GitHub API {status}: {message}")
        self.status = status


def urllib_transport(method: str, url: str, headers: dict, body: "bytes | None") -> "tuple[int, bytes]":
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 - fixed https GitHub URL
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


class GitHubContents:
    def __init__(self, repo: str, token: str, branch: str = "main", transport: Transport | None = None):
        if not repo or "/" not in repo:
            raise ValueError("repo must be OWNER/NAME")
        if not token:
            raise ValueError("token is required")
        self.repo = repo
        self.token = token
        self.branch = branch or "main"
        self.transport = transport or urllib_transport

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "tr-market-engine",
            "Content-Type": "application/json",
        }

    def _url(self, path: str) -> str:
        return f"https://api.github.com/repos/{self.repo}/contents/{path.lstrip('/')}"

    def _call(self, method: str, url: str, payload: dict | None = None) -> "tuple[int, Any]":
        body = json.dumps(payload).encode() if payload is not None else None
        status, raw = self.transport(method, url, self._headers(), body)
        try:
            data = json.loads(raw.decode() or "null")
        except (ValueError, UnicodeDecodeError):
            data = None
        return status, data

    def list_dir(self, path: str) -> list[dict[str, Any]]:
        status, data = self._call("GET", f"{self._url(path)}?ref={self.branch}")
        if status == 404:
            return []
        if status != 200 or not isinstance(data, list):
            raise GitHubError(status, str(data)[:200])
        return [d for d in data if isinstance(d, dict) and d.get("type") == "file"]

    def get_json(self, path: str) -> "tuple[dict | None, str | None]":
        status, data = self._call("GET", f"{self._url(path)}?ref={self.branch}")
        if status == 404:
            return None, None
        if status != 200 or not isinstance(data, dict):
            raise GitHubError(status, str(data)[:200])
        raw = base64.b64decode(data.get("content", "") or b"")
        return json.loads(raw.decode() or "{}"), data.get("sha")

    def put_json(self, path: str, obj: dict, sha: "str | None", message: str) -> str:
        payload: dict[str, Any] = {
            "message": message,
            "content": base64.b64encode(json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True).encode()).decode(),
            "branch": self.branch,
        }
        if sha:
            payload["sha"] = sha
        status, data = self._call("PUT", self._url(path), payload)
        if status in (200, 201) and isinstance(data, dict):
            return str((data.get("content") or {}).get("sha") or "")
        if status in (409, 422):
            raise ConflictError(str(data)[:200])
        raise GitHubError(status, str(data)[:200])

    def delete(self, path: str, sha: str, message: str) -> None:
        status, data = self._call("DELETE", self._url(path), {"message": message, "sha": sha, "branch": self.branch})
        if status in (200, 404):
            return
        if status in (409, 422):
            raise ConflictError(str(data)[:200])
        raise GitHubError(status, str(data)[:200])
