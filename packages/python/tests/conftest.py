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


def response_for(name: str) -> httpx.Response:
    """An httpx.Response rebuilt from one manifest entry (status, rate-limit headers, body)."""
    entry = next(e for e in MANIFEST["responses"] if e["name"] == name)
    headers = {"Content-Type": "application/json"}
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
        content=(FIXTURES / entry["file"]).read_bytes(),
        request=httpx.Request(entry["method"], "https://example" + entry["path"]),
    )


class FakeApi:
    """Routes (method, path) to the first successful manifest entry; records every request."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.routes = {
            (e["method"], e["path"]): e["name"] for e in MANIFEST["responses"] if e["status"] == 200
        }

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path.removeprefix("/consultas-externas-api")  # the base_url prefix
        name = self.routes.get((request.method, path))
        if name is None:
            return httpx.Response(404, json={"error": f"no fixture for {request.method} {path}"})
        return response_for(name)

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
