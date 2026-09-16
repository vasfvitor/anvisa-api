"""Snapshot ANVISA's OpenAPI document and portal documentation for the drift check.

The OpenAPI document at consultas-externas-api/v3/api-docs covers only part of the API.
The rest (certificados, empresa nacional/internacional, dossiê, alimentos, produtos de
saúde) is documented solely in the portal's own pages, which its backend serves as JSON,
unauthenticated:

    GET https://api-gateway.prd.apps.anvisa.gov.br/portal-apis/api/v1/public/portal/menus
    GET https://api-gateway.prd.apps.anvisa.gov.br/portal-apis/api/v1/public/portal/paginas?rota=<route>

(`rota` must be sent with `/` percent-encoded as %2F; a literal slash is a 404.)
This script refreshes spec/consultas-externas.openapi.json (ANVISA's document, reformatted
as pretty-printed JSON, otherwise untouched) and writes the menu tree, the portal
backend's own spec and every page of type PAGINA under spec/portal/ in the same format, so
`git diff` shows exactly what ANVISA changed. No credentials needed; ~12 requests. Note that
the gateway's rate limit (burst 25, refill 1/s) applies to these unauthenticated requests
too, from the same bucket as the authenticated API.

The OpenAPI documents are normalized before being written: each operation's `responses` map and
`components.schemas` are sorted by key, and the `nonce` query parameter is stripped from the
OAuth2 flow URLs. All three are server runtime state, not API surface (springdoc serializes those
maps in hash order, which differs between requests, and the Keycloak nonce is regenerated on
every restart), and they made the drift check go red with nothing to look at. Everything else,
in particular the property order inside each schema, keeps ANVISA's key order.
"""

from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

GATEWAY = "https://api-gateway.prd.apps.anvisa.gov.br"
SNGPC_SPEC = "https://sngpc-api.anvisa.gov.br/swagger/v1/swagger.json"  # separate service, watched only
PORTAL = f"{GATEWAY}/portal-apis"
USER_AGENT = "anvisa-api spec snapshot (+https://github.com/vasfvitor/anvisa-api)"
SPEC_DIR = Path(__file__).resolve().parent
OUT = SPEC_DIR / "portal"


def get_json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 - fixed https host
        return json.load(resp)


def dump(path: Path, data) -> None:
    # ANVISA's key order is kept: sorting would reorder properties and churn the generated models
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {path.relative_to(SPEC_DIR.parent)}")


def _strip_nonce(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    pairs = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    query = [(k, v) for k, v in pairs if k != "nonce"]
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))


def normalize_openapi(doc: dict) -> dict:
    """Drop the parts of an OpenAPI document that change without the API changing."""
    for item in (doc.get("paths") or {}).values():
        for op in item.values():
            if isinstance(op, dict) and isinstance(op.get("responses"), dict):
                op["responses"] = {code: op["responses"][code] for code in sorted(op["responses"])}
    components = doc.get("components") or {}
    if isinstance(components.get("schemas"), dict):
        schemas = components["schemas"]
        components["schemas"] = {name: schemas[name] for name in sorted(schemas)}
    for scheme in (components.get("securitySchemes") or {}).values():
        for flow in (scheme.get("flows") or {}).values():
            for key in ("authorizationUrl", "tokenUrl", "refreshUrl"):
                if isinstance(flow.get(key), str):
                    flow[key] = _strip_nonce(flow[key])
    return doc


def pages(tree) -> list[dict]:
    """Every node of tipo PAGINA, depth-first, in menu order."""
    found = []
    for node in tree:
        if node.get("tipo") == "PAGINA" and node.get("rota"):
            found.append(node)
        found.extend(pages(node.get("filhos") or node.get("children") or []))
    return found


def main() -> int:
    OUT.mkdir(exist_ok=True)
    menus = get_json(f"{PORTAL}/api/v1/public/portal/menus")
    dump(OUT / "menus.json", menus)
    dump(OUT / "portal-apis.openapi.json", normalize_openapi(get_json(f"{PORTAL}/v3/api-docs")))
    dump(OUT / "sngpc.openapi.json", normalize_openapi(get_json(SNGPC_SPEC)))
    dump(
        SPEC_DIR / "consultas-externas.openapi.json",
        normalize_openapi(get_json(f"{GATEWAY}/consultas-externas-api/v3/api-docs")),
    )
    tree = menus if isinstance(menus, list) else menus.get("menus") or menus.get("content") or []
    for node in pages(tree):
        rota = node["rota"]
        page = get_json(f"{PORTAL}/api/v1/public/portal/paginas?rota={urllib.parse.quote(rota, safe='')}")
        # `conteudo` is JSON stored as a string on ENDPOINTS pages; decode it so diffs are readable.
        if isinstance(page.get("conteudo"), str):
            try:
                page["conteudo"] = json.loads(page["conteudo"])
            except ValueError:
                pass
        dump(OUT / f"{rota.replace('/', '__')}.json", page)
    return 0


if __name__ == "__main__":
    sys.exit(main())
