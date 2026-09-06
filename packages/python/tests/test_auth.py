from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
from conftest import load

from anvisa.auth import Credentials, TokenAuth, read_env_file
from anvisa.errors import AuthError, CredentialsError

TOKEN_PATH = "/auth/realms/externo/protocol/openid-connect/token"


class Server:
    """Counts token requests and lets a test force a 401 on the API call."""

    def __init__(self):
        self.token_requests = []
        self.fail_next_api_call = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            self.token_requests.append(request)
            return httpx.Response(200, json=load("token.json"))
        if request.headers.get("Authorization") != "Bearer FAKE.TOKEN.FOR-TESTS":
            return httpx.Response(401)
        if self.fail_next_api_call:
            self.fail_next_api_call = False
            return httpx.Response(401)
        return httpx.Response(200, json={"ok": True})


@pytest.fixture
def server():
    return Server()


def make_client(server, clock):
    auth = TokenAuth(Credentials("my-id", "my-secret"), clock=clock)
    return httpx.Client(
        base_url="https://api.example",
        transport=httpx.MockTransport(server),
        headers={"User-Agent": "anvisa-test/0"},
        auth=auth,
    ), auth


def test_token_is_fetched_once_and_sent_as_form(server):
    now = [1000.0]
    client, auth = make_client(server, lambda: now[0])
    assert client.get("/x").status_code == 200
    assert client.get("/y").status_code == 200
    assert len(server.token_requests) == 1
    form = parse_qs(server.token_requests[0].content.decode())
    assert form == {
        "grant_type": ["client_credentials"],
        "client_id": ["my-id"],
        "client_secret": ["my-secret"],
    }
    assert server.token_requests[0].headers["User-Agent"] == "anvisa-test/0"
    assert auth.token_valid


def test_token_refetched_near_expiry(server):
    now = [1000.0]
    client, _ = make_client(server, lambda: now[0])
    client.get("/x")
    now[0] += 1740 - 30  # inside the 60 s margin
    client.get("/x")
    assert len(server.token_requests) == 2


def test_401_triggers_one_refetch_and_retry(server):
    client, _ = make_client(server, lambda: 0.0)
    server.fail_next_api_call = True
    assert client.get("/x").status_code == 200
    assert len(server.token_requests) == 2


def test_token_endpoint_failure_raises():
    def deny(request):
        return httpx.Response(400, json={"error": "invalid_client"})

    client = httpx.Client(
        transport=httpx.MockTransport(deny), auth=TokenAuth(Credentials("a", "b"))
    )
    with pytest.raises(AuthError, match="HTTP 400"):
        client.get("https://api.example/x")


def test_credentials_from_env_and_file(tmp_path: Path):
    creds = Credentials.from_env(
        {"ANVISA_CLIENT_ID": "e1", "ANVISA_CLIENT_SECRET": "e2"}, path=tmp_path / "none"
    )
    assert (creds.client_id, creds.client_secret) == ("e1", "e2")

    file = tmp_path / "credentials.env"
    file.write_text("# comment\nexport CLIENT_ID='f1'\nCLIENT_SECRET=\"f2\"\n\n")
    creds = Credentials.from_env({}, path=file)
    assert (creds.client_id, creds.client_secret) == ("f1", "f2")
    assert "f2" not in repr(creds)

    with pytest.raises(CredentialsError, match="ANVISA_CLIENT_ID"):
        Credentials.from_env({}, path=tmp_path / "missing")


def test_read_env_file_missing_is_empty(tmp_path: Path):
    assert read_env_file(tmp_path / "nope") == {}
