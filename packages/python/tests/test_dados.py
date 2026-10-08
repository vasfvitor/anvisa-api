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
from anvisa.dados import CATALOG, Unchanged, build, parse, select
from anvisa.dados.catalog import (
    ALIMENTOS,
    ALIMENTOS_RESULTADO,
    PETICOES_ALIMENTO,
    PETICOES_ALIMENTO_ANDAMENTO,
    PRODUTOS_IRREGULARES,
    SANEANTES,
    TYPES,
)
from anvisa.dados.fetch import download, http_client
from anvisa.dados.parse import (
    check_header,
    decode,
    normalize,
    read_header,
    records,
    stream,
    unescape,
)
from anvisa.errors import DadosError, SchemaDriftError

DADOS = Path(__file__).resolve().parents[3] / "fixtures" / "dados"
SAMPLES = {
    ALIMENTOS.name: "alimentos_head.csv",
    ALIMENTOS_RESULTADO.name: "alimentos_resultado_head.csv",
    SANEANTES.name: "saneantes_head.csv",
    PETICOES_ALIMENTO.name: "peticoes_alimento_head.csv",
    PETICOES_ALIMENTO_ANDAMENTO.name: "peticoes_alimento_andamento_head.csv",
    PRODUTOS_IRREGULARES.name: "produtos_irregulares_head.csv",
}
HEADERS = {
    ALIMENTOS.name: "headers_alimentos.txt",
    ALIMENTOS_RESULTADO.name: "headers_alimentos_resultado.txt",
    SANEANTES.name: "headers_saneantes.txt",
    PETICOES_ALIMENTO.name: "headers_peticoes_alimento.txt",
    PETICOES_ALIMENTO_ANDAMENTO.name: "headers_peticoes_alimento_andamento.txt",
    PRODUTOS_IRREGULARES.name: "headers_produtos_irregulares.txt",
}
NOW = datetime(2026, 10, 6, 21, 3, 12, tzinfo=timezone.utc)
needs_duckdb = pytest.mark.skipif(
    importlib.util.find_spec("duckdb") is None, reason="needs the anvisa[dados] extra"
)


def sample(ds) -> Path:
    return DADOS / SAMPLES[ds.name]


def sample_records(ds) -> list[list[str]]:
    return list(records(decode(sample(ds).read_bytes()), len(ds.columns)))


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
        assert ds.directory == "" or ds.directory.endswith("/")
        assert not ds.directory.startswith("/") and "//" not in ds.url.split("://", 1)[1]
    # ANVISA reuses file names across folders, so the work directory is keyed by dataset
    assert len({d.local_file for d in CATALOG}) == len(CATALOG)
    with pytest.raises(ValueError, match="must be '' or end in '/'"):
        dataclasses.replace(ALIMENTOS, directory="CONSULTAS/PRODUTOS")
    assert select(["alimentos"]) == (ALIMENTOS, ALIMENTOS_RESULTADO)  # the group
    assert select(["alimentos_resultado"]) == (ALIMENTOS_RESULTADO,)
    assert select(["saneantes", "alimentos_resultado"]) == (ALIMENTOS_RESULTADO, SANEANTES)
    assert select(["peticoes_alimento"]) == (PETICOES_ALIMENTO, PETICOES_ALIMENTO_ANDAMENTO)
    assert PETICOES_ALIMENTO.url == (
        "https://dados.anvisa.gov.br/dados/CICLO_ANALISE_PETICOES_ALIMENTO.CSV"
    )
    with pytest.raises(DadosError, match="unknown dataset"):
        select(["cosmeticos"])


def test_fixture_header_matches_catalog():
    for ds in CATALOG:
        assert read_header(sample(ds)) == list(ds.columns)
        check_header(ds, read_header(sample(ds)))
    # the `#` comment marker on the finalized-petitions header is dropped, not a schema change
    assert sample(PETICOES_ALIMENTO).read_bytes().startswith(b"#NUM_EXPEDIENTE_PETICAO;")


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
    # saneantes: an inner `"` followed by CRLF CRLF, which only the record width disambiguates
    meninas = [r[0] for r in sample_records(SANEANTES) if "AS MENINAS" in r[0]]
    assert any(n.endswith('"AS MENINAS"\r\n\r\nSPRAY') for n in meninas), meninas


def test_records_tells_inner_quotes_from_truncation():
    """A `"` + line break inside a non-last field is read as part of the value only when the
    width-blind reading of the same span cannot parse; a truncated record stays a short one."""
    normal = '"X";"1";"2"\n'
    inner = '"A "B"\n\nC";"1";"2"\n'
    assert list(records(inner + normal, 3)) == [['A "B"\n\nC', "1", "2"], ["X", "1", "2"]]
    truncated = '"A";"1"\n'
    assert list(records(truncated + normal, 3)) == [["A", "1"], ["X", "1", "2"]]
    # the same truncated record, but the next one begins with a bare field: still two records
    assert list(records(truncated + '7;"1";"2"\n', 3)) == [["A", "1"], ["7", "1", "2"]]


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
    assert normalize(ALIMENTOS, src, tmp_path / "out5.csv", chunk_size=5) == result


def chunked(text, n: int):
    return (text[i : i + n] for i in range(0, len(text), n))


def test_stream_matches_records_at_every_chunk_size():
    """Chunk boundaries cannot change a reading: the end-of-text rule only applies to the last
    chunk, and a field cut short by the buffer is parsed again with the next one."""
    for ds in CATALOG:
        raw = sample(ds).read_bytes()
        width = len(ds.columns)
        text = decode(raw)
        for n in (1, 2, 3, 7, 64, 4096, len(text) + 1):
            assert list(stream(chunked(text, n), width)) == sample_records(ds), (ds.name, n)
        # the way `normalize` feeds it: bytes cut anywhere, decoded piece by piece
        assert list(stream(map(decode, chunked(raw, 7)), width)) == sample_records(ds)
        # no trailing line break: `\Z` closes the last record
        bare = text.rstrip("\r\n")
        assert list(stream(chunked(bare, 5), width)) == list(records(bare, width))


def test_stream_boundaries_do_not_change_a_reading():
    """Every possible cut of the texts that exercise the dialect's corner cases."""
    normal = '"X";"1";"2"\n'
    texts = [
        '"a";"b";"c"\r\n"d";"e";"f"\r\n',  # cut between `"` and `\r`, between `\r` and `\n`
        '"A "B"\n\nC";"1";"2"\n' + normal,  # inside the inner `"..."\n` (the merged path)
        '"A";"1"\n' + normal,  # a truncated record, re-split the width-blind way
        '"A";"1"\n7;"1";"2"\n',  # the same, but the next record begins with a bare field
        'x;y;z\r\n"q";"r\r\n";"s"\r\n',  # bare fields; CRLF inside a quoted non-last field
        "x;y;z",  # bare, no line break at all
        '"a";"b";',  # a trailing partial record (dropped, as `records` drops it)
    ]
    for text in texts:
        expected = list(records(text, 3))
        for n in range(1, len(text) + 2):
            assert list(stream(chunked(text, n), 3)) == expected, (text, n)


def test_stream_errors_match_whole_text(tmp_path):
    """A malformed file fails at the same character with the same context, however it is cut."""
    normal = '"X";"1";"2"\n'
    texts = [
        normal * 5 + 'ab"c;"1";"2"\n',  # a quote in a bare field, beyond 40 characters in
        "x;y;z\rq;r;s\n",  # a lone `\r`
        '"a";"b"\n',  # a short last record is fine...
        '"a";"b";"c"\r',  # ...but a `"` + `\r` + end of file is not
        '"a";"b";"c\n',  # an unclosed quote
    ]
    for text in texts:
        try:
            expected = list(records(text, 3))
        except DadosError as exc:
            expected = str(exc)
        for n in (1, 3, 7, 1000):
            try:
                got = list(stream(chunked(text, n), 3))
            except DadosError as exc:
                got = str(exc)
            assert got == expected, (text, n)
    rows = sample_records(ALIMENTOS)
    src = write_dialect(tmp_path / "in.csv", rows)
    # a quote inside a quoted value is fine; a bare field with one is not
    quoted = b'"' + rows[5][0].encode("cp1252") + b'"'
    broken = src.read_bytes().replace(quoted, b'ab"c', 1)
    src.write_bytes(broken)
    with pytest.raises(DadosError, match="unparseable CSV at character") as whole:
        normalize(ALIMENTOS, src, tmp_path / "out.csv")
    with pytest.raises(DadosError) as streamed:
        normalize(ALIMENTOS, src, tmp_path / "out7.csv", chunk_size=7)
    assert str(streamed.value) == str(whole.value)


def test_stream_caps_a_record_that_never_ends(monkeypatch):
    monkeypatch.setattr(parse, "MAX_RECORD", 100)
    it = stream(chunked('"never closes;' + "x" * 500, 50), 3)
    with pytest.raises(DadosError, match="no record ends within the next 100 characters"):
        list(it)


def test_normalize_output_is_chunk_size_independent(tmp_path):
    whole = normalize(ALIMENTOS, sample(ALIMENTOS), tmp_path / "whole.csv")
    small = normalize(ALIMENTOS, sample(ALIMENTOS), tmp_path / "small.csv", chunk_size=7)
    assert whole == small
    assert (tmp_path / "whole.csv").read_bytes() == (tmp_path / "small.csv").read_bytes()


# --- download -----------------------------------------------------------------


def test_download_streams_via_part(fake_dados, tmp_path):
    with fake_dados.client() as http:
        source = download(http, ALIMENTOS, tmp_path)
    body = sample(ALIMENTOS).read_bytes()
    assert source.etag == '"20230eb-65d1981268f1f"'
    assert source.last_modified == "Mon, 05 Oct 2026 15:26:22 GMT"
    assert source.bytes == len(body) and source.sha256 == hashlib.sha256(body).hexdigest()
    assert (tmp_path / ALIMENTOS.local_file).read_bytes() == body
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
    assert [p.name for p in tmp_path.iterdir()] == [ALIMENTOS.local_file]


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
def test_convert_saneantes_values(tmp_path):
    """Month-first dates, 0/1 booleans, the load time from DT_ATUALIZACAO, and the inner quote
    followed by CRLF CRLF kept inside the product name."""
    import duckdb

    from anvisa.dados.convert import convert

    csv = tmp_path / SANEANTES.file
    csv.write_bytes(sample(SANEANTES).read_bytes())
    stats = convert(SANEANTES, csv, tmp_path / "s.parquet")
    assert stats.rows == 23 and stats.rejected == ()
    assert stats.loaded_at == "2026-10-05T00:00:00"
    assert all(v == 0 for v in stats.nulls_added.values())
    con = duckdb.connect()
    first = con.execute(
        "SELECT dt_vencimento_produto, is_registrado, st_produto_ativo, nu_cnpj_empresa "
        f"FROM '{tmp_path}/s.parquet' WHERE nu_processo = '25351650678202111'"
    ).fetchone()
    assert first == (datetime(2031, 6, 21, 10, 1, 26), False, True, "08409808000139")
    kinds = con.execute(
        f"SELECT is_registrado, count(*) FROM '{tmp_path}/s.parquet' GROUP BY 1 ORDER BY 1"
    ).fetchall()
    assert [k for k, _ in kinds] == [False, True]
    names = con.execute(
        f"SELECT no_produto FROM '{tmp_path}/s.parquet' WHERE no_produto LIKE '%AS MENINAS%'"
        " ORDER BY 1"
    ).fetchall()
    assert ('AROMATIZADOR DE AMBIENTE "AS MENINAS"\r\n\r\nSPRAY',) in names  # whole, CRLFs kept
    assert ('AROMATIZADOR DE AMBIENTE "AS MENINAS"',) in names  # its sibling, a separate record
    aquaflex = con.execute(
        f"SELECT no_produto FROM '{tmp_path}/s.parquet' WHERE no_produto LIKE 'AQUAFLEX%'"
    ).fetchone()[0]
    assert aquaflex.endswith("‏")  # &#8207; decoded, and that is what ANVISA wrote


@needs_duckdb
def test_convert_petitions(tmp_path):
    """Month-first dates in the finalized file and day-first in the open one, the `#` header,
    the open file's `Todos` rows, a processo's history contiguous, and the join to alimentos."""
    import duckdb

    from anvisa.dados.convert import convert

    tables = {}
    for ds in (PETICOES_ALIMENTO, PETICOES_ALIMENTO_ANDAMENTO, ALIMENTOS):
        csv = tmp_path / ds.file
        csv.write_bytes(sample(ds).read_bytes())
        stats = convert(ds, csv, tmp_path / f"{ds.name}.parquet")
        tables[ds.name] = f"'{tmp_path / ds.name}.parquet'"
        if ds is not ALIMENTOS:
            assert stats.rejected == () and stats.loaded_at is None
            assert all(v == 0 for v in stats.nulls_added.values())
    con = duckdb.connect()

    def rows(sql):
        return con.execute(sql.format(**tables)).fetchall()

    done = "{peticoes_alimento}"
    first = rows(
        "SELECT num_processo_peticao, s_n_peticao_primaria, cod_assunto_peticao, "
        "data_situacao_atual_peticao, data_ini_ocorrencia_grp_etapa, "
        "ordem_ocorre_grupo_etapa_asc, ordem_ocorre_grupo_etapa_desc "
        f"FROM {done} WHERE num_expediente_peticao = '0875314158' ORDER BY 6"
    )
    assert first[0] == (
        "25351734343201491",
        False,
        457,
        datetime(2016, 3, 21),
        datetime(2015, 9, 29),
        1,
        3,
    )
    assert [r[5:] for r in first] == [(1, 3), (2, 2), (3, 1)]
    closed = rows(
        f"SELECT DISTINCT data_primeira_finalizacao FROM {done} "
        "WHERE num_expediente_peticao = '524004112'"
    )
    assert closed == [(datetime(2012, 1, 25, 16, 37, 27),)]  # `01/25/2012 16:37:27`
    assert rows(f"SELECT count(*) FROM {done} WHERE desc_sub_fila_lista_analise LIKE '% '") == [
        (0,)
    ]  # the trailing spaces are stripped
    assunto = rows(
        f"SELECT DISTINCT desc_assunto_peticao FROM {done} WHERE cod_assunto_peticao = 4092"
    )
    assert assunto[0][0].endswith("primeira infância&#8203,")  # kept as ANVISA wrote it
    processos = rows(f"SELECT num_processo_peticao FROM {done}")
    assert processos == sorted(processos)  # a processo's petitions are contiguous

    open_ = "{peticoes_alimento_andamento}"
    queue = rows(
        "SELECT data_ini_ocorrencia_grp_etapa, data_fim_ocorrencia_grp_etapa "
        f"FROM {open_} WHERE num_expediente_peticao = '0823936261' "
        "AND desc_grupo_etapa_ciclo_analise = 'Fila de Análise'"
    )
    # `12/08/2026 18:03:00` is 12 August, day first; month first would read 8 December
    assert queue == [(datetime(2026, 8, 12, 18, 3), datetime(2026, 9, 23, 14, 7, 8))]
    todos = rows(
        "SELECT desc_grupo_etapa_ciclo_analise, count(*), count(data_fim_ocorrencia_grp_etapa), "
        f"count(DISTINCT num_expediente_peticao) FROM {open_} "
        "WHERE ordem_ocorre_grupo_etapa_asc = 0 GROUP BY 1"
    )
    assert todos == [("Todos", 5, 0, 5)]  # one per petição, never closed
    assert "data_primeira_finalizacao" not in [c for c, *_ in rows(f"DESCRIBE {open_}")]

    joined = rows(
        f"SELECT DISTINCT p.num_processo_peticao FROM {done} p "
        "JOIN {alimentos} a ON a.nu_processo = p.num_processo_peticao"
    )
    assert ("25351053143202611",) in joined  # a petition's company comes from alimentos


@needs_duckdb
def test_convert_produtos_irregulares(tmp_path):
    """The company acted against is NU_CNPJ_EMPRESA_INVESTIGADA, not NU_CNPJ (who filed the
    dossiê, often ANVISA); entities and inner quotes in product names; area-first sort."""
    import duckdb

    from anvisa.dados.convert import convert

    tables = {}
    for ds in (PRODUTOS_IRREGULARES, ALIMENTOS):
        csv = tmp_path / ds.file
        csv.write_bytes(sample(ds).read_bytes())
        stats = convert(ds, csv, tmp_path / f"{ds.name}.parquet")
        tables[ds.name] = f"'{tmp_path / ds.name}.parquet'"
        if ds is PRODUTOS_IRREGULARES:
            assert stats.rows == 54 and stats.rejected == ()
            assert stats.loaded_at == "2026-10-05T00:00:02"
            assert all(v == 0 for v in stats.nulls_added.values())
    con = duckdb.connect()

    def rows(sql):
        return con.execute(sql.format(**tables)).fetchall()

    t = "{produtos_irregulares}"
    areas = [a for (a,) in rows(f"SELECT co_tipo_produto FROM {t}")]
    assert areas == sorted(areas)  # one area's rows are contiguous
    # ANVISA (03112386000111) filed this one; the company is in the investigada column
    filed = rows(
        "SELECT DISTINCT nu_cnpj, nu_cnpj_empresa_investigada, ds_tipo_produto "
        f"FROM {t} WHERE co_seq_dossie_investig_med = 45947"
    )
    assert filed[0][0] == "03112386000111" and filed[0][1] != filed[0][0]
    assert filed[0][2] == "Alimento"
    # what the investigada column holds besides CNPJs, kept as written
    investigated = {v for (v,) in rows(f"SELECT nu_cnpj_empresa_investigada FROM {t}")}
    assert {"DESCONHECIDO", "desconhecido", "03855103143", None} <= investigated
    joined = rows(
        f"SELECT DISTINCT i.co_seq_dossie_investig_med FROM {t} i "
        "JOIN {alimentos} a ON a.nu_cnpj_empresa = i.nu_cnpj_empresa_investigada"
    )
    assert (53236,) in joined  # a company with regularized products and a measure
    # per dossiê: the latest publication, and how many measures it took
    measures = rows(
        "SELECT total_medida_cautelar, dt_publicacao_medida, count(DISTINCT dt_publicacao), "
        f"max(dt_publicacao) FROM {t} WHERE co_seq_dossie_investig_med = 33727 GROUP BY ALL"
    )
    assert measures == [(2, datetime(2022, 8, 25), 2, datetime(2022, 8, 25))]
    names = [n for (n,) in rows(f"SELECT DISTINCT produto FROM {t} WHERE produto IS NOT NULL")]
    assert 'GENGIBRE VÉDICO EM PÓ" - MARCA SOULY' in names  # unescaped quote, kept
    assert any("BABY & KIDS" in n for n in names)  # &amp; decoded
    assert not any(re.search(r"&(#\d+|[a-z]+);", n) for n in names)
    company = rows(
        f"SELECT no_empresa_investigada FROM {t} WHERE co_seq_dossie_investig_med = 44903"
    )
    assert company[0][0] == "EBAZAR.COM.BR. LTDA\u200b (MERCADO LIVRE)"  # &#8203; decoded
    assert rows(f"SELECT count(registro) FROM {t} WHERE co_tipo_produto = 6") == [(0,)]
    # `HARVONI` and `HARVONI `: two records upstream, one value once stripped
    harvoni = rows(f"SELECT count(*), count(DISTINCT produto) FROM {t} WHERE produto = 'HARVONI'")
    assert harvoni == [(2, 1)]


@needs_duckdb
def test_petition_date_order_is_not_interchangeable(tmp_path):
    """Each file gets exactly one date order: with the other one, too many values stop parsing
    and the build fails, instead of 12/08 quietly turning into 8 December."""
    from anvisa.dados.convert import convert

    pairs = (
        (PETICOES_ALIMENTO, PETICOES_ALIMENTO_ANDAMENTO),
        (PETICOES_ALIMENTO_ANDAMENTO, PETICOES_ALIMENTO),
    )
    for ds, other in pairs:
        wrong = dataclasses.replace(ds, timestamp_formats=other.timestamp_formats)
        csv = tmp_path / ds.file
        csv.write_bytes(sample(ds).read_bytes())
        with pytest.raises(DadosError, match="did ANVISA change the format"):
            convert(wrong, csv, tmp_path / "x.parquet")


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
    assert list(manifest["tables"]) == [ds.name for ds in CATALOG]
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
    petitions = manifest["tables"]["peticoes_alimento"]
    assert petitions["source"]["loaded_at"] is None  # the petition files carry no load time
    assert petitions["source"]["url"] == PETICOES_ALIMENTO.url


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
    assert list(result["tables"]) == [ds.name for ds in CATALOG]
    assert result["tables"]["alimentos"]["source"]["etag"] == '"new"'
    # the unchanged files answered 304 first, then were fetched again for a complete build
    statuses = [r.headers.get("If-None-Match") for r in fake_dados.data_requests()]
    unchanged = len(CATALOG) - 1
    assert statuses.count(None) == unchanged and len(statuses) == len(CATALOG) + unchanged


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
    names = [d["name"] for d in json.loads(result.stdout)]
    assert names == [ds.name for ds in CATALOG]

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
