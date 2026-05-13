#!/usr/bin/env python3
"""
リポジトリ直下から CRM ビルドを起動するラッパー（本番用 datacloud_prd）。
実体は datacloud_prd/build_crm_tables.py（カレントは datacloud_prd に合わせる）。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
_SCRIPT_DIR = _ROOT / "datacloud_prd"
_SCRIPT = _SCRIPT_DIR / "build_crm_tables.py"

if not _SCRIPT.is_file():
    print(f"見つかりません: {_SCRIPT}", file=sys.stderr)
    print("datacloud_prd フォルダが同じ階層にあるか確認してください。", file=sys.stderr)
    raise SystemExit(2)

raise SystemExit(
    subprocess.call(
        [sys.executable, str(_SCRIPT)] + sys.argv[1:],
        cwd=str(_SCRIPT_DIR),
    )
)
