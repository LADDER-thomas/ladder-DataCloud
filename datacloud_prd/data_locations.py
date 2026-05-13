"""S3 バケット等の環境別参照先。値は data_locations.json に集約する。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

DATA_LOCATIONS_FILE = "data_locations.json"

_REQUIRED_KEYS = (
    "crm_tables_bucket",
    "datacloud_export_default_upload_bucket",
)


def load_data_locations(base_dir: Path) -> dict[str, Any]:
    """
    ``base_dir / data_locations.json`` を読み、必須キーを検証して返す。
    """
    path = base_dir / DATA_LOCATIONS_FILE
    if not path.is_file():
        sample = base_dir / "data_locations.sample.json"
        raise FileNotFoundError(
            f"データ参照設定がありません: {path}\n"
            f"次をコピーして編集してください: {sample}"
        )
    with path.open(encoding="utf-8") as f:
        raw: Any = json.load(f)
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: ルートは JSON オブジェクトである必要があります")
    for k in _REQUIRED_KEYS:
        v = str(raw.get(k, "") or "").strip()
        if not v:
            raise ValueError(f"{path}: 必須キー {k!r} が空または未設定です")
    return raw
