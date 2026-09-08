"""Shared fixtures: a Client whose transport answers from the recorded responses."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from anvisa.auth import Credentials
from anvisa.client import Client
from anvisa.throttle import Throttle

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "consultas-externas"
MANIFEST = json.loads((FIXTURES / "manifest.json").read_text(encoding="utf-8"))


def load(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def headers_only(path: Path) -> tuple[dict[str, str], bytes]:
    """A `headers_*.txt` fixture: too big a body to keep, so only its headers were saved.

    The status line and the note after the blank line are dropped; the body is empty, which
    is enough to test the headers-driven parts (file name, content type, streaming)."""
    block = path.read_text(encoding="utf-8").split("\n\n")[0].splitlines()[1:]
    fields = (line.split(":", 1) for line in block if ":" in line)
    headers = {k.strip(): v.strip() for k, v in fields}
    headers.pop("Transfer-Encoding", None)  # httpx frames the body itself
    return {k: v for k, v in headers.items() if v != "(absent)"}, b""


def response_for(name: str) -> httpx.Response:
    """An httpx.Response rebuilt from one manifest entry (status, rate-limit headers, body)."""
    entry = next(e for e in MANIFEST["responses"] if e["name"] == name)
    file = FIXTURES / entry["file"]
    if file.name.startswith("headers_"):
        headers, content = headers_only(file)
    else:
        headers, content = {"Content-Type": "application/json"}, file.read_bytes()
        for key in ("content_type", "content_disposition"):
            if key in entry:
                headers[key.replace("_", "-").title()] = entry[key]
    if "remaining" in entry:
        headers.update(
            {
                "X-RateLimit-Remaining": str(entry["remaining"]),
                "X-RateLimit-Burst-Capacity": "25",
                "X-RateLimit-Replenish-Rate": "1",
            }
        )
    return httpx.Response(
        entry["status"],
        headers=headers,
        content=content,
        request=httpx.Request(entry["method"], "https://example" + entry["path"]),
    )


class FakeApi:
    """Routes (method, path) to the first successful manifest entry; records every request."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.routes: dict[tuple[str, str], list[dict]] = {}
        for e in MANIFEST["responses"]:
            if e["status"] == 200:
                self.routes.setdefault((e["method"], e["path"]), []).append(e)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path.removeprefix("/consultas-externas-api")  # the base_url prefix
        candidates = self.routes.get((request.method, path)) or []
        if not candidates:
            return httpx.Response(404, json={"error": f"no fixture for {request.method} {path}"})
        try:
            body = json.loads(request.read()) if request.content else None
        except ValueError:  # the token request is form-encoded
            body = None
        # several fixtures on one path (e.g. fila/consulta for two subfilas): match the request body
        entry = next((e for e in candidates if e.get("request") == body), candidates[0])
        return response_for(entry["name"])

    def json_bodies(self) -> list[dict]:
        return [
            json.loads(r.content)
            for r in self.requests
            if r.method == "POST" and r.content and r.content[:1] == b"{"
        ]


@pytest.fixture
def fake_api() -> FakeApi:
    return FakeApi()


@pytest.fixture
def client(fake_api: FakeApi) -> Client:
    with Client(
        Credentials("id", "secret"),
        transport=httpx.MockTransport(fake_api.handler),
        throttle=Throttle(sleep=lambda s: None),
    ) as c:
        yield c
