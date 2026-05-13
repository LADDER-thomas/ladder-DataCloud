"""reism_to_legacy_column_rules.json の読み込みとカバレッジ補助。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_DEFAULT_RULES = Path(__file__).resolve().parent / "reism_to_legacy_column_rules.json"

# build_crm_tables / name_matching / brand_filter が参照する論理列（抜けチェック用）
REQUIRED_JUKKAN_COLUMNS: frozenset[str] = frozenset(
    {
        "顧客番号",
        "メールアドレス",
        "性別",
        "生年月日",
        "会員ランク名",
        "LINE ID",
        "更新日",
        "顧客購入回数",
        "顧客タイプ名",
        "顧客ID",
        "請求先（都道府県）",
        "請求先（郵便番号フル）",
        "請求先（住所フル）",
        "請求先（電話番号フル）",
        "請求先（名前フル）",
        "お届け先（都道府県）",
        "お届け先（郵便番号フル）",
        "お届け先（住所フル）",
        "お届け先（電話番号フル）",
        "お届け先（名前フル）",
        "受注番号",
        "受注日",
        "支払い合計",
        "合計",
        "支払い方法",
        "受注種別",
        "対応状況",
        "送料",
        "決済状況",
        "取消日",
        "購入商品（商品コード）",
        "購入商品（SKUコード）",
        "購入商品（商品名）",
        "購入商品（単価）",
        "購入商品（個数）",
        "購入商品（商品カテゴリー）",
        "購入商品（原価）",
        "購入URL",
        "広告URLグループ名",
        "媒体",
        "クーポン",
        "デバイス",
        "受注経路",
    }
)

REQUIRED_TEIKI_COLUMNS: frozenset[str] = frozenset(
    {
        "顧客番号",
        "メールアドレス",
        "性別",
        "生年月日",
        "会員ランク名",
        "LINE ID",
        "更新日",
        "顧客購入回数",
        "顧客タイプ名",
        "顧客ID",
        "お届け先（都道府県）",
        "お届け先（郵便番号フル）",
        "お届け先（住所フル）",
        "お届け先（電話番号フル）",
        "お届け先（名前フル）",
        "定期受注番号",
        "定期受注ID",
        "媒体",
        "ステータス",
        "定期回数",
        "配送サイクル",
        "何ヶ月ごと",
        "何日に",
        "何日ごと",
        "支払い方法",
        "広告URLグループ",
        "広告主",
        "次回配送予定日",
        "キャンセル日",
        "停止理由",
        "キャンセル理由",
        "購入商品（SKUコード）",
        "購入商品（商品コード）",
        "購入商品（商品カテゴリー）",
    }
)


def load_transform_rules(path: Path | None = None) -> dict[str, Any]:
    p = path or _DEFAULT_RULES
    with p.open(encoding="utf-8") as f:
        return json.load(f)


def rule_columns(rules: dict[str, Any], key: str) -> set[str]:
    arr = rules.get(key) or []
    return {str(x.get("column", "")).strip() for x in arr if str(x.get("column", "")).strip()}


def coverage_gaps(rules: dict[str, Any]) -> tuple[set[str], set[str]]:
    """ルール JSON に無い必須列（実装前のギャップ一覧）。"""
    j = REQUIRED_JUKKAN_COLUMNS - rule_columns(rules, "jukkan")
    t = REQUIRED_TEIKI_COLUMNS - rule_columns(rules, "teiki")
    return j, t
