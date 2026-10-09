"""The resolved spec against the client. Every path the client calls is in the spec, the paths
it does not call are exactly the domains ANVISA added on 2026-10-01, and the overlay's `x-*`
corrections are what the client does: `Accept: */*` on the downloads, required filters
enforced before any request or sent in the body."""

from __future__ import annotations

import ast
import inspect
import re

import pytest
from conftest import spec

from anvisa import client as client_module
from anvisa.errors import MissingFilterError

WRAPPERS = {
    "get": "GET",
    "get_bytes": "GET",
    "get_stream": "GET",
    "post": "POST",
    "post_bytes": "POST",
}
DOWNLOADERS = {"get_bytes", "post_bytes", "get_stream"}
NEW_DOMAINS = ("/api/v1/tabaco", "/api/v1/saude", "/api/v1/certificado")  # + certificadoMedicamento
PARAM = re.compile(r"\{[^}]*\}")


def wrapped_operations() -> dict[tuple[str, str], str]:
    """(METHOD, path with every parameter as `{}`) -> the `Client` method used, for each
    `self._client.<get|post|...>(<literal>, ...)` in client.py. Read from the source, not by
    calling anything: the paths are literals or f-strings, four of them on the line after
    the call, which is why this is `ast` and not a grep."""
    found = {}
    for node in ast.walk(ast.parse(inspect.getsource(client_module))):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        func = node.func
        on_client = (
            isinstance(func.value, ast.Attribute)
            and func.value.attr == "_client"
            and isinstance(func.value.value, ast.Name)
            and func.value.value.id == "self"
        )
        if not on_client or func.attr not in WRAPPERS:
            continue
        arg = node.args[0]
        if isinstance(arg, ast.Constant):
            path = arg.value
        elif isinstance(arg, ast.JoinedStr):
            path = "".join(v.value if isinstance(v, ast.Constant) else "{}" for v in arg.values)
        else:
            pytest.fail(f"client.py:{node.lineno}: the path is not a literal")
        found[(WRAPPERS[func.attr], path)] = func.attr
    return found


def operations() -> dict[tuple[str, str], dict]:
    return {
        (method.upper(), PARAM.sub("{}", path)): op
        for path, item in spec()["paths"].items()
        for method, op in item.items()
        if method in ("get", "post", "put", "delete", "patch")
    }


def test_every_wrapped_path_is_in_the_spec():
    wrapped = wrapped_operations()
    assert len(wrapped) == 32
    assert set(wrapped) <= set(operations())


def test_unwrapped_paths_are_exactly_the_new_domains():
    """Fails when one of them gets wrapped, or when ANVISA adds an operation: update it."""
    unwrapped = set(operations()) - set(wrapped_operations())
    assert all(path.startswith(NEW_DOMAINS) for _, path in unwrapped), sorted(unwrapped)
    assert len(unwrapped) == 27


def test_accept_any_matches_download_methods():
    """The overlay marks the operations that refuse `Accept: application/json` with
    `x-accept: */*`; those, and only those, go through the byte-returning methods."""
    ops = operations()
    for key, wrapper in wrapped_operations().items():
        assert (ops[key].get("x-accept") == "*/*") == (wrapper in DOWNLOADERS), key


# `x-required-filters` on the overlay, and the call that exercises each rule: `anyOf` means a
# call with no filter must fail locally, `allOf` means the body sent must carry those keys.
WITHOUT_FILTERS = {("POST", "/api/v1/udi"): lambda c: c.udi.search()}
WITH_FILTERS = {
    ("POST", "/api/v1/fila/consulta"): lambda c: c.fila.query(167),
    ("POST", "/api/v1/fila/downloadfila"): lambda c: c.fila.download(167),
    ("POST", "/api/v1/lista/consulta"): lambda c: c.lista.query(2141),
    ("POST", "/api/v1/lista/downloadlista"): lambda c: c.lista.download(2141),
}


def test_required_filters_are_enforced(client, fake_api):
    ops = operations()
    rules = {k: op["x-required-filters"] for k, op in ops.items() if "x-required-filters" in op}
    # a new rule in the overlay must land in one of the maps, not be skipped silently
    assert {k for k, r in rules.items() if r.get("anyOf")} == set(WITHOUT_FILTERS)
    assert {k for k, r in rules.items() if r.get("allOf")} == set(WITH_FILTERS)
    for call in WITHOUT_FILTERS.values():
        with pytest.raises(MissingFilterError):
            call(client)
        assert fake_api.requests == []  # rejected before even the token request
    for key, call in WITH_FILTERS.items():
        call(client)
        body = fake_api.json_bodies()[-1]
        assert set(rules[key]["allOf"]) <= set(body["filter"]), key
        if ops[key].get("x-paginated") is False:
            assert not {"page", "size"} & set(body), key  # the API ignores them; not sent
