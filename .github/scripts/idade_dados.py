"""Fail when no ANVISA source file changed for more than N days.

usage: python3 -I idade_dados.py <manifest.json> <max days>

Reads every table's source Last-Modified (and each file's, for the two-file CMED source) from the
manifest and prints a GitHub Actions error when the newest one is older than the limit. Standard
library only: it runs before nothing is installed.
"""

import json
import sys
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

manifest, limite = json.load(open(sys.argv[1], encoding="utf-8")), int(sys.argv[2])
datas = []
for tabela in manifest["tables"].values():
    fonte = tabela.get("source") or {}
    for item in [fonte, *(fonte.get("files") or [])]:
        if item.get("last_modified"):
            datas.append(parsedate_to_datetime(item["last_modified"]))
if not datas:
    print("::error::the manifest has no source Last-Modified to check")
    sys.exit(1)
mais = max(datas)
dias = (datetime.now(timezone.utc) - mais).days
print(f"newest ANVISA file: {mais:%Y-%m-%d %H:%M} UTC ({dias} days ago)")
if dias > limite:
    print(f"::error::no ANVISA source file changed in {dias} days (newest {mais:%Y-%m-%d}).")
    sys.exit(1)
