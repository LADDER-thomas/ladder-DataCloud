"""stg-appflow 等から本店受注・定期の論理列に揃えた CSV を取得・縦結合する。"""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pandas as pd

from crm_s3 import _join_s3_key, get_object_text


def _as_key_list(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        s = raw.strip()
        return [s] if s else []
    if isinstance(raw, (list, tuple)):
        out: list[str] = []
        for x in raw:
            s = str(x).strip()
            if s:
                out.append(s)
        return out
    return []


def _resolve_object_keys(block: dict[str, Any], stem: str) -> list[str]:
    for k in (f"{stem}_keys", stem):
        if k in block and block[k] is not None:
            return _as_key_list(block.get(k))
    return []


def load_appflow_column_mapping(path: Path) -> dict[str, dict[str, str]]:
    """
    JSON: ``{ "jukkan": { "論理列名(従来受注)": "AppFlow側の列名" }, "teiki": { ... } }``
    空オブジェクトのときは列名が従来と同一であることを前提とする。
    """
    if not path.is_file():
        return {"jukkan": {}, "teiki": {}}
    with path.open(encoding="utf-8") as f:
        raw = json.load(f)
    if not isinstance(raw, dict):
        return {"jukkan": {}, "teiki": {}}
    j = raw.get("jukkan") or {}
    t = raw.get("teiki") or {}
    if not isinstance(j, dict):
        j = {}
    if not isinstance(t, dict):
        t = {}
    jm = {str(a).strip(): str(b).strip() for a, b in j.items() if str(a).strip() and str(b).strip()}
    tm = {str(a).strip(): str(b).strip() for a, b in t.items() if str(a).strip() and str(b).strip()}
    return {"jukkan": jm, "teiki": tm}


def _read_csv_text(text: str) -> pd.DataFrame:
    return pd.read_csv(io.StringIO(text), dtype=str, keep_default_na=False)


def _rename_to_legacy(df: pd.DataFrame, legacy_to_appflow: dict[str, str]) -> pd.DataFrame:
    """legacy_to_appflow: 従来の日本語列名 -> AppFlow 上の実列名。"""
    if not legacy_to_appflow:
        return df.copy()
    inv = {v: k for k, v in legacy_to_appflow.items()}
    return df.rename(columns=inv)


def _merge_vertical(dfs: list[pd.DataFrame]) -> pd.DataFrame:
    if not dfs:
        return pd.DataFrame()
    col_order: list[str] = []
    seen: set[str] = set()
    for d in dfs:
        for c in d.columns:
            if c not in seen:
                seen.add(c)
                col_order.append(c)
    parts: list[pd.DataFrame] = []
    for d in dfs:
        x = d.copy()
        for c in col_order:
            if c not in x.columns:
                x[c] = ""
        parts.append(x[col_order])
    out = pd.concat(parts, ignore_index=True)
    for c in out.columns:
        out[c] = out[c].fillna("").astype(str)
    return out


def _to_csv_text(df: pd.DataFrame) -> str:
    buf = io.StringIO()
    df.to_csv(buf, index=False, encoding="utf-8-sig")
    return buf.getvalue()


def merged_appflow_csv_texts(
    cfg: dict[str, Any],
    *,
    mapping_path: Path,
    which: str,
) -> str:
    """
    ``which`` は ``jukkan`` または ``teiki``。
    設定 ``appflow`` の bucket / prefix とオブジェクトキー列から取得し、
    複数ファイルは縦結合して UTF-8 BOM 付き CSV テキストを返す。
    """
    block = cfg.get("appflow") or {}
    bucket = str(block.get("bucket") or "").strip()
    if not bucket:
        raise ValueError("appflow.bucket が空です")
    prefix = str(block.get("prefix") or "").strip()
    r_app = str(block.get("region") or "").strip()
    region = r_app or str(cfg.get("region") or "").strip() or None
    keys = _resolve_object_keys(block, which)
    if not keys:
        raise ValueError(
            f"appflow に {which}_keys（または {which}）でオブジェクトキーを 1 件以上指定してください"
        )
    mapping = load_appflow_column_mapping(mapping_path)
    legacy_map = mapping.get(which) or {}

    dfs: list[pd.DataFrame] = []
    for name in keys:
        key = _join_s3_key(prefix, name)
        text = get_object_text(bucket, key, region)
        raw = _read_csv_text(text)
        dfs.append(_rename_to_legacy(raw, legacy_map))
    merged = _merge_vertical(dfs)
    return _to_csv_text(merged)


def merged_appflow_jukkan_teiki_texts(
    cfg: dict[str, Any],
    *,
    mapping_path: Path,
) -> tuple[str, str]:
    return (
        merged_appflow_csv_texts(cfg, mapping_path=mapping_path, which="jukkan"),
        merged_appflow_csv_texts(cfg, mapping_path=mapping_path, which="teiki"),
    )
