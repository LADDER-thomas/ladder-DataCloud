#!/usr/bin/env python3
"""
商品マッピングマスタ（Excel）から brand_mapping.json 相当の dict / ファイルを生成する。

想定レイアウト（いずれか）:

1) **統合シート**（既定: 先頭シート）
   列: マッピング種別（sku / product_code）・コード・ブランド
   英語ヘッダー mapping_type, code, brand でも可。

2) **シート分割**
   シート名例: sku / product_code（または --sheet-sku / --sheet-product-code で指定）
   各シートは2列: コード列・ブランド列（列名は自動認識）。

任意シート「設定」または「settings」（--sheet-settings）:
   2列 key / value で target_brand, category_includes_brand（カンマ区切り）を上書き。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

import pandas as pd

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from brand_filter import _default_brand_mapping, _normalize_brand_mapping_dict

_WS = re.compile(r"[\s\u3000]+")


def _norm_header(s: object) -> str:
    t = str(s).strip().lower().replace("　", " ")
    return _WS.sub("", t)


def _cell_str(v: object) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    return str(v).strip()


def _find_column(df: pd.DataFrame, candidates: list[str]) -> str | None:
    """候補ヘッダの先頭から順に、一致する列名を返す（先に定義した別名を優先）。"""
    norm_to_actual = {_norm_header(c): str(c) for c in df.columns}
    for cand in candidates:
        n = _norm_header(cand)
        if n in norm_to_actual:
            return norm_to_actual[n]
    return None


def _norm_mapping_type(cell: object) -> str | None:
    s = _cell_str(cell).lower()
    if not s:
        return None
    if s in ("sku", "s", "1", "skuコード", "商品sku"):
        return "sku"
    if s in (
        "product_code",
        "product",
        "p",
        "2",
        "商品コード",
        "品番",
        "jan",
        "janコード",
    ):
        return "product_code"
    return None


def _parse_category_list(s: str) -> list[str]:
    out: list[str] = []
    for part in s.replace("、", ",").split(","):
        p = part.strip()
        if p:
            out.append(p)
    return out


def _read_settings_df(df: pd.DataFrame) -> dict[str, Any]:
    """2列（キー・値）想定。1列目を key、2列目を value。"""
    if df.shape[1] < 2:
        return {}
    c0, c1 = df.columns[0], df.columns[1]
    out: dict[str, Any] = {}
    for _, row in df.iterrows():
        k = _cell_str(row.get(c0))
        v = _cell_str(row.get(c1))
        if not k:
            continue
        nk = _norm_header(k)
        if nk == _norm_header("target_brand"):
            out["target_brand"] = v
        elif nk == _norm_header("category_includes_brand"):
            out["category_includes_brand"] = _parse_category_list(v)
    return out


def _load_settings_sheet(
    path: Path,
    sheet: str | int | None,
    *,
    engine: str,
) -> dict[str, Any]:
    if sheet is None:
        return {}
    try:
        df = pd.read_excel(path, sheet_name=sheet, dtype=object, engine=engine)
    except ValueError:
        return {}
    return _read_settings_df(df)


def _collect_mapping_rows_unified(df: pd.DataFrame) -> tuple[dict[str, str], dict[str, str]]:
    sku: dict[str, str] = {}
    product_code: dict[str, str] = {}

    col_type = _find_column(
        df,
        ["mapping_type", "マッピング種別", "種別", "type", "mappingtype"],
    )
    col_code = _find_column(
        df,
        ["code", "コード", "sku", "SKUコード", "商品コード", "セラーSKU", "品番"],
    )
    col_brand = _find_column(
        df,
        ["brand", "ブランド", "マッピングブランド", "brand_name"],
    )
    if not col_code or not col_brand:
        raise ValueError(
            "統合シートに「コード」相当列と「ブランド」相当列が見つかりません。"
            " code / コード / SKUコード / 商品コード 等と brand / ブランド を含むヘッダを付与してください。"
        )
    if not col_type:
        raise ValueError(
            "統合シートに「マッピング種別」列が見つかりません。"
            " sku 行と商品コード行を区別する列（mapping_type / マッピング種別 等）を追加するか、"
            " --mode split でシート分割モードを使ってください。"
        )

    for _, row in df.iterrows():
        code = _cell_str(row.get(col_code))
        brand = _cell_str(row.get(col_brand))
        if not code or not brand:
            continue
        mt = _norm_mapping_type(row.get(col_type))
        if mt == "sku":
            sku[code] = brand
        elif mt == "product_code":
            product_code[code] = brand
        else:
            continue

    return sku, product_code


def _collect_two_column_sheet(
    df: pd.DataFrame,
    *,
    kind: str,
) -> dict[str, str]:
    """SKU 用・商品コード用シート。コード列とブランド列を推定。"""
    if kind == "sku":
        code_candidates = [
            "sku",
            "SKUコード",
            "sku_code",
            "セラーSKU",
            "コード",
            "code",
        ]
    else:
        code_candidates = [
            "product_code",
            "商品コード",
            "jan",
            "品番",
            "コード",
            "code",
        ]
    brand_candidates = ["brand", "ブランド", "マッピングブランド", "brand_name"]

    col_code = _find_column(df, code_candidates)
    col_brand = _find_column(df, brand_candidates)
    if not col_code or not col_brand:
        raise ValueError(
            f"{kind} シート: コード列・ブランド列を自動認識できませんでした。"
            f" 想定ヘッダ例: {code_candidates[:4]} / {brand_candidates}"
        )
    out: dict[str, str] = {}
    for _, row in df.iterrows():
        code = _cell_str(row.get(col_code))
        brand = _cell_str(row.get(col_brand))
        if code and brand:
            out[code] = brand
    return out


def _sheet_names(path: Path, *, engine: str) -> list[str]:
    xl = pd.ExcelFile(path, engine=engine)
    return list(xl.sheet_names)


def build_brand_mapping_from_excel(
    path: Path,
    *,
    mode: str,
    sheet_unified: str | int,
    sheet_sku: str | None,
    sheet_product_code: str | None,
    sheet_settings: str | None,
    engine: str = "openpyxl",
) -> dict[str, Any]:
    path = path.resolve()
    if not path.is_file():
        raise FileNotFoundError(str(path))

    base = _default_brand_mapping()
    settings: dict[str, Any] = {}

    if sheet_settings:
        settings = _load_settings_sheet(path, sheet_settings, engine=engine)

    if mode == "unified":
        df = pd.read_excel(path, sheet_name=sheet_unified, dtype=object, engine=engine)
        sku, product_code = _collect_mapping_rows_unified(df)
    else:
        names = {n.lower(): n for n in _sheet_names(path, engine=engine)}
        sku_name = sheet_sku or names.get("sku")
        pc_name = sheet_product_code or names.get("product_code")
        if not sku_name or not pc_name:
            raise ValueError(
                "mode=split ではシート名 sku と product_code（または --sheet-sku / --sheet-product-code）が必要です。"
                f" 現在のシート: {list(names.values())}"
            )
        df_sku = pd.read_excel(path, sheet_name=sku_name, dtype=object, engine=engine)
        df_pc = pd.read_excel(path, sheet_name=pc_name, dtype=object, engine=engine)
        sku = _collect_two_column_sheet(df_sku, kind="sku")
        product_code = _collect_two_column_sheet(df_pc, kind="product_code")

    merged = {**base, **settings, "sku": sku, "product_code": product_code}
    return _normalize_brand_mapping_dict(merged)


def write_brand_mapping_json(
    data: dict[str, Any],
    out_path: Path,
    *,
    with_comment: bool = True,
) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = dict(data)
    if with_comment:
        payload = {
            "_comment": (
                "brand_mapping_from_excel で生成。sku / product_code は完全一致。"
                "未登録行は購入商品（商品カテゴリー）が category_includes_brand のみなら target とみなす。"
            ),
            **{k: v for k, v in payload.items() if not str(k).startswith("_")},
        }
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    out_path.write_text(text, encoding="utf-8")


def _parse_sheet_arg(s: str) -> str | int:
    t = str(s).strip()
    if t.isdigit():
        return int(t)
    return t


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Excel マッピングマスタから brand_mapping.json を生成する。",
    )
    p.add_argument(
        "excel_path",
        type=Path,
        help="入力 .xlsx のパス",
    )
    p.add_argument(
        "-o",
        "--output",
        type=Path,
        required=True,
        help="出力 brand_mapping.json のパス",
    )
    p.add_argument(
        "--mode",
        choices=("unified", "split"),
        default="unified",
        help="unified=1シートに種別・コード・ブランド / split=sku シートと product_code シート（既定: unified）",
    )
    p.add_argument(
        "--sheet",
        type=_parse_sheet_arg,
        default=0,
        help="unified 時のシート名または 0 始まりインデックス（既定: 0=先頭シート）",
    )
    p.add_argument("--sheet-sku", default=None, help="split 時: SKU マッピングのシート名")
    p.add_argument(
        "--sheet-product-code",
        default=None,
        help="split 時: 商品コードマッピングのシート名",
    )
    p.add_argument(
        "--sheet-settings",
        default=None,
        help="任意: target_brand / category_includes_brand を書いた設定シート名（設定 / settings 等）",
    )
    p.add_argument(
        "--no-comment",
        action="store_true",
        help="出力 JSON に _comment を付けない",
    )
    args = p.parse_args(argv)

    sheet_unified: str | int = args.sheet

    try:
        data = build_brand_mapping_from_excel(
            args.excel_path,
            mode=args.mode,
            sheet_unified=sheet_unified,
            sheet_sku=args.sheet_sku,
            sheet_product_code=args.sheet_product_code,
            sheet_settings=args.sheet_settings,
        )
    except Exception as e:
        print(f"[error] {e}", file=sys.stderr)
        return 1

    write_brand_mapping_json(
        data,
        args.output.resolve(),
        with_comment=not args.no_comment,
    )
    print(f"[ok] 出力: {args.output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
