"""What each published column means, in Portuguese, for the manifest and the Pages dictionary.

Keyed by table name, then by the **source header** (the key of `Dataset.columns`) for the
catalog tables and by the output column name for the derived ones. A sibling of catalog.py
rather than a third element of `columns`, which five readers consume positionally as
header -> type. `test_descriptions_cover_every_column` keeps the two in step.

Write for someone reading the Parquet, not for the parser: what the value is, its shape (digits,
separators, placeholders), and where it joins. Profiling counts stay in catalog.py's comments.
"""

from __future__ import annotations

_BRASILIA = " (horário de Brasília)"
_PROCESSO = (
    "Nº do processo na ANVISA, só dígitos. Os atuais têm 17 (exibidos como "
    "25351.161029/2026-55); os antigos, de 6 a 16, a maioria 13. Compare como texto."
)
_CNPJ = "CNPJ da empresa detentora, 14 dígitos com zeros à esquerda. Texto, nunca número."
_RAZAO = "Razão social da empresa detentora."
_NOME_PRODUTO = "Nome do produto como regularizado."
_TIPO = (
    '"Registrado" (a ANVISA analisou antes de liberar) ou "Notificado" (a empresa '
    "comunicou e pode vender sem análise prévia)."
)
_CARGA_ETL = "Quando a ANVISA gerou o arquivo aberto" + _BRASILIA + "; igual em todas as linhas."
_ATUALIZACAO = (
    "Quando a ANVISA atualizou o arquivo aberto" + _BRASILIA + "; igual em todas as linhas."
)

_PETICAO = {
    "NUM_EXPEDIENTE_PETICAO": (
        "Nº do expediente da petição, 9 ou 10 dígitos com zeros à esquerda; identifica a petição."
    ),
    "NUM_PROCESSO_PETICAO": (
        "Nº do processo a que a petição pertence, só dígitos (17 nos atuais, 13 nos antigos). "
        "Quando é o processo de um produto, junta com alimentos.nu_processo."
    ),
    "S_N_PETICAO_PRIMARIA": "Verdadeiro na petição que abriu o processo.",
    "COD_ASSUNTO_PETICAO": "Código do assunto da petição; um para um com desc_assunto_peticao.",
    "DESC_ASSUNTO_PETICAO": "Assunto da petição (o tipo de pedido feito à ANVISA).",
    "DATA_SITUACAO_ATUAL_PETICAO": "Data da situação atual da petição" + _BRASILIA + ".",
    "DESC_SITUACAO_ATUAL_PETICAO": 'Situação atual da petição (ex.: "Publicado deferimento").',
    "DESC_TIPO_DOCUMENTO": (
        'Tipo do documento: "Petição" num processo existente, "Processo" quando é um pedido '
        "novo, de produto ainda não registrado."
    ),
    "DESC_AREA_INTERESSE": 'Área da ANVISA; sempre "Alimento" neste arquivo.',
    "DESC_FILA_ANALISE": "Fila de análise em que a petição está ou esteve.",
    "DESC_SUB_FILA_LISTA_ANALISE": "Subfila ou lista de análise dentro da fila.",
    "DESC_GRUPO_ETAPA_CICLO_ANALISE": (
        "A etapa do ciclo de análise que esta linha descreve (fila, análise, exigência, "
        "finalização…)."
    ),
    "DATA_INI_OCORRENCIA_GRP_ETAPA": "Início da etapa" + _BRASILIA + ".",
    "DATA_FIM_OCORRENCIA_GRP_ETAPA": "Fim da etapa" + _BRASILIA + "; vazio na etapa em curso.",
    "ORDEM_OCORRE_GRUPO_ETAPA_ASC": (
        "Ordem da etapa dentro da petição, a partir de 1; com o expediente, identifica a linha."
    ),
    "ORDEM_OCORRE_GRUPO_ETAPA_DESC": "Ordem inversa da etapa: 1 é a mais recente.",
}

_PRODUTOS_COLS = {
    "area": '"alimentos" ou "saneantes": a tabela de origem.',
    "id": (
        "Identificador na origem: co_seq_produto nos alimentos, nu_expediente nos saneantes. "
        "Único com area."
    ),
    "nome": _NOME_PRODUTO,
    "marcas": 'Marcas do alimento, separadas por " ; "; nulo nos saneantes.',
    "empresa": _RAZAO,
    "cnpj": _CNPJ,
    "processo": _PROCESSO,
    "registro": (
        "Nº de registro do produto (9 dígitos); nas notificações de alimentos, o nº do "
        "processo; vazio nas de saneantes."
    ),
    "ativo": "Regularização ativa na ANVISA no dia do build.",
    "tipo": _TIPO,
    "categoria": "Categoria do alimento (ex.: Suplementos alimentares); nulo nos saneantes.",
    "desde": "Primeira regularização do alimento (data); nulo nos saneantes.",
    "vencimento": (
        "Vencimento da regularização (data): nos alimentos, o mais tardio entre as "
        "apresentações, com precisão de mês, e 2029-12-01 em toda notificação; nos saneantes "
        "pode faltar ou ir até o ano 3033."
    ),
    "apresentacoes": "Quantas apresentações o alimento tem em alimentos; 1 nos saneantes.",
    "registros": (
        "Os nº de registro das apresentações do alimento (13 dígitos cada), em ordem; lista "
        "vazia nas notificações e nos saneantes."
    ),
    "gluten": (
        '"Sim", "Não" ou "Varia" (apresentações divergem), pelo rótulo; nulo quando o '
        "alimento não declara e nos saneantes."
    ),
    "lactose": '"Sim", "Não" ou "Varia", pelo rótulo; nulo quando não declara e nos saneantes.',
    "contem": (
        'Alergênicos que o rótulo declara conter ("Contém" ou "Contém derivado de"), nomes '
        "legais em ordem, juntando todas as apresentações; nulo quando não declara."
    ),
    "pode_conter": (
        'Alergênicos que o rótulo diz "Pode conter" e nenhuma apresentação declara conter; '
        "nulo quando não declara."
    ),
    "nao_contem": (
        'Alergênicos que o rótulo diz "Não contém" e nenhuma apresentação contém ou pode '
        "conter; nulo quando não declara."
    ),
    "alergia_varia": (
        "Verdadeiro quando as apresentações do alimento declaram alergênicos ou "
        "intolerâncias diferentes entre si; nulo quando não declara."
    ),
}

_PRECO = "em centavos de real, por alíquota de ICMS do estado de venda; nulo onde a lista traz '-'."


def _cmed_precos() -> dict[str, str]:
    """pf_*, pmc_* and pmvg_*: one sentence each, generated from the rate in the name."""
    blocos = {
        "pf": (
            "Preço fábrica (PF), o máximo do fabricante ou distribuidor para farmácias e "
            "entes públicos,"
        ),
        "pmc": "Preço máximo ao consumidor (PMC), o teto da farmácia,",
        "pmvg": "Preço máximo de venda ao governo (PMVG), o teto em compras públicas,",
    }
    out = {}
    for bloco, texto in blocos.items():
        out[f"{bloco}_sem_impostos"] = f"{texto} sem impostos; nulo onde a lista traz '-'."
        for rate in (
            "0",
            "12",
            "17",
            "17_5",
            "18",
            "19",
            "19_5",
            "20",
            "20_5",
            "21",
            "22",
            "22_5",
            "23",
        ):
            pct = rate.replace("_", ",") + " %"
            if rate == "0":
                out[f"{bloco}_0"] = f"{texto} com ICMS 0 % (isentos) " + _PRECO
                continue
            out[f"{bloco}_{rate}"] = f"{texto} com ICMS de {pct} " + _PRECO
            out[f"{bloco}_{rate}_alc"] = (
                f"{texto} com ICMS de {pct} em Área de Livre Comércio (ALC) " + _PRECO
            )
    return out


_CMED: dict[str, str] = {
    "ggrem": (
        "Código GGREM da apresentação, 15 dígitos: a chave da lista da CMED, estável entre meses."
    ),
    "registro": (
        "Nº de registro da apresentação na ANVISA, 13 dígitos; texto. Nulo numa linha ('-')."
    ),
    "registro_produto": (
        "Os 9 primeiros dígitos do registro, o produto: o que produtos_irregulares e o "
        "cadastro de medicamentos da ANVISA usam."
    ),
    "substancia": 'Princípio(s) ativo(s), em maiúsculas; vários separados por ";".',
    "cnpj": "CNPJ do laboratório detentor, 14 dígitos; texto.",
    "laboratorio": "Razão social do laboratório detentor.",
    "produto": "Nome comercial do medicamento.",
    "apresentacao": "Apresentação (concentração, forma, embalagem), como a CMED escreve.",
    "classe_terapeutica": "Classe terapêutica (código e nome), ex. 'D7B2 - CORTICOESTERÓIDES…'.",
    "tipo_produto": (
        'Status do produto: "Genérico", "Similar", "Novo", "Específico", "Biológico", '
        '"Fitoterápico", "Produto de Terapia Avançada", "Radiofármaco"; nulo em 50 linhas.'
    ),
    "regime_preco": (
        '"Regulado" ou "Liberado" (só o preço fábrica é liberado; o PMC segue a margem).'
    ),
    "ean_1": "Código EAN (13 dígitos) principal; texto. Pode repetir entre apresentações.",
    "ean_2": "Segundo EAN, quando há; nulo na maioria.",
    "ean_3": "Terceiro EAN, quando há; nulo em quase todas.",
    **_cmed_precos(),
    "pmc_asterisco": (
        "O arquivo PMC marca com * os preços desta apresentação acima de ICMS 0 % (os "
        "isentos de ICMS): alíquotas exibidas, mas não aplicáveis. Os valores vêm sem o *."
    ),
    "pmvg_asterisco": (
        "O arquivo PMVG marca com * os preços desta apresentação acima de ICMS 0 % (isentos e "
        "produtos com CAP): alíquotas exibidas, mas não aplicáveis. Os valores vêm sem o *."
    ),
    "restricao_hospitalar": (
        "Uso restrito a hospitais: nessas apresentações não há PMC (pmc_* nulo)."
    ),
    "cap": (
        "Sujeito ao Coeficiente de Adequação de Preços (CAP) nas vendas ao governo: o PMVG "
        "é o PF com o desconto do CAP."
    ),
    "confaz_87": "Isenção de ICMS pelo Convênio CONFAZ 87/02 (compras públicas).",
    "icms_0": "Princípio ativo isento de ICMS por convênio do CONFAZ ou lei estadual.",
    "analise_recursal": (
        'Marca de preço em análise recursal: "(AR)" ou o nº da nota; nulo na maioria.'
    ),
    "lista_pis_cofins": (
        'Lista de concessão de crédito tributário PIS/COFINS: "Positiva", "Negativa" ou "Neutra".'
    ),
    "comercializacao_periodo": "O ano a que comercializacao se refere (o do cabeçalho da lista).",
    "comercializacao": "A apresentação foi comercializada no ano em comercializacao_periodo.",
    "tarja": (
        '"Tarja Vermelha", "Tarja Vermelha sob restrição", "Tarja Preta", "Tarja Sem Tarja" ou '
        '"- (*)" (sem informação).'
    ),
    "publicado_em": (
        "Data dos arquivos da CMED (Last-Modified em dados.anvisa.gov.br)" + _BRASILIA + "; a "
        "mesma em todas as linhas de um build."
    ),
}

DESCRICOES: dict[str, dict[str, str]] = {
    "alimentos": {
        "NO_PRODUTO": (
            'Nome do produto como regularizado; muitas vezes genérico ("Suplemento Alimentar em '
            'Cápsulas").'
        ),
        "NU_REGISTRO_NOTIFICACAO_PRODUTO": (
            "Nº de registro do produto (9 dígitos) ou, nas notificações, o nº do processo."
        ),
        "NU_PROCESSO": _PROCESSO,
        "NO_RAZAO_SOCIAL_EMPRESA": _RAZAO,
        "NU_CNPJ_EMPRESA": _CNPJ,
        "DS_SITUACAO_ASSUNTO_DOC": 'Situação da petição que regularizou o produto (ex.: "Anuído").',
        "DT_VENCIMENTO_REGISTRO": (
            "Vencimento do registro com precisão de mês, gravado como dia 1. Notificações trazem "
            "12/2029, um valor de preenchimento; registros vencem 5, 10, 15 ou 20 anos após a "
            "regularização."
        ),
        "CO_SEQ_PRODUTO": (
            "Identificador do produto; as apresentações de um mesmo produto compartilham nome, "
            "marcas, empresa, processo e situação. Junta com alimentos_resultado.co_produto."
        ),
        "DT_REGULARIZACAO": "Data da regularização" + _BRASILIA + ".",
        "CO_SEQ_APRESENTACAO_PRODUTO": (
            "Identificador da apresentação; chave de junção com alimentos_resultado."
        ),
        "CO_TIPO_REGULARIZACAO": "Código de tipo_regularizacao.",
        "CO_SITUACAO_ASSUNTO_DOC": "Código de ds_situacao_assunto_doc.",
        "DT_PUBLICACAO": "Data de publicação da regularização" + _BRASILIA + ".",
        "DT_SITUACAO": "Data da situação atual" + _BRASILIA + ".",
        "DT_INICIO_ANALISE": "Início da análise pela ANVISA" + _BRASILIA + ".",
        "NU_REGISTRO_PRODUTO": (
            "Nº de registro do produto (9 dígitos); vazio nas notificações. Quando presente, "
            "igual a nu_registro_notificacao_produto."
        ),
        "ST_PRODUTO_ATIVO": (
            "Produto ativo. Nulo quando a origem traz um valor que não é S nem N (uma linha)."
        ),
        "NU_REGISTRO": (
            "Nº de registro da apresentação: o do produto mais 4 dígitos (13 ao todo); vazio nas "
            "notificações."
        ),
        "CO_FORMA_FISICA": (
            "Código da forma física; o texto está em alimentos_resultado.ds_forma_fisica."
        ),
        "MARCAS": 'Marcas do produto, separadas por " ; ".',
        "SITUACAO_REGISTRO": '"Ativo" ou "Inativo" (cancelado, vencido ou encerrado a pedido).',
        "TIPO_REGULARIZACAO": _TIPO,
        "DS_CATEGORIA_PRODUTO": 'Categoria do produto (ex.: "Suplementos alimentares").',
        "DS_ALEGACAO_FUNCIONAL": 'Alegações funcionais autorizadas, separadas por ";".',
        "DT_CARGA_ETL": _CARGA_ETL,
    },
    "alimentos_resultado": {
        "CO_SEQ_APRESENTACAO_PRODUTO": (
            "Identificador da apresentação; chave de junção com alimentos."
        ),
        "NU_APRESENTACAO_PRODUTO": "Número da apresentação dentro do produto.",
        "NU_REGISTRO": "Nº de registro da apresentação (13 dígitos); vazio nas notificações.",
        "VALIDADE": 'Prazo de validade em texto (ex.: "24 Meses").',
        "DS_FORMA_FISICA": "Forma física (pó, cápsula, líquido…).",
        "SITUACAO_APRESENTACAO": "Situação da apresentação.",
        "MATERIAL_EMBALAGENS": 'Materiais de embalagem, separados por " | ".',
        "TIPO_EMBALAGENS": 'Tipos de embalagem, separados por " | ".',
        "EMPRESAS_ENVASADORAS": 'Empresas envasadoras, separadas por " | ".',
        "EMPRESAS_INTERNACIONAIS": 'Fabricantes internacionais, separados por " | ".',
        "GRUPOS_POPULACIONAIS": "Grupos populacionais a que o produto se destina.",
        "VIAS_ADMINISTRACAO": 'Vias de administração, separadas por " | ".',
        "TABELA_NUTRICIONAL": "Lista de ingredientes em texto livre, apesar do nome.",
        "INTOLERANCIAS": (
            'Pares "Contém Glúten - Sim/Não" e "Contém Lactose - Sim/Não", separados por " | ".'
        ),
        "ALERGENICOS": (
            'Grupos separados por " | ", cada um "Rótulo - item#item" (rótulos: Contém, Contém '
            "derivado de, Pode conter, Não contém)."
        ),
        "CO_PRODUTO": "Identificador do produto; igual a alimentos.co_seq_produto.",
        "DT_CARGA_ETL": _CARGA_ETL,
    },
    "saneantes": {
        "NO_PRODUTO": _NOME_PRODUTO,
        "NU_PROCESSO": _PROCESSO,
        "NU_CNPJ_EMPRESA": _CNPJ,
        "NO_RAZAO_SOCIAL_EMPRESA": _RAZAO,
        "ST_PRODUTO_ATIVO": "Produto ativo (regularização vigente).",
        "NU_REGISTRO_PRODUTO": "Nº de registro (9 dígitos); vazio nas notificações.",
        "DT_VENCIMENTO_PRODUTO": (
            "Vencimento da regularização" + _BRASILIA + "; vazio em parte das linhas, e a ANVISA "
            "publica datas até o ano 3033."
        ),
        "IS_REGISTRADO": "Verdadeiro nos registrados, falso nos notificados.",
        "NU_EXPEDIENTE": (
            "Nº do expediente, 9 ou 10 dígitos com zeros à esquerda; identifica o produto, mas "
            "seis vêm em duas linhas (Registrado e Notificado)."
        ),
        "DT_ATUALIZACAO": _ATUALIZACAO,
    },
    "peticoes_alimento": {
        **_PETICAO,
        "DATA_PRIMEIRA_FINALIZACAO": "Primeira finalização da petição" + _BRASILIA + ".",
        "DATA_FINALIZACAO_ATUAL": (
            "Finalização mais recente" + _BRASILIA + "; difere da primeira quando a petição voltou "
            "a ser analisada."
        ),
    },
    "peticoes_alimento_andamento": _PETICAO,
    "produtos_irregulares": {
        "CO_SEQ_DOSSIE_INVESTIG_MED": (
            "Identificador do dossiê de investigação; um dossiê tem várias linhas (ação × "
            "atividade × produto)."
        ),
        "NU_PROCESSO": (
            "Nº do processo da medida de fiscalização, 17 dígitos; não é o de um produto."
        ),
        "NU_CNPJ": (
            "CNPJ de quem abriu o dossiê, não da empresa fiscalizada: muitas vezes a própria "
            "ANVISA (03112386000111) ou um marketplace."
        ),
        "NO_RAZAO_SOCIAL": "Razão social de quem abriu o dossiê.",
        "CO_TIPO_PRODUTO": (
            "Código da área: 1 medicamento, 2 cosmético, 3 saneante, 6 alimento, entre outros."
        ),
        "DS_TIPO_PRODUTO": "Área do produto (Alimento, Cosmético, Saneantes, Medicamento…).",
        "CO_RISCO": "Código do risco: 19, 20 e 21 são Risco I, II e III.",
        "DS_RISCO_PRODUTO": "Classe de risco do produto.",
        "TOTAL_MEDIDA_CAUTELAR": "Quantas medidas cautelares o dossiê tem (1 a 5).",
        "NO_EMPRESA_INVESTIGADA": "Nome da empresa fiscalizada; vazio em muitas linhas.",
        "NU_CNPJ_EMPRESA_INVESTIGADA": (
            "A empresa fiscalizada: 14 dígitos quando é um CNPJ, 11 quando é uma pessoa (CPF), "
            'ou texto ("DESCONHECIDO", "Não se aplica"); vazio em parte das linhas.'
        ),
        "CO_ASSUNTO": "Código do assunto da medida.",
        "DT_PUBLICACAO_MEDIDA": "Publicação mais recente entre as medidas do dossiê"
        + _BRASILIA
        + ".",
        "CO_ACAO_FISCALIZACAO": "Código de ds_acao_fiscalizacao (1 a 6).",
        "CO_ATIVIDADE_FISCALIZACAO": (
            "Código de ds_atividade_fiscalizacao; vazio em parte das linhas."
        ),
        "DT_PUBLICACAO": "Quando esta medida foi publicada" + _BRASILIA + ".",
        "PRODUTOS_CONCATENADOS": (
            'Todos os produtos do dossiê num texto só, separados por ". ", cortado em 4.000 '
            "caracteres; prefira produto."
        ),
        "DS_ACAO_FISCALIZACAO": (
            "A ação: suspensão, proibição, recolhimento, apreensão, interdição ou inutilização."
        ),
        "DS_ATIVIDADE_FISCALIZACAO": (
            "A atividade atingida (fabricação, comercialização, distribuição, propaganda…)."
        ),
        "ACAO_ATIVIDADE": "As ações e atividades do dossiê numa frase.",
        "PRODUTO": "O produto desta linha, como escrito na medida; vazio em poucas linhas.",
        "REGISTRO": "Nº de registro do produto, até 11 dígitos; vazio em toda linha de alimento.",
        "DT_CARGA_ETL": _CARGA_ETL,
    },
    "cosmeticos": {
        "NU_PROCESSO": _PROCESSO
        + " Único por produto; veja st_registrado para as linhas em dobro.",
        "NO_PRODUTO": _NOME_PRODUTO,
        "NU_CNPJ_EMPRESA": _CNPJ,
        "NO_RAZAO_SOCIAL_EMPRESA": _RAZAO + " Uma por CNPJ.",
        "DT_VENCIMENTO": (
            "Vencimento da regularização" + _BRASILIA + "; vazio em 15% das linhas, e há datas "
            "passadas em produtos ainda listados como ativos."
        ),
        "ST_SITUACAO_PRODUTO": "Produto ativo (regularização vigente).",
        "NU_REGISTRO": (
            "Nº de registro (9 dígitos) nos registrados e em parte dos isentos; vazio nos demais. "
            'Uma linha traz o texto "null".'
        ),
        "DS_TIPO_PETICAO": (
            "Tipo da regularização: Notificado, ISENTO DE REGISTRO, REGISTRO ou DESCARTAVEL; "
            "vazio exatamente nas linhas com st_registrado verdadeiro."
        ),
        "ST_REGISTRADO": (
            "Verdadeiro na linha do registro. 11.144 processos aparecem duas vezes, uma com cada "
            "valor, mesmo CNPJ e mesmo registro; conte produtos com DISTINCT nu_processo."
        ),
        "DT_ATUALIZACAO": _ATUALIZACAO,
    },
    # derived tables (derivar.py): keyed by output column
    "produtos": _PRODUTOS_COLS,
    "empresas": {
        "cnpj": "CNPJ, 14 dígitos com zeros à esquerda. Texto, nunca número.",
        "nome": (
            "Razão social: a grafia mais frequente nas tabelas de produtos (empate: ordem "
            "alfabética); nas empresas só com medidas, o nome escrito na medida, que pode faltar."
        ),
        "alimentos": "Alimentos e suplementos da empresa (produtos, por co_seq_produto).",
        "alimentos_ativos": "Desses, quantos com regularização ativa.",
        "saneantes": "Saneantes da empresa (processos).",
        "saneantes_ativos": "Desses, quantos ativos.",
        "cosmeticos": "Cosméticos da empresa (processos distintos; um par de linhas conta uma).",
        "cosmeticos_ativos": "Desses, quantos ativos, pela linha sem registro do par.",
        "produtos": "alimentos + saneantes + cosmeticos.",
        "ativos": "alimentos_ativos + saneantes_ativos + cosmeticos_ativos.",
        "medidas": (
            "Dossiês de fiscalização em que a empresa é a fiscalizada "
            "(produtos_irregulares.nu_cnpj_empresa_investigada); 0 quando nenhum."
        ),
        "medida_ultima": "Data da publicação mais recente entre essas medidas; nulo sem medidas.",
    },
    "medidas": {
        "dossie": "Identificador do dossiê (produtos_irregulares.co_seq_dossie_investig_med).",
        "produto": "Produto atingido, como escrito na medida, sem espaços nas pontas.",
        "area": "Área do produto (Alimento, Cosmético, Saneantes, Medicamento…).",
        "area_codigo": "Código da área: 1 medicamento, 2 cosmético, 3 saneante, 6 alimento.",
        "risco": "Classe de risco do produto (Risco I, II ou III).",
        "processo": "Nº do processo da medida, 17 dígitos; não é o de um produto.",
        "cnpj": (
            "CNPJ da empresa fiscalizada, 14 dígitos; nulo quando a origem traz CPF, texto ou nada."
        ),
        "empresa": "Nome da empresa fiscalizada; nulo quando falta ou quando o alvo é uma pessoa.",
        "pessoa": "Verdadeiro quando o alvo é uma pessoa física (CPF na origem).",
        "registros": (
            "Números de registro citados na medida, só dígitos, em ordem; lista vazia nos "
            "alimentos."
        ),
        "acoes": (
            "Ações da medida, em ordem alfabética: Apreensão, Interdição cautelar, Inutilização, "
            "Proibição, Recolhimento, Suspensão."
        ),
        "atividades": (
            "Atividades atingidas, em ordem alfabética (Comercialização, Distribuição, "
            "Fabricação, Propaganda, Uso…); vazia quando a origem não traz."
        ),
        "primeira": "Data da primeira publicação entre as medidas do dossiê para este produto.",
        "ultima": "Data da publicação mais recente.",
        "autor_cnpj": (
            "CNPJ de quem abriu o dossiê: a própria ANVISA (03112386000111), um marketplace ou "
            "a própria empresa (recolhimento voluntário)."
        ),
        "autor": "Razão social de quem abriu o dossiê.",
    },
    "alimentos_por_produto": {
        **_PRODUTOS_COLS,
        "situacao_assunto": (
            'Situação da petição na ANVISA: "Publicado deferimento", "Anuído", "Publicado '
            'indeferimento"…; igual em todas as apresentações.'
        ),
        "alegacao": 'Alegações funcionais autorizadas, separadas por ";"; nulo quando não há.',
        "publicacao": (
            "Publicação mais recente da regularização" + _BRASILIA + "; nulo em quase metade."
        ),
        "situacao_em": "Data mais recente da situação atual" + _BRASILIA + "; nulo em parte.",
        "inicio_analise": (
            "Início mais antigo de análise entre as apresentações"
            + _BRASILIA
            + "; nulo na maioria."
        ),
    },
    "cosmeticos_por_empresa": {
        "cnpj": _CNPJ,
        "ordem": "Posição do produto na lista da empresa, de 1 a 100: ativos antes, depois nome.",
        "id": "Nº do processo (o identificador do cosmético); único com cnpj.",
        "nome": _NOME_PRODUTO,
        "empresa": _RAZAO,
        "processo": _PROCESSO,
        "registro": "Nº de registro, quando há; vazio nos notificados e isentos.",
        "ativo": "Regularização ativa na ANVISA no dia do build (st_situacao_produto).",
        "tipo": '"Registrado", "Notificado", "Isento de registro" ou "Descartável".',
        "vencimento": "Vencimento da regularização (data); nulo quando a ANVISA não informa.",
    },
    "medicamentos": {
        "TIPO_PRODUTO": 'Sempre "MEDICAMENTO".',
        "NOME_PRODUTO": _NOME_PRODUTO,
        "DATA_FINALIZACAO_PROCESSO": "Quando o registro foi concedido (data).",
        "CATEGORIA_REGULATORIA": (
            "Categoria do medicamento: Similar, Genérico, Novo, Específico, Biológico, "
            "Fitoterápico, Dinamizado, Radiofármaco, Produto de Terapia Avançada, Gases "
            "Medicinais, BAIXO RISCO; vem como a ANVISA escreve, com grafias duplas, um ? no "
            "lugar de Á e nomes truncados; vazio em 9 mil linhas."
        ),
        "NUMERO_REGISTRO_PRODUTO": (
            "Nº de registro do produto, 9 dígitos (os 4 da apresentação não estão aqui); "
            "vazio em 21 mil linhas. Texto, nunca número."
        ),
        "DATA_VENCIMENTO_REGISTRO": (
            "Vencimento do registro, com precisão de mês (dia 1); vazio em parte das linhas."
        ),
        "NUMERO_PROCESSO": _PROCESSO,
        "CLASSE_TERAPEUTICA": (
            "Classe terapêutica, em maiúsculas (ex.: ANTIDIABETICOS); vazia em 26 mil linhas."
        ),
        "EMPRESA_DETENTORA_REGISTRO": (
            "CNPJ (14 dígitos) e razão social da empresa detentora, juntos com ' - '."
        ),
        "SITUACAO_REGISTRO": '"Ativo" ou "Inativo": a situação do registro no dia do build.',
        "PRINCIPIO_ATIVO": (
            "Princípio ativo em minúsculas; associações listam vários, separados por vírgula "
            "ou +; vazio em 32 mil linhas."
        ),
    },
    "produtos_saude": {
        "NUMERO_REGISTRO_CADASTRO": (
            "Nº de registro ou cadastro do produto, 10 ou 11 dígitos; começa com 8 nos "
            "cadastros (classes I e II) e com 1 nos registros. Texto, nunca número."
        ),
        "NUMERO_PROCESSO": _PROCESSO,
        "NOME_TECNICO": (
            "Nome técnico do produto na nomenclatura da ANVISA (ex.: Luvas Descartaveis), "
            "o mesmo da consulta nomeTecnico da API."
        ),
        "CLASSE_RISCO": "Classe de risco do dispositivo: I (menor), II, III ou IV (maior).",
        "NOME_COMERCIAL": "Nome comercial do produto como regularizado.",
        "CNPJ_DETENTOR_REGISTRO_CADASTRO": _CNPJ,
        "DETENTOR_REGISTRO_CADASTRO": _RAZAO,
        "NOME_FABRICANTE": (
            "Fabricante; um produto com vários fabricantes tem uma linha por fabricante. "
            "Vazio em 12,7 mil linhas."
        ),
        "NOME_PAIS_FABRIC": (
            "País do fabricante, como a ANVISA escreve (ex.: CHINA, REPÚBLICA POPULAR)."
        ),
        "DT_PUB_REGISTRO_CADASTRO": (
            "Publicação do registro ou cadastro (data); vazia em 22 linhas."
        ),
        "VALIDADE_REGISTRO_CADASTRO": (
            'Validade: a palavra "VIGENTE" nos cadastros (sem prazo) ou uma data dd/mm/aaaa '
            "nos registros, como texto; há registros com a data já passada."
        ),
        "DT_ATUALIZACAO_DADO": _ATUALIZACAO,
    },
    "cmed": _CMED,
}
