"""Telegram notifications on the standard library (sendMessage / sendDocument by file_id)."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

# post(url, form_dict) -> (status, response_dict)
Post = Callable[[str, dict], "tuple[int, dict]"]


def urllib_post(url: str, form: dict) -> "tuple[int, dict]":
    data = urllib.parse.urlencode(form).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 - fixed https Telegram URL
            return resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "{}")
        except ValueError:
            return e.code, {}


class Notifier:
    """Never raises: notifications must not break a download run."""

    def __init__(self, token: str, post: Post | None = None):
        self.token = token
        self.post = post or urllib_post
        self.errors: list[str] = []

    def _call(self, method: str, form: dict) -> bool:
        if not self.token:
            return False
        try:
            status, payload = self.post(f"https://api.telegram.org/bot{self.token}/{method}", form)
        except Exception as e:  # noqa: BLE001
            self.errors.append(f"{method}: {e}")
            return False
        ok = status == 200 and bool(payload.get("ok"))
        if not ok:
            self.errors.append(f"{method}: {status} {payload.get('description', '')}")
        return ok

    def send_message(self, chat_id: int | None, text: str) -> bool:
        if chat_id is None:
            return False
        return self._call("sendMessage", {"chat_id": str(chat_id), "text": text[:4000]})

    def send_document(self, chat_id: int | None, file_id: str, caption: str = "") -> bool:
        if chat_id is None or not file_id:
            return False
        return self._call("sendDocument", {"chat_id": str(chat_id), "document": file_id, "caption": caption[:1024]})
