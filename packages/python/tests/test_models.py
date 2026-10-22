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
    # the four domains of 2026-10-01, recorded 2026-10-22
    ("saude_p1.json", models.PageConsultaProdutoSaude, None),
    ("saude_filtro_nome.json", models.PageConsultaProdutoSaude, None),
    ("saude_filtro_cnpj.json", models.PageConsultaProdutoSaude, None),
    ("saude_detail_25351279419201172.json", models.DetalheProdutoSaude, None),
    ("certificado_status.json", models.ChaveValorInteger, 4),
    ("certificado_certificacaoConcedidaPor.json", models.EmissaoCertificadoDTO, 10),
    ("certificado_p1.json", models.PageCertificadoResultado, None),
    ("certificado_filtro_status.json", models.PageCertificadoResultado, None),
    ("certificado_filtro_cnpj.json", models.PageCertificadoResultado, None),
    ("certificado_33521614.json", models.CertificadoDetalhe, None),
    ("certificadoMedicamento_status.json", models.ChaveValorInteger, 4),
    ("certificadoMedicamento_linhasCertificacao.json", models.LinhaCbpf, 16),
    ("certificadoMedicamento_formasFarmaceuticas.json", models.FormaFarmaceuticaBasica, 80),
    ("certificadoMedicamento_classesCertificacao.json", models.ClasseCertificacao, 9),
    ("certificadoMedicamento_certificacaoConcedidaPor.json", models.EmissaoCertificadoDTO, 10),
    ("certificadoMedicamento_p1.json", models.PageCertificadoResultado, None),
    ("certificadoMedicamento_filtro_cnpjSolicitante.json", models.PageCertificadoResultado, None),
    ("certificadoMedicamento_33318813.json", models.CertificadoDetalhe, None),
    ("tabaco_tipoTabaco.json", models.TipoProdutoTabaco, 14),
    ("tabaco_situacoes.json", models.ChaveValorObject, 2),
    ("tabaco_origemTabaco.json", models.ChaveValorObject, 3),
    ("tabaco_destinoTabaco.json", models.ChaveValorObject, 3),
    ("tabaco_categoriaAditivo.json", models.CategoriaAditivo, 11),
    ("tabaco_p1.json", models.PageConsultaTabacoDTO, None),
    ("tabaco_filtro_nome.json", models.PageConsultaTabacoDTO, None),
    ("tabaco_filtro_cnpj_ignorado.json", models.PageConsultaTabacoDTO, None),
    ("tabaco_detailTabaco_33007111.json", models.ConsultaTabacoDTO, None),
    ("err_msg013.json", models.ErroApi, None),
    ("err_msg013_medicamento.json", models.ErroApi, None),
    ("err_tabaco_detail.json", models.ErroApi, None),
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
