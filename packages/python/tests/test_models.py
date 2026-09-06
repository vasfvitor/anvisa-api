from datetime import datetime, timezone

import pytest
from conftest import load

from anvisa import models

CASES = [
    ("areafila.json", models.TipoProduto, 11),
    ("arealista.json", models.TipoProduto, 4),
    ("nomeTecnico_categorias.json", models.TipoProduto, 2),
    ("fila_grupos.json", models.ChaveValorLong, 8),
    ("subfilas.json", models.ChaveValorInteger, 13),
    ("fila_consulta.json", models.FilaCalculadaDTO, 40),
    ("subfilas_281.json", models.ChaveValorInteger, 4),
    ("fila_consulta_161.json", models.FilaCalculadaDTO, 35),
    ("lista_grupos.json", models.ChaveValorLong, 6),
    ("lista_sublistas.json", models.ChaveValorInteger, 1),
    ("lista_consulta.json", models.FilaCalculadaDTO, 555),
    ("udi_filtro.json", models.PageUdiDTO, None),
    ("udi_filtro_udiDi.json", models.PageUdiDTO, None),
    ("udi_filtro_cnpj.json", models.PageUdiDTO, None),
    ("udi_filtro_gmdn.json", models.PageUdiDTO, None),
    ("udi_filtro_registro.json", models.PageUdiDTO, None),
    ("udi_historicos.json", models.HistoricoUdiDTO, 171),
    ("gmdn_busca.json", models.PageTermoGMDNDTO, None),
    ("nomeTecnico_filtro_nome.json", models.PageNomeTecnicoDTO, None),
    ("nomeTecnico_filtro_categoria.json", models.PageNomeTecnicoDTO, None),
    ("assunto_tiposSolicitacao.json", models.TipoSolicitacaoDTO, 2),
    ("assunto_tiposProduto.json", models.TipoProdutoDTO, 13),
    ("assunto_sistemas.json", models.SistemaDTO, 7),
    ("assunto_servicos.json", models.ServicoDTO, 378),
    ("nomeTecnico_p1.json", models.PageNomeTecnicoDTO, None),
    ("udi_detail_377.json", models.DetalheDispositivoDTO, None),
    ("gmdn_47852.json", models.TermoGMDNDTO, None),
    ("assuntos.json", models.AssuntoDTO, 2595),
    ("assunto_10013.json", models.DetalheAssunto, None),
    ("err_msg062.json", models.ErroApi, None),
    ("err_subfila.json", models.ErroApi, None),
    ("err_sublista.json", models.ErroApi, None),
    ("err_page_index.json", models.ErroApi, None),
    ("err_jackson.json", models.ErroApi, None),
    ("err_assunto_busca.json", models.ErroApi, None),
]


@pytest.mark.parametrize(("name", "model", "count"), CASES, ids=[c[0] for c in CASES])
def test_every_recorded_response_parses(name, model, count):
    data = load(name)
    if count is None:
        model.model_validate(data)
    else:
        assert len([model.model_validate(item) for item in data]) == count


def test_epoch_millis_become_aware_datetimes():
    page = models.PageUdiDTO.model_validate(load("udi_filtro.json"))
    midnight_brt = datetime(2026, 4, 8, 3, 0, tzinfo=timezone.utc)
    assert page.content[0].dtPublicacao == midnight_brt


def test_fila_row_types_fixed_by_overlay():
    row = models.FilaCalculadaDTO.model_validate(load("fila_consulta.json")[0])
    assert row.nuOrdem == 1
    assert isinstance(row.dtEntrada, datetime)
    assert row.numeroProcessoFormatado == "25351.216322/2025-86"


def test_detail_enums_hold():
    detail = models.DetalheDispositivoDTO.model_validate(load("udi_detail_377.json"))
    assert detail.dispositivo.isUsoUnico.value == "SIM"
    assert detail.dispositivo.termoGmdn.codigo == "47852"
