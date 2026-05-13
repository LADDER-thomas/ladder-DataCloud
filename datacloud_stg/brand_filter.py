"""
マッピングマスタ（brand_mapping.json）と商品カテゴリでブランドを判定し、
指定ブランド（例: P3）の行だけを残す。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pandas as pd

_WS = re.compile(r"[\s\u3000]+")


def _default_brand_mapping() -> dict[str, Any]:
    return {
        "target_brand": "P3",
        "sku": {},
        "product_code": {},
        "category_includes_brand": ["P3", "P3 Booster"],
    }


def _normalize_brand_mapping_dict(raw: dict[str, Any]) -> dict[str, Any]:
    default = _default_brand_mapping()
    out = dict(default)
    for k in ("target_brand", "sku", "product_code", "category_includes_brand"):
        if k in raw:
            out[k] = raw[k]
    if not isinstance(out["sku"], dict):
        out["sku"] = {}
    if not isinstance(out["product_code"], dict):
        out["product_code"] = {}
    if not isinstance(out["category_includes_brand"], list):
        out["category_includes_brand"] = list(default["category_includes_brand"])
    out["sku"] = {
        str(k).strip(): str(v).strip()
        for k, v in out["sku"].items()
        if str(k).strip() and not str(k).strip().startswith("_")
    }
    out["product_code"] = {
        str(k).strip(): str(v).strip()
        for k, v in out["product_code"].items()
        if str(k).strip() and not str(k).strip().startswith("_")
    }
    return out


def load_brand_mapping(path: Path) -> dict[str, Any]:
    """ローカルの brand_mapping.json を読む。無い・不正なら空設定（カテゴリ推定のみ）。"""
    default = _default_brand_mapping()
    if not path.is_file():
        return default
    try:
        with path.open(encoding="utf-8") as f:
            raw: Any = json.load(f)
        if not isinstance(raw, dict):
            return default
        return _normalize_brand_mapping_dict(raw)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return default


def load_brand_mapping_from_text(text: str, *, strict: bool = False) -> dict[str, Any]:
    """
    JSON 文字列からマッピングを読む。
    ``strict=True`` のとき JSON 不正や dict 以外は ValueError。
    """
    default = _default_brand_mapping()
    if not (text or "").strip():
        if strict:
            raise ValueError("ブランドマッピングの本文が空です")
        return default
    try:
        raw: Any = json.loads(text)
        if not isinstance(raw, dict):
            if strict:
                raise ValueError("ブランドマッピング JSON のルートはオブジェクトである必要があります")
            return default
        return _normalize_brand_mapping_dict(raw)
    except json.JSONDecodeError as e:
        if strict:
            raise ValueError(f"ブランドマッピング JSON の解析に失敗しました: {e}") from e
        return default


def _split_multi(s: object) -> list[str]:
    if s is None or (isinstance(s, float) and pd.isna(s)):
        return []
    t = str(s).strip()
    if not t:
        return []
    parts: list[str] = []
    for chunk in t.split(","):
        c = chunk.strip()
        if c:
            parts.append(c)
    return parts


def _norm_cat_token(s: str) -> str:
    return _WS.sub("", s.strip())


def _category_matches_allowed(cat_raw: object, allowed: list[str]) -> bool:
    """カンマ区切りカテゴリが、いずれも allowed（空白正規化一致）に含まれるか。"""
    if not cat_raw or str(cat_raw).strip() == "":
        return False
    allowed_set = {_norm_cat_token(x) for x in allowed}
    parts = _split_multi(cat_raw)
    if not parts:
        return False
    for p in parts:
        n = _norm_cat_token(p)
        if n not in allowed_set:
            return False
    return True


def _resolve_line_brand(
    row: pd.Series,
    sku_col: str,
    pc_col: str,
    cat_col: str,
    cfg: dict[str, Any],
    target: str,
) -> bool:
    """
    行が target ブランドのみか。
    - SKU / 商品コードがマッピングにある場合はその値が target であること。
    - 複数 SKU の行は、列挙された SKU がすべてマッピング上 target（未登録 SKU がある場合はカテゴリでフォールバック）。
    """
    sku_map: dict[str, str] = cfg.get("sku") or {}
    pc_map: dict[str, str] = cfg.get("product_code") or {}
    allowed_cat: list[str] = list(cfg.get("category_includes_brand") or ["P3", "P3 Booster"])

    skus = _split_multi(row.get(sku_col)) if sku_col and sku_col in row.index else []
    pcs = _split_multi(row.get(pc_col)) if pc_col and pc_col in row.index else []

    brands_from_sku: list[str | None] = []
    for s in skus:
        brands_from_sku.append(sku_map.get(s) if s in sku_map else None)
    brands_from_pc: list[str | None] = []
    for p in pcs:
        brands_from_pc.append(pc_map.get(p) if p in pc_map else None)

    # マッピングで明示されたブランドが一つでも target 以外なら除外
    for b in brands_from_sku + brands_from_pc:
        if b is not None and b != target:
            return False

    # すべての SKU がマップにあり target
    if skus and all(b == target for b in brands_from_sku) and None not in brands_from_sku:
        return True
    # SKU なしで product_code のみ
    if not skus and pcs and all(b == target for b in brands_from_pc) and None not in brands_from_pc:
        return True
    # 一部のみマップ命中 + 残りはカテゴリ
    if skus and any(b is not None for b in brands_from_sku):
        if any(b is None for b in brands_from_sku):
            return _category_matches_allowed(row.get(cat_col, ""), allowed_cat)
        return all(b == target for b in brands_from_sku)

    # フォールバック: カテゴリのみ
    return _category_matches_allowed(row.get(cat_col, ""), allowed_cat)


def filter_jukkan_for_brand(df: pd.DataFrame, cfg: dict[str, Any], target: str) -> pd.DataFrame:
    if df.empty:
        return df
    mask = df.apply(
        lambda r: _resolve_line_brand(
            r,
            "購入商品（SKUコード）",
            "購入商品（商品コード）",
            "購入商品（商品カテゴリー）",
            cfg,
            target,
        ),
        axis=1,
    )
    return df[mask].copy()


def filter_teiki_for_brand(df: pd.DataFrame, cfg: dict[str, Any], target: str) -> pd.DataFrame:
    if df.empty:
        return df
    mask = df.apply(
        lambda r: _resolve_line_brand(
            r,
            "購入商品（SKUコード）",
            "購入商品（商品コード）",
            "購入商品（商品カテゴリー）",
            cfg,
            target,
        ),
        axis=1,
    )
    return df[mask].copy()


def filter_mall_for_brand(df: pd.DataFrame, cfg: dict[str, Any], target: str) -> pd.DataFrame:
    if df.empty:
        return df
    sku_map: dict[str, str] = cfg.get("sku") or {}
    pc_map: dict[str, str] = cfg.get("product_code") or {}
    has_map = bool(sku_map or pc_map)

    def _row_ok(r: pd.Series) -> bool:
        sk = str(r.get("SKUコード", "") or "").strip()
        pid = str(r.get("商品ID", "") or "").strip()
        name = str(r.get("商品名", "") or "")
        if sk and sk in sku_map:
            return sku_map[sk] == target
        if pid and pid in pc_map:
            return pc_map[pid] == target
        if has_map:
            # マッピングがあるが当該 SKU/商品ID が未登録 → 商品名・SKU にブランドを含む場合のみ許可（他ブランド除外用）
            if target and (target in name or target.lower() in sk.lower()):
                return True
            return False
        # マッピングファイルが空のときのフォールバック
        if target and target in name:
            return True
        if target and target.lower() in sk.lower():
            return True
        return False

    mask = df.apply(_row_ok, axis=1)
    return df[mask].copy()


def filter_tiktok_for_brand(df: pd.DataFrame, cfg: dict[str, Any], target: str) -> pd.DataFrame:
    if df.empty:
        return df
    sku_map: dict[str, str] = cfg.get("sku") or {}
    allowed_cat: list[str] = list(cfg.get("category_includes_brand") or ["P3", "P3 Booster"])

    def _row_ok(r: pd.Series) -> bool:
        sk = str(r.get("セラーSKU", "") or "").strip()
        if sk and sk in sku_map:
            return sku_map[sk] == target
        cat = r.get("商品カテゴリー", "")
        if _category_matches_allowed(cat, allowed_cat):
            return True
        if target and sk and target.lower() in sk.lower():
            return True
        return False

    mask = df.apply(_row_ok, axis=1)
    return df[mask].copy()


def apply_transactional_brand_filter(
    jukkan: pd.DataFrame,
    teiki: pd.DataFrame,
    mall: pd.DataFrame,
    tiktok: pd.DataFrame,
    *,
    target_brand: str,
    mapping: dict[str, Any],
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """受注・定期・モール・TikTok をブランドで絞り込む。``mapping`` は正規化済み dict。"""
    cfg = mapping
    tb = str(target_brand).strip()
    return (
        filter_jukkan_for_brand(jukkan, cfg, tb),
        filter_teiki_for_brand(teiki, cfg, tb),
        filter_mall_for_brand(mall, cfg, tb),
        filter_tiktok_for_brand(tiktok, cfg, tb),
    )
