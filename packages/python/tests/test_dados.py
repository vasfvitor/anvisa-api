"""anvisa.dados: parsing ANVISA's CSV dialect, downloading, converting to Parquet, building."""

from __future__ import annotations

import dataclasses
import hashlib
import importlib.util
import json
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import httpx
import pytest
from conftest import headers_only
from typer.testing import CliRunner

from anvisa import cli
from anvisa.dados import CATALOG, Unchanged, build, select
from anvisa.dados.catalog import ALIMENTOS, ALIMENTOS_RESULTADO, TYPES
from anvisa.dados.fetch import download, http_client
from anvisa.dados.parse import (
    check_header,
    decode,
    normalize,
    read_header,
    records,
    unescape,
)
from anvisa.errors import DadosError, SchemaDriftError

DADOS = Path(__file__).resolve().parents[3] / "fixtures" / "dados"
SAMPLES = {
    ALIMENTOS.name: "alimentos_head.csv",
    ALIMENTOS_RESULTADO.name: "alimentos_resultado_head.csv",
}
HEADERS = {
    ALIMENTOS.name: "headers_alimentos.txt",
    ALIMENTOS_RESULTADO.name: "headers_alimentos_resultado.txt",
}
NOW = datetime(2026, 10, 6, 21, 3, 12, tzinfo=timezone.utc)
needs_duckdb = pytest.mark.skipif(
    importlib.util.find_spec("duckdb") is None, reason="needs the anvisa[dados] extra"
)


def sample(ds) -> Path:
    return DADOS / SAMPLES[ds.name]


def sample_records(ds) -> list[list[str]]:
    return list(records(decode(sample(ds).read_bytes())))


def write_dialect(path: Path, rows: list[list[str]]) -> Path:
    """Write rows the way ANVISA does: every field quoted, nothing escaped, Windows-1252."""
    path.write_bytes(
        "".join(";".join(f'"{v}"' for v in row) + "\n" for row in rows).encode("cp1252")
    )
    return path


class FakeDados:
    """Serves the fixture samples at their catalog URLs with the recorded headers, honours
    If-None-Match, and serves `manifest` (when set) at MANIFEST_URL."""

    MANIFEST_URL = "https://example.github.io/anvisa-api/manifest.json"

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.manifest: dict | None = None
        self.files = {}
        for ds in CATALOG:
            headers, _ = headers_only(DADOS / HEADERS[ds.name])
            headers.pop("Content-Length")  # recorded for the full file; httpx sets the sample's
            self.files[ds.url] = (headers, sample(ds).read_bytes())

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        if url == self.MANIFEST_URL:
            if self.manifest is None:
                return httpx.Response(404, text="not found")
            return httpx.Response(200, json=self.manifest)
        if url not in self.files:
            return httpx.Response(404, text="not found")
        headers, body = self.files[url]
        if request.headers.get("If-None-Match") == headers["ETag"]:
            return httpx.Response(304, headers={"ETag": headers["ETag"]})
        return httpx.Response(200, headers=headers, content=body)

    def client(self) -> httpx.Client:
        return http_client(transport=httpx.MockTransport(self.handler))

    def data_requests(self) -> list[httpx.Request]:
        return [r for r in self.requests if str(r.url) != self.MANIFEST_URL]


@pytest.fixture
def fake_dados() -> FakeDados:
    return FakeDados()


# --- catalog and dialect ------------------------------------------------------


def test_catalog_is_consistent():
    assert len({d.name for d in CATALOG}) == len(CATALOG)
    for ds in CATALOG:
        assert set(ds.columns.values()) <= TYPES
        assert set(ds.sort) <= set(ds.columns)
        assert all(ds.columns[c] == "VARCHAR" for c in ds.unescape)
        assert all(ds.columns[c] in ("DATE", "TIMESTAMP") for c in ds.formats)
        assert ds.url.startswith("https://dados.anvisa.gov.br/") and ds.url.endswith(ds.file)
    assert select(["alimentos"]) == CATALOG  # the group
    assert select(["alimentos_resultado"]) == (ALIMENTOS_RESULTADO,)
    with pytest.raises(DadosError, match="unknown dataset"):
        select(["cosmeticos"])


def test_fixture_header_matches_catalog():
    for ds in CATALOG:
        assert read_header(sample(ds)) == list(ds.columns)
        check_header(ds, read_header(sample(ds)))


def test_header_drift_is_loud():
    header = list(ALIMENTOS.columns)
    header[2] = "NU_PROCESSO_NOVO"
    with pytest.raises(SchemaDriftError) as exc:
        check_header(ALIMENTOS, header)
    message = str(exc.value)
    assert "NU_PROCESSO_NOVO" in message and "'NU_PROCESSO'" in message
    assert "column 3" in message and "TA_CONSULTA_ALIMENTOS.CSV" in message


def test_dialect_keeps_every_record_whole():
    """The fixtures hold the cases that break stock CSV readers (see parse.py)."""
    for ds in CATALOG:
        rows = sample_records(ds)
        assert {len(r) for r in rows} == {len(ds.columns)}
    main = sample_records(ALIMENTOS)
    names = [r[0] for r in main]
    assert any("\n" in n for n in names)  # a line break inside a quoted field
    assert any('"' in n for n in names)  # an unescaped quote inside a quoted field
    detail = {r[0]: r for r in sample_records(ALIMENTOS_RESULTADO)[1:]}
    assert 'biscoito tipo "cookies"' in detail["429807342"][12]
    ending = next(r for r in detail.values() if r[15] == "3929988")
    assert ending[12].endswith('brilhante. "')  # written as `brilhante. "";"Contém...`
    assert ending[13] == "Contém Glúten - Não | Contém Lactose - Não"


def test_decode_is_windows_1252():
    assert decode(b"LEDA SIM\xd5ES \x96 FI 6\x92 DR\x99") == "LEDA SIMÕES – FI 6’ DR™"
    assert decode(b"\x81\x9d") == "\x81\x9d"  # undefined in cp1252: passed through, no error
    text = decode(sample(ALIMENTOS).read_bytes())
    assert "–" in text and not re.search("[\x80-\x9f]", text)


def test_unescape_only_terminated_entities():
    assert unescape("L&apos;ANA MED ; &quot;X&quot; ; A&#8208;B") == 'L\'ANA MED ; "X" ; A‐B'
    assert unescape("M&notícia & P&G &amp") == "M&notícia & P&G &amp"


def test_normalize_skips_and_reports_short_records(tmp_path):
    rows = sample_records(ALIMENTOS)
    rows[3] = rows[3][:-1]
    src = write_dialect(tmp_path / "in.csv", rows)
    result = normalize(ALIMENTOS, src, tmp_path / "out.csv")
    assert result.rejected == (3,) and result.rows == len(rows) - 2


# --- download -----------------------------------------------------------------


def test_download_streams_via_part(fake_dados, tmp_path):
    with fake_dados.client() as http:
        source = download(http, ALIMENTOS, tmp_path)
    body = sample(ALIMENTOS).read_bytes()
    assert source.etag == '"20230eb-65d1981268f1f"'
    assert source.last_modified == "Mon, 05 Oct 2026 15:26:22 GMT"
    assert source.bytes == len(body) and source.sha256 == hashlib.sha256(body).hexdigest()
    assert (tmp_path / ALIMENTOS.file).read_bytes() == body
    assert not list(tmp_path.glob("*.part"))
    request = fake_dados.requests[0]
    assert request.headers["User-Agent"].startswith("anvisa-python/")
    assert request.headers["Accept-Encoding"] == "identity"
    assert "If-None-Match" not in request.headers


def test_download_304_returns_none(fake_dados, tmp_path):
    with fake_dados.client() as http:
        assert download(http, ALIMENTOS, tmp_path, etag='"20230eb-65d1981268f1f"') is None
        assert download(http, ALIMENTOS, tmp_path, last_modified="Mon, 05 Oct 2026") is not None
    assert fake_dados.requests[1].headers["If-Modified-Since"] == "Mon, 05 Oct 2026"
    assert [p.name for p in tmp_path.iterdir()] == [ALIMENTOS.file]


def test_download_truncated_is_an_error(tmp_path):
    def handler(request):
        return httpx.Response(200, headers={"Content-Length": "100"}, content=b"short")

    with (
        http_client(transport=httpx.MockTransport(handler)) as http,
        pytest.raises(DadosError, match="truncated"),
    ):
        download(http, ALIMENTOS, tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_download_http_errors_are_dados_errors(tmp_path):
    def handler(request):
        if "RESULTADO" in str(request.url):
            raise httpx.ConnectError("boom", request=request)
        return httpx.Response(503, text="down")

    with http_client(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(DadosError, match="HTTP 503"):
            download(http, ALIMENTOS, tmp_path)
        with pytest.raises(DadosError, match="boom"):
            download(http, ALIMENTOS_RESULTADO, tmp_path)
    assert list(tmp_path.iterdir()) == []


# --- convert ------------------------------------------------------------------


def converted(ds, tmp_path, **kwargs):
    from anvisa.dados.convert import convert

    csv = tmp_path / ds.file
    csv.write_bytes(sample(ds).read_bytes())
    parquet = tmp_path / f"{ds.name}.parquet"
    return convert(ds, csv, parquet, **kwargs), parquet


@needs_duckdb
def test_convert_types_and_values(tmp_path):
    import duckdb

    _, parquet = converted(ALIMENTOS, tmp_path)
    con = duckdb.connect()
    described = con.sql(f"DESCRIBE SELECT * FROM '{parquet}'").fetchall()
    assert [(name, kind) for name, kind, *_ in described] == [
        (c.lower(), t) for c, t in ALIMENTOS.columns.items()
    ]

    def one(sql):
        return con.sql(sql.replace("FROM T", f"FROM '{parquet}'")).fetchone()

    # MMYYYY and MM/YYYY month of expiry; 122029 is every notificação's placeholder
    sample_row = "SELECT dt_vencimento_registro FROM T WHERE nu_cnpj_empresa = '40208221000174'"
    assert one(sample_row) == (date(2029, 12, 1),)
    assert one("SELECT count(*) FROM T WHERE dt_vencimento_registro = DATE '2031-03-01'")[0] == 1
    assert one("SELECT count(*) FROM T WHERE dt_vencimento_registro IS NULL")[0] == 0
    assert one("SELECT max(dt_carga_etl) FROM T")[0] == datetime(2026, 10, 5)
    assert one(
        "SELECT count(*) FILTER (NOT st_produto_ativo), count(*) FILTER (st_produto_ativo) FROM T"
    ) >= (1, 1)
    marcas = [m for (m,) in con.sql(f"SELECT marcas FROM '{parquet}'").fetchall() if m]
    assert any("L'ANA MED" in m for m in marcas)
    assert not any("&apos;" in m or "&quot;" in m for m in marcas)
    assert one("SELECT count(*) FROM T WHERE no_produto LIKE '%&#%'")[0] == 0
    cnpjs = [c for (c,) in con.sql(f"SELECT nu_cnpj_empresa FROM '{parquet}'").fetchall()]
    assert "00000000091308" in cnpjs and {len(c) for c in cnpjs} == {14}  # zeros kept
    # Windows-1252 0x96 and 0x92, decoded to – and ’ rather than C1 control characters
    assert one("SELECT count(*) FROM T WHERE no_razao_social_empresa LIKE '%COSTA – FI'")[0]
    assert one("SELECT count(*) FROM T WHERE no_produto LIKE '%DE 6’-SIALIL%'")[0]
    # the \xa0\r\n tail is stripped, so the category groups with its clean spelling
    assert one("SELECT count(*) FROM T WHERE ds_categoria_produto LIKE '%infância'")[0] == 1


@needs_duckdb
def test_convert_reports_nulls_added(tmp_path):
    stats, _ = converted(ALIMENTOS, tmp_path)
    assert stats.rows == len(sample_records(ALIMENTOS)) - 1 and stats.rejected == ()
    assert stats.nulls_added["st_produto_ativo"] == 1  # the one "X"
    assert {k for k, v in stats.nulls_added.items() if v} == {"st_produto_ativo"}
    assert stats.loaded_at == "2026-10-05T00:00:00"
    assert not list(tmp_path.glob("*.utf8.csv"))  # the intermediate file is cleaned up


@needs_duckdb
def test_convert_guard_fails_on_mass_nulls(tmp_path):
    from anvisa.dados.convert import convert

    rows = sample_records(ALIMENTOS)
    iso = [[re.sub(r"(\d\d)/(\d\d)/(\d{4})", r"\3-\2-\1", v) for v in r] for r in rows]
    csv = write_dialect(tmp_path / "iso.csv", iso)
    with pytest.raises(DadosError, match="DT_REGULARIZACAO.*did ANVISA change the format"):
        convert(ALIMENTOS, csv, tmp_path / "x.parquet")
    assert not (tmp_path / "x.parquet").exists()
    assert not list(tmp_path.glob("*.utf8.csv"))


@needs_duckdb
def test_convert_guard_fails_on_malformed_records(tmp_path):
    from anvisa.dados.convert import convert

    rows = sample_records(ALIMENTOS)
    rows[5] = rows[5][:-2]
    csv = write_dialect(tmp_path / "short.csv", rows)
    with pytest.raises(DadosError, match="wrong number of fields .first: record 5"):
        convert(ALIMENTOS, csv, tmp_path / "x.parquet")
    assert not list(tmp_path.glob("*.utf8.csv"))  # cleaned up on failure too


@needs_duckdb
def test_parquet_is_sorted_in_small_row_groups(tmp_path):
    """DuckDB rounds ROW_GROUP_SIZE up to a multiple of 2048, so this needs > 4096 rows."""
    import duckdb

    from anvisa.dados.convert import convert

    header, *rows = sample_records(ALIMENTOS)
    cnpj = header.index("NU_CNPJ_EMPRESA")
    many = []
    for i in range(5000):
        row = list(rows[i % len(rows)])
        row[cnpj] = f"{(i * 7919) % 5000:014d}"  # every CNPJ once, in scrambled order
        many.append(row)
    csv = write_dialect(tmp_path / "many.csv", [header, *many])
    stats = convert(ALIMENTOS, csv, tmp_path / "many.parquet", row_group_size=2048)
    assert stats.rows == 5000
    groups = duckdb.sql(
        "SELECT row_group_id, stats_min, stats_max "
        f"FROM parquet_metadata('{tmp_path}/many.parquet')"
        " WHERE path_in_schema = 'nu_cnpj_empresa' ORDER BY row_group_id"
    ).fetchall()
    assert len(groups) == 3
    assert all(lo is not None and hi is not None for _, lo, hi in groups)
    assert all(groups[i][2] <= groups[i + 1][1] for i in range(len(groups) - 1))


# --- build --------------------------------------------------------------------


@needs_duckdb
def test_build_end_to_end(fake_dados, tmp_path):
    import duckdb

    out = tmp_path / "dist"
    with fake_dados.client() as http:
        manifest = build(out, http=http, now=NOW, commit="abc123")
    assert json.loads((out / "manifest.json").read_text(encoding="utf-8")) == manifest
    assert manifest["schema_version"] == 1 and manifest["build_id"] == "20261006T210312Z"
    assert manifest["built_at"] == "2026-10-06T21:03:12Z" and manifest["commit"] == "abc123"
    assert manifest["generator"].startswith("anvisa-python/")
    assert set(manifest["tables"]) == {"alimentos", "alimentos_resultado"}
    main = manifest["tables"]["alimentos"]
    assert main["path"] == "data/20261006T210312Z/alimentos.parquet"
    parquet = out / main["path"]
    assert main["bytes"] == parquet.stat().st_size
    assert main["sha256"] == hashlib.sha256(parquet.read_bytes()).hexdigest()
    assert main["sort"] == ["nu_cnpj_empresa", "nu_processo"]
    assert main["columns"][0] == {"name": "no_produto", "type": "VARCHAR", "source": "NO_PRODUTO"}
    assert main["source"]["etag"] == '"20230eb-65d1981268f1f"'
    assert main["source"]["loaded_at"] == "2026-10-05T00:00:00"
    assert main["rejected_records"] == 0 and main["timezone"] == "America/Sao_Paulo"
    assert main["row_group_size"] == 8192  # the build default
    detail = manifest["tables"]["alimentos_resultado"]
    assert detail["sort"] == ["co_produto", "co_seq_apresentacao_produto"]
    assert detail["row_group_size"] == 2048  # the catalog override, so a product is one group
    products = duckdb.sql(f"SELECT co_produto FROM '{out / detail['path']}'").fetchall()
    assert products == sorted(products)  # a product's apresentações are contiguous
    page = (out / "index.html").read_text(encoding="utf-8")
    assert "alimentos_resultado.parquet" in page and 'href="manifest.json"' in page
    joined = duckdb.sql(
        f"SELECT count(*) FROM '{parquet}' m JOIN "
        f"'{out / manifest['tables']['alimentos_resultado']['path']}' r "
        "USING (co_seq_apresentacao_produto)"
    ).fetchone()[0]
    assert joined >= 10


def published(fake_dados, tmp_path, commit="abc123") -> dict:
    with fake_dados.client() as http:
        manifest = build(tmp_path / "first", http=http, now=NOW, commit=commit)
    fake_dados.manifest = manifest
    fake_dados.requests.clear()
    return manifest


@needs_duckdb
def test_build_skips_when_published_matches(fake_dados, tmp_path):
    first = published(fake_dados, tmp_path)
    with fake_dados.client() as http:
        result = build(
            tmp_path / "second", http=http, skip_unchanged=FakeDados.MANIFEST_URL, commit="abc123"
        )
    assert result == Unchanged(first["build_id"])
    assert not (tmp_path / "second").exists()
    sent = [r.headers.get("If-None-Match") for r in fake_dados.data_requests()]
    assert sent == [t["source"]["etag"] for t in first["tables"].values()]
    manifest_request = fake_dados.requests[0]
    assert manifest_request.headers["Cache-Control"] == "no-cache"


@needs_duckdb
def test_build_rebuilds_when_one_file_changed(fake_dados, tmp_path):
    published(fake_dados, tmp_path)
    headers, body = fake_dados.files[ALIMENTOS.url]
    fake_dados.files[ALIMENTOS.url] = ({**headers, "ETag": '"new"'}, body)
    with fake_dados.client() as http:
        result = build(
            tmp_path / "second", http=http, skip_unchanged=FakeDados.MANIFEST_URL, commit="abc123"
        )
    assert set(result["tables"]) == {"alimentos", "alimentos_resultado"}
    assert result["tables"]["alimentos"]["source"]["etag"] == '"new"'
    # the unchanged file answered 304 first, then was fetched again for a complete build
    statuses = [r.headers.get("If-None-Match") for r in fake_dados.data_requests()]
    assert statuses.count(None) == 1 and len(statuses) == 3


@needs_duckdb
def test_build_rebuilds_when_commit_changed(fake_dados, tmp_path):
    published(fake_dados, tmp_path, commit="old")
    with fake_dados.client() as http:
        result = build(
            tmp_path / "second", http=http, skip_unchanged=FakeDados.MANIFEST_URL, commit="new"
        )
    assert isinstance(result, dict) and result["commit"] == "new"
    assert (tmp_path / "second" / "manifest.json").exists()


@needs_duckdb
def test_build_proceeds_without_published_manifest(fake_dados, tmp_path):
    with fake_dados.client() as http:
        result = build(tmp_path / "dist", http=http, skip_unchanged=FakeDados.MANIFEST_URL)
    assert isinstance(result, dict) and (tmp_path / "dist" / "manifest.json").exists()
    assert all("If-None-Match" not in r.headers for r in fake_dados.data_requests())


@needs_duckdb
def test_build_checks_every_header_before_converting(fake_dados, tmp_path):
    headers, body = fake_dados.files[ALIMENTOS_RESULTADO.url]
    fake_dados.files[ALIMENTOS_RESULTADO.url] = (headers, body.replace(b"VALIDADE", b"PRAZO", 1))
    with fake_dados.client() as http, pytest.raises(SchemaDriftError, match="PRAZO"):
        build(tmp_path / "dist", http=http)
    assert not (tmp_path / "dist").exists()  # alimentos, listed first, was not converted


# --- cli ----------------------------------------------------------------------

runner = CliRunner()


@needs_duckdb
def test_cli_list_and_build(fake_dados, monkeypatch, tmp_path):
    result = runner.invoke(cli.app, ["-f", "json", "dados", "list"])
    assert result.exit_code == 0, result.output
    assert [d["name"] for d in json.loads(result.stdout)] == ["alimentos", "alimentos_resultado"]

    monkeypatch.setattr(cli, "dados_http", fake_dados.client)
    out = tmp_path / "dist"
    args = ["dados", "build", "--out", str(out), "--dataset", "alimentos_resultado"]
    result = runner.invoke(cli.app, args, env={"GITHUB_SHA": "abc123"})
    assert result.exit_code == 0, result.output
    summary = json.loads(result.stdout)  # progress went to stderr, stdout is pure JSON
    assert list(summary["tables"]) == ["alimentos_resultado"]
    assert summary["tables"]["alimentos_resultado"]["rows"] == 20
    assert "convert TA_CONSULTA_ALIMENTOS_RESULTADO.CSV" in result.stderr
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["commit"] == "abc123"

    args = ["dados", "build", "-o", str(tmp_path / "again"), "-d", "alimentos_resultado"]
    args += ["--skip-unchanged", str(out / "manifest.json")]
    result = runner.invoke(cli.app, args, env={"GITHUB_SHA": "abc123"})
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "skipped": True,
        "reason": "unchanged",
        "published_build_id": manifest["build_id"],
    }

    result = runner.invoke(cli.app, ["dados", "build", "-o", str(out), "-d", "nope"])
    assert result.exit_code == 1 and "unknown dataset" in result.stderr


def test_missing_duckdb_is_a_clear_error(monkeypatch, tmp_path):
    from anvisa.dados.convert import convert

    monkeypatch.setitem(sys.modules, "duckdb", None)  # makes `import duckdb` raise ImportError
    csv = tmp_path / ALIMENTOS.file
    csv.write_bytes(sample(ALIMENTOS).read_bytes())
    with pytest.raises(DadosError, match=re.escape("pip install 'anvisa[dados]'")):
        convert(ALIMENTOS, csv, tmp_path / "x.parquet")
    result = runner.invoke(cli.app, ["-f", "json", "dados", "list"])
    assert result.exit_code == 0 and len(json.loads(result.stdout)) == len(CATALOG)


def test_dataset_is_a_frozen_value():
    with pytest.raises(dataclasses.FrozenInstanceError):
        ALIMENTOS.name = "x"


@pytest.mark.live
def test_live_dados_head():
    """No credentials: checks TLS to dados.anvisa.gov.br and the headers the build relies on."""
    with http_client(timeout=30) as http:
        response = http.head(ALIMENTOS.url)
    assert response.status_code == 200
    assert response.headers.get("accept-ranges") == "bytes"
    assert response.headers.get("etag") or response.headers.get("last-modified")
