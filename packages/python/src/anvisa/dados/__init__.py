"""Open data from dados.anvisa.gov.br: ANVISA's bulk CSVs, published as typed Parquet.

    from anvisa.dados import build
    build("dist")   # dist/manifest.json, dist/index.html, dist/data/<build_id>/*.parquet

Importing this package does not import DuckDB; only converting does (`anvisa[dados]` extra).
"""

from ..errors import DadosError, SchemaDriftError
from .build import Unchanged, build
from .catalog import CATALOG, Dataset, select
from .fetch import Source

__all__ = [
    "CATALOG",
    "DadosError",
    "Dataset",
    "SchemaDriftError",
    "Source",
    "Unchanged",
    "build",
    "select",
]
