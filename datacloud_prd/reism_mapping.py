"""
Salesforce カスタム項目と Ecforce API 概念（Name / APIItemGroup / APIItem）の対応。

データソース: ``ladder_reism_mapping.csv``（Data Loader 用の reism マッピングエクスポート）。
列 ``reismobj__AccountField__c`` … ``reismobj__OrderItemField__c`` のいずれかに
Salesforce API 名（例: ``ecf_id__c``）が入り、どのオブジェクトスロットに書くかが分かる。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

# CSV 列名 → 論理スロット（AppFlow の Order / Account 等と照合しやすい短名）
SF_SLOT_COLUMNS: tuple[tuple[str, str], ...] = (
    ("reismobj__AccountDetailField__c", "account_detail"),
    ("reismobj__AccountField__c", "account"),
    ("reismobj__CatalogItemDetailField__c", "catalog_item_detail"),
    ("reismobj__CatalogItemField__c", "catalog_item"),
    ("reismobj__ContactField__c", "contact"),
    ("reismobj__DistributionPriceField__c", "distribution_price"),
    ("reismobj__FixedValue__c", "fixed_value"),
    ("reismobj__OrderDetailField__c", "order_detail"),
    ("reismobj__OrderField__c", "order"),
    ("reismobj__OrderItemDetailField__c", "order_item_detail"),
    ("reismobj__OrderItemField__c", "order_item"),
)

_DEFAULT_PATH = Path(__file__).resolve().parent / "ladder_reism_mapping.csv"


def load_reism_mapping(path: Path | None = None) -> pd.DataFrame:
    """UTF-8 BOM 想定。全列文字列。"""
    p = path or _DEFAULT_PATH
    return pd.read_csv(p, dtype=str, keep_default_na=False, encoding="utf-8-sig")


def row_targets(row: pd.Series) -> dict[str, str]:
    """1 行について、スロット短名 → Salesforce フィールド API 名（非空のみ）。"""
    out: dict[str, str] = {}
    for col, slot in SF_SLOT_COLUMNS:
        v = str(row.get(col, "") or "").strip()
        if v:
            out[slot] = v
    return out


def targets_for_name(df: pd.DataFrame, name: str) -> dict[str, str]:
    """``Name`` 列（例: ``Order_id``）に一致する行のスロット→フィールド。"""
    n = str(name).strip()
    hit = df[df["Name"].astype(str).str.strip() == n]
    if hit.empty:
        return {}
    return row_targets(hit.iloc[0])


def names_for_order_field(df: pd.DataFrame, sf_field: str) -> list[str]:
    """Order オブジェクトの ``reismobj__OrderField__c`` が ``sf_field`` である行の ``Name`` 一覧。"""
    col = "reismobj__OrderField__c"
    f = str(sf_field).strip()
    sub = df[df[col].astype(str).str.strip() == f]
    return sub["Name"].astype(str).str.strip().tolist()


def names_for_order_item_field(df: pd.DataFrame, sf_field: str) -> list[str]:
    col = "reismobj__OrderItemField__c"
    f = str(sf_field).strip()
    sub = df[df[col].astype(str).str.strip() == f]
    return sub["Name"].astype(str).str.strip().tolist()


def names_for_account_field(df: pd.DataFrame, sf_field: str) -> list[str]:
    col = "reismobj__AccountField__c"
    f = str(sf_field).strip()
    sub = df[df[col].astype(str).str.strip() == f]
    return sub["Name"].astype(str).str.strip().tolist()


def mapping_path_from_cfg(cfg: dict[str, Any], default: Path | None = None) -> Path:
    """``crm_s3_sources.json`` の ``reism_mapping_csv``（相対ならスクリプト配置ディレクトリ基準）。"""
    raw = str((cfg.get("reism_mapping_csv") or "")).strip()
    if raw:
        p = Path(raw)
        if not p.is_absolute():
            p = Path(__file__).resolve().parent / p
        return p
    return default or _DEFAULT_PATH


def load_reism_mapping_from_cfg(cfg: dict[str, Any]) -> pd.DataFrame:
    return load_reism_mapping(mapping_path_from_cfg(cfg))
