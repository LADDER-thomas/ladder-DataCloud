#!/usr/bin/env python3
"""
5 つのソース CSV から CRM 分析用 6 テーブル（CSV）を生成する。

入力:
  本店受注・定期: crm_s3_sources.json の appflow（stg-appflow 等）から
  NDJSON 全結合または AppFlow CSV 縦結合。定期行は本店 Order のみ
  （モール・TikTok に定期 04 用の入力はない）。

  モール・TikTok・メール: crm_s3_sources.json の bucket / objects から S3 GetObject。

出力（--out-dir、既定 crm_out）:
  列名は物理名（英語スネークケース）。
  01_顧客マスター.csv
  02_顧客住所配送先.csv
  03_注文明細商品.csv
  04_定期購入管理.csv
  05_解約理由マスター.csv
  06_販促チャネル.csv

05_解約理由マスター: mailexport の「分類１」に **「解約」** を含む行のみ（注文キャンセル系は対象外）。
「お届け日・サイクル・支払方法変更」「初回キャンセル」を含む分類１は除外。
`reason_id` と class1_segment_*（「>」「＞」分割）に格納。

01_顧客マスター: 解約済み顧客も含む。**定期解約フラグ**・**定期解約理由ID**（05 の reason_id 参照。
メールは分類１に「解約」がある場合のみ反映し、注文キャンセルのみのメールは含めない）。

03_注文明細・04_定期購入: customer_no（論理名 顧客番号）を持ち、名寄せ後の 01 と外部キー結合可能。

出力先: 生成 CSV をローカルに書き、続けて S3 バケット（--s3-output-bucket、既定は data_locations.json の crm_tables_bucket）へ PutObject。
実行前に、同一バケットの <出力プレフィックス>/ に既存の成果物 CSV があれば
同バケットの backup/実行日/時刻/ へ CopyObject でバックアップする。

--crm-brand で受注・定期・モール・TikTok を指定ブランドのみに絞れる（mailexport は対象外）。
省略時は crm_s3_sources.json の crm_brand、なければ --brand-mapping（既定 brand_mapping.json）の crm_brand / target_brand を参照。

名寄せ（Data Cloud 要件「名寄せルール」に準拠）:
  顧客: (1) メールアドレス (2) 電話番号 (3) 姓名相当+郵便番号 で同一人物クラスタを結合し、
  代表の顧客番号へ寄せる。モール/TikTok は MALL_/TIKTOK_ の疑似番号から上記キーで EC と結合可能。
  商品（SKU）: 商品名の一致に加え、sku_product_name_mapping.json で別名→正規名を上書き可能
  （出力列 product_name_canonical）。

本店・定期は従来どおり更新日ベースで 1 顧客番号 1 行に潰したうえで、上記クラスタリングを適用。

mailexport.csv: From/To から顧客メールを推定し集計、メールアドレス一致で顧客マスターに
「メール履歴_*」列を付与。EC に無いメールのみ MAIL_* 顧客番号で行追加。
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

import pandas as pd

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from crm_s3 import (  # noqa: E402
    backup_existing_crm_outputs_on_s3,
    brand_mapping_s3_uri,
    ensure_brand_mapping_on_s3,
    fetch_all_crm_source_texts,
    load_s3_sources_config,
    put_object_text,
    set_default_crm_tables_bucket,
)
from data_locations import load_data_locations  # noqa: E402
from name_matching import (  # noqa: E402
    attach_canonical_product_names,
    build_customer_canonical_id_mapping,
    load_sku_product_name_mapping,
    remap_customer_id_series,
)
from brand_filter import (  # noqa: E402
    apply_transactional_brand_filter,
    load_brand_mapping_from_text,
)
from appflow_sources import merged_appflow_jukkan_teiki_texts  # noqa: E402
from reism_appflow_join import legacy_jukkan_teiki_from_appflow_ndjson  # noqa: E402

# 解約理由マスター: 分類１を「>」分割するときの最大階層数（列 分類１_1 … 分類１_N）
_CLASS1_SEGMENT_LEVELS = 8


def _build_05_reason_logical_to_physical() -> dict[str, str]:
    m: dict[str, str] = {"理由ID": "reason_id"}
    for i in range(1, _CLASS1_SEGMENT_LEVELS + 1):
        m[f"分類１_{i}"] = f"class1_segment_{i}"
    return m


# ---------------------------------------------------------------------------
# 論理名（日本語・処理用）→ 物理名（英語・CSV 出力）
# ---------------------------------------------------------------------------
# テーブルごとの論理名→物理名（ドキュメント用コメント: 論理名の意味）
LOGICAL_TO_PHYSICAL = {
    "01_customer": {
        "顧客番号": "customer_no",  # 顧客番号
        "メールアドレス": "email",
        "性別": "gender",
        "生年月日": "birth_date",
        "会員ランク": "member_rank",
        "LINE ID": "line_id",
        "顧客購入回数": "lifetime_purchase_count",
        "顧客タイプ名": "platform_last_bought",
        "初回購入媒体": "platform_first_bought",
        "顧客ID": "erp_customer_id",
        "都道府県": "prefecture",
        "メール履歴_件数": "mail_history_count",
        "メール履歴_初回日時": "mail_history_first_at",
        "メール履歴_最終日時": "mail_history_last_at",
        "定期解約フラグ": "subscription_cancel_flag",
        "定期解約理由ID": "subscription_reason_id",
    },
    "02_address": {
        "顧客番号": "customer_no",
        "関連番号": "ref_no",
        "関連種別": "ref_type",
        "住所区分": "address_type",
        "郵便番号": "postal_code",
        "都道府県": "prefecture",
        "住所": "address_full",
        "電話番号": "phone_no",
        "基準日": "as_of_date",
    },
    "03_order_line": {
        "受注番号": "order_no",
        "顧客番号": "customer_no",
        "注文ソース": "order_source",
        "受注日": "order_date",
        "合計金額": "order_amount",
        "支払い方法": "payment_method",
        "受注種別": "order_type",
        "受注ステータス": "order_status",
        "送料": "shipping_fee",
        "決済状況": "payment_status",
        "モール名": "mall_name",
        "取消日": "cancel_date",
        "商品コード": "product_code",
        "SKU": "sku",
        "商品名": "product_name",
        "商品名_名寄せ": "product_name_canonical",
        "単価": "unit_price",
        "個数": "qty",
        "カテゴリ": "category",
        "原価": "cost_price",
    },
    "04_subscription": {
        "定期受注番号": "subscription_no",
        "定期受注ID": "subscription_erp_id",
        "顧客番号": "customer_no",
        "媒体": "sales_channel",
        "ステータス": "status",
        "定期回数": "subscription_count",
        "サイクル": "cycle_text",
        "支払い方法": "payment_method",
        "広告URLグループ": "ad_url_group",
        "広告主": "ad_publisher",
        "次回配送予定日": "next_delivery_scheduled_at",
        "キャンセル日": "cancel_date",
        "停止理由": "stop_reason",
        "キャンセル理由": "cancel_reason_raw",
        "定期解約理由ID": "subscription_reason_id",
        "定期突合用理由テキスト": "subscription_reason_map_text",
    },
    "05_reason": _build_05_reason_logical_to_physical(),
    "06_promotion": {
        "受注番号": "order_no",
        "購入URL": "purchase_url",
        "広告URLグループ": "ad_url_group",
        "媒体": "channel_media",
        "クーポン": "coupon",
        "デバイス": "device",
        "受注経路": "order_route",
    },
}


def _apply_physical_names(df: pd.DataFrame, table_key: str) -> pd.DataFrame:
    """論理列名を物理名に変換する。"""
    m = LOGICAL_TO_PHYSICAL[table_key]
    out = df.rename(columns={k: v for k, v in m.items() if k in df.columns})
    out = out.drop(columns=["データソース", "data_source_arn"], errors="ignore")
    head: list[str] = [v for v in m.values() if v in out.columns]
    tail = [c for c in out.columns if c not in head]
    return out[head + tail]


def _load_csv_from_text(text: str) -> pd.DataFrame:
    return pd.read_csv(io.StringIO(text), dtype=str, keep_default_na=False)


def _load_tiktok_from_text(text: str) -> pd.DataFrame:
    # 区切りがカンマだが、先頭フィールド直後にタブが混じる行があるため正規化
    lines = text.splitlines()
    if not lines:
        return pd.DataFrame()
    norm: list[str] = []
    header = lines[0].split(",")
    ncols = len(header)
    for i, line in enumerate(lines):
        if i == 0:
            norm.append(line)
            continue
        # 先頭の「数字+タブ+カンマ」を数字+カンマに
        line = re.sub(r"^(\d+)\t,", r"\1,", line)
        norm.append(line)
    buf = "\n".join(norm)
    try:
        return pd.read_csv(io.StringIO(buf), dtype=str, keep_default_na=False)
    except Exception:
        return pd.read_csv(io.StringIO(text), dtype=str, sep="\t", keep_default_na=False)


def _dataframes_jukkan_teiki_from_appflow(
    cfg: dict[str, Any], appflow_mapping: Path
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    本店・定期 DataFrame: ``appflow`` のバケットからのみ生成。
    `format` が ``ndjson_joined`` なら NDJSON 全結合、それ以外は ``merged_appflow_jukkan_teiki_texts``。
    """
    app = cfg.get("appflow") or {}
    fmt = str(app.get("format") or "csv").strip().lower()
    if fmt == "ndjson_joined":
        jt, tt = legacy_jukkan_teiki_from_appflow_ndjson(cfg)
    else:
        jt, tt = merged_appflow_jukkan_teiki_texts(
            cfg,
            mapping_path=appflow_mapping.resolve(),
        )
    return _load_csv_from_text(jt), _load_csv_from_text(tt)


def _strip_money(s: str) -> str:
    s = (s or "").strip()
    s = re.sub(r"[,\sJPY円]", "", s, flags=re.I)
    return s


def _df_col_str(df: pd.DataFrame, name: str) -> pd.Series:
    """列が無いときは空文字 Series（インデックス一致）。"""
    if name in df.columns:
        return df[name].fillna("").astype(str).map(lambda x: str(x).strip())
    return pd.Series("", index=df.index, dtype=str)


# メールログから顧客アドレスを拾うための簡易ルール
_EMAIL_RE = re.compile(
    r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}",
    re.I,
)
# 自社ドメイン（問い合わせ窓口）— 相手側を顧客とみなす
_INTERNAL_EMAIL_DOMAINS = frozenset(
    {
        "pthree.jp",
        "okusuri.help",
    }
)
_INTERNAL_LOCALS = frozenset({"support", "noreply", "no-reply", "info", "mailer-daemon"})


def _primary_email(raw: str) -> str:
    if not raw or not str(raw).strip():
        return ""
    s = str(raw).strip().strip('"').strip("'")
    m = _EMAIL_RE.search(s)
    return m.group(0).lower() if m else ""


def _is_internal_email(email: str) -> bool:
    if not email or "@" not in email:
        return True
    local, _, domain = email.partition("@")
    dl = domain.lower()
    if dl in _INTERNAL_EMAIL_DOMAINS or any(
        dl.endswith("." + d) for d in _INTERNAL_EMAIL_DOMAINS
    ):
        return True
    if local.lower() in _INTERNAL_LOCALS:
        return True
    return False


def _customer_emails_from_mail_row(row: pd.Series) -> list[str]:
    """1 行について、顧客とみなすメールアドレス（正規化済み）を列挙。"""
    f = _primary_email(row.get("Fromアドレス", ""))
    t = _primary_email(row.get("Toアドレス", ""))
    fi = _is_internal_email(f) if f else True
    ti = _is_internal_email(t) if t else True
    if fi and ti:
        return []
    if not fi and ti:
        return [f]
    if fi and not ti:
        return [t]
    return [f, t]


def aggregate_mail_stats(mail: pd.DataFrame) -> pd.DataFrame:
    """メールアドレス単位の件数・初回・最終受信（受送信時刻）。"""
    empty = pd.DataFrame(
        columns=[
            "email_norm",
            "メール履歴_件数",
            "メール履歴_初回日時",
            "メール履歴_最終日時",
        ]
    )
    if mail.empty or "受送信時刻" not in mail.columns:
        return empty
    recs: list[tuple[str, str]] = []
    for _, row in mail.iterrows():
        ts = str(row.get("受送信時刻", "") or "").strip()
        for em in _customer_emails_from_mail_row(row):
            if em:
                recs.append((em, ts))
    if not recs:
        return empty
    dfp = pd.DataFrame(recs, columns=["email_norm", "_ts_raw"])
    dfp["_dt"] = pd.to_datetime(dfp["_ts_raw"], errors="coerce")
    g = dfp.groupby("email_norm", as_index=False).agg(
        メール履歴_件数=("email_norm", "count"),
        _tmin=("_dt", "min"),
        _tmax=("_dt", "max"),
    )

    def _fmt_dt(x: object) -> str:
        if pd.isna(x):
            return ""
        try:
            return pd.Timestamp(x).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return ""

    g["メール履歴_初回日時"] = g["_tmin"].map(_fmt_dt)
    g["メール履歴_最終日時"] = g["_tmax"].map(_fmt_dt)
    bad = (g["メール履歴_初回日時"] == "") & (g["メール履歴_最終日時"] == "")
    if bad.any():
        raw_min = dfp.groupby("email_norm")["_ts_raw"].min()
        raw_max = dfp.groupby("email_norm")["_ts_raw"].max()
        for i in g.index[bad]:
            em = g.at[i, "email_norm"]
            if em in raw_min.index:
                g.at[i, "メール履歴_初回日時"] = str(raw_min[em])
                g.at[i, "メール履歴_最終日時"] = str(raw_max[em])
    g = g.drop(columns=["_tmin", "_tmax"])
    g["メール履歴_件数"] = g["メール履歴_件数"].astype(int)
    return g


def enrich_customer_master_with_mail(
    customer: pd.DataFrame,
    mail: pd.DataFrame,
) -> pd.DataFrame:
    """顧客マスターにメール履歴列を付与し、ログのみに存在するメールを MAIL_* 行で追加。"""
    stats = aggregate_mail_stats(mail)
    base_cols = list(customer.columns)
    mail_cols = ["メール履歴_件数", "メール履歴_初回日時", "メール履歴_最終日時"]

    if stats.empty:
        out = customer.copy()
        for c in mail_cols:
            out[c] = ""
        if "_customer_snapshot_at" not in out.columns:
            out["_customer_snapshot_at"] = pd.NaT
        return out

    out = customer.copy()
    out["_em_norm"] = out["メールアドレス"].map(
        lambda x: _primary_email(str(x)) if x else ""
    )
    merged = out.merge(stats, how="left", left_on="_em_norm", right_on="email_norm")
    merged = merged.drop(columns=["email_norm"], errors="ignore")
    merged["メール履歴_件数"] = merged["メール履歴_件数"].fillna(0)
    merged["メール履歴_件数"] = merged["メール履歴_件数"].astype(int)
    for c in ("メール履歴_初回日時", "メール履歴_最終日時"):
        merged[c] = merged[c].fillna("")
    merged = merged.drop(columns=["_em_norm"])

    existing_norm = {
        _primary_email(str(x))
        for x in out["メールアドレス"].values
        if str(x).strip()
    }
    existing_norm.discard("")

    only_mail = stats[~stats["email_norm"].isin(existing_norm)].copy()
    if only_mail.empty:
        return merged[base_cols + mail_cols]

    only_mail["顧客番号"] = only_mail["email_norm"].map(
        lambda e: "MAIL_" + hashlib.md5(e.encode("utf-8")).hexdigest()[:16]
    )
    only_mail["メールアドレス"] = only_mail["email_norm"]
    for c in base_cols:
        if c not in only_mail.columns:
            if c == "_customer_snapshot_at":
                only_mail[c] = pd.NaT
            else:
                only_mail[c] = ""
    add = only_mail.drop(columns=["email_norm"])[base_cols + mail_cols]
    return pd.concat([merged[base_cols + mail_cols], add], ignore_index=True)


def _pick_last_by_customer(df: pd.DataFrame, customer_col: str, date_col: str | None) -> pd.DataFrame:
    if df.empty:
        return df
    if date_col and date_col in df.columns:
        d = df.copy()
        d["_sort"] = pd.to_datetime(d[date_col], errors="coerce")
        d = d.sort_values("_sort")
    else:
        d = df.copy()
    return d.groupby(customer_col, as_index=False).last()


def build_customer_master(
    jukkan: pd.DataFrame,
    teiki: pd.DataFrame,
    mall: pd.DataFrame,
    tiktok: pd.DataFrame,
) -> pd.DataFrame:
    rows: list[pd.DataFrame] = []
    # 本店受注（顧客番号あり）
    if not jukkan.empty and "顧客番号" in jukkan.columns:
        j_cols = [
            c
            for c in [
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
            ]
            if c in jukkan.columns
        ]
        j = jukkan[j_cols].copy()
        for c in ("顧客購入回数", "顧客タイプ名", "顧客ID"):
            if c not in j.columns:
                j[c] = ""
        if "請求先（都道府県）" in jukkan.columns:
            j["都道府県"] = jukkan.loc[j.index, "請求先（都道府県）"].astype(str)
        else:
            j["都道府県"] = ""
        rows.append(_pick_last_by_customer(j, "顧客番号", "更新日" if "更新日" in j.columns else None))
    # 定期（teiki）ソース
    if not teiki.empty and "顧客番号" in teiki.columns:
        t_cols = [
            c
            for c in [
                "顧客番号",
                "メールアドレス",
                "生年月日",
                "会員ランク名",
                "LINE ID",
                "更新日",
                "顧客購入回数",
                "顧客タイプ名",
                "顧客ID",
            ]
            if c in teiki.columns
        ]
        t = teiki[t_cols].copy()
        if "性別" not in t.columns:
            t["性別"] = ""
        for c in ("顧客購入回数", "顧客タイプ名", "顧客ID"):
            if c not in t.columns:
                t[c] = ""
        if "お届け先（都道府県）" in teiki.columns:
            t["都道府県"] = teiki.loc[t.index, "お届け先（都道府県）"].astype(str)
        else:
            t["都道府県"] = ""
        rows.append(_pick_last_by_customer(t, "顧客番号", "更新日" if "更新日" in t.columns else None))
    if rows:
        base = pd.concat(rows, ignore_index=True)
        base = _pick_last_by_customer(
            base, "顧客番号", "更新日" if "更新日" in base.columns else None
        )
    else:
        base = pd.DataFrame(
            columns=[
                "顧客番号",
                "メールアドレス",
                "性別",
                "生年月日",
                "会員ランク",
                "LINE ID",
                "顧客購入回数",
                "顧客タイプ名",
                "顧客ID",
                "都道府県",
                "_customer_snapshot_at",
            ]
        )
    # 本店・定期の最新行時刻（名寄せ後の重複解消に使用）
    if not base.empty and "更新日" in base.columns:
        base["_customer_snapshot_at"] = pd.to_datetime(base["更新日"], errors="coerce")
    elif not base.empty and "_customer_snapshot_at" not in base.columns:
        base["_customer_snapshot_at"] = pd.NaT
    # 列名統一
    if "会員ランク名" in base.columns:
        base = base.rename(columns={"会員ランク名": "会員ランク"})
    elif "会員ランク" not in base.columns:
        base["会員ランク"] = ""

    # モール: 顧客番号なし → 注文ID ベースの外部キー（注文明細は複数行のため注文単位で一意化）
    if not mall.empty and "注文ID" in mall.columns:
        m = mall.drop_duplicates(subset=["注文ID"], keep="last").copy()
        m["顧客番号"] = m["注文ID"].map(lambda x: f"MALL_{x}")
        m["メールアドレス"] = ""
        m["性別"] = ""
        m["生年月日"] = ""
        m["会員ランク"] = ""
        m["LINE ID"] = ""
        if "モール名" in m.columns:
            m["顧客タイプ名"] = m["モール名"].fillna("").astype(str)
        else:
            m["顧客タイプ名"] = ""
        for c in ("顧客購入回数", "顧客ID", "都道府県"):
            m[c] = ""
        if "注文取込日時" in m.columns:
            m["_customer_snapshot_at"] = pd.to_datetime(
                m["注文取込日時"], errors="coerce"
            )
        elif "モール注文日時" in m.columns:
            m["_customer_snapshot_at"] = pd.to_datetime(
                m["モール注文日時"], errors="coerce"
            )
        else:
            m["_customer_snapshot_at"] = pd.NaT
        base = pd.concat(
            [
                base,
                m[
                    [
                        "顧客番号",
                        "メールアドレス",
                        "性別",
                        "生年月日",
                        "会員ランク",
                        "LINE ID",
                        "顧客購入回数",
                        "顧客タイプ名",
                        "顧客ID",
                        "都道府県",
                        "_customer_snapshot_at",
                    ]
                ],
            ],
            ignore_index=True,
        )

    if not tiktok.empty and "注文ID" in tiktok.columns:
        tt = tiktok.drop_duplicates(subset=["注文ID"], keep="last").copy()
        tt["顧客番号"] = tt["注文ID"].map(lambda x: f"TIKTOK_{x}")
        tt["メールアドレス"] = ""
        tt["性別"] = ""
        tt["生年月日"] = ""
        tt["会員ランク"] = ""
        tt["LINE ID"] = ""
        tt["顧客タイプ名"] = "TikTok"
        for c in ("顧客購入回数", "顧客ID", "都道府県"):
            tt[c] = ""
        if "注文作成時刻" in tt.columns:
            tt["_customer_snapshot_at"] = pd.to_datetime(
                tt["注文作成時刻"], errors="coerce"
            )
        else:
            tt["_customer_snapshot_at"] = pd.NaT
        base = pd.concat(
            [
                base,
                tt[
                    [
                        "顧客番号",
                        "メールアドレス",
                        "性別",
                        "生年月日",
                        "会員ランク",
                        "LINE ID",
                        "顧客購入回数",
                        "顧客タイプ名",
                        "顧客ID",
                        "都道府県",
                        "_customer_snapshot_at",
                    ]
                ],
            ],
            ignore_index=True,
        )

    out = base[
        [
            "顧客番号",
            "メールアドレス",
            "性別",
            "生年月日",
            "会員ランク",
            "LINE ID",
            "顧客購入回数",
            "顧客タイプ名",
            "顧客ID",
            "都道府県",
            "_customer_snapshot_at",
        ]
    ].copy()
    return out


def _address_line_with_ecf_fallback(
    row: pd.Series, prefer_col: str, col_names: set[str]
) -> str:
    """従来の住所フルが空のとき ``ecf_shipping_full_address__c``（Ecforce 配送先フル）を使う。"""
    v = str(row.get(prefer_col, "") or "").strip()
    if v:
        return v
    if "ecf_shipping_full_address__c" in col_names:
        v2 = str(row.get("ecf_shipping_full_address__c", "") or "").strip()
        if v2:
            return v2
    return ""


def build_customer_address(jukkan: pd.DataFrame, teiki: pd.DataFrame) -> pd.DataFrame:
    adr: list[pd.DataFrame] = []
    ju_cols = set(jukkan.columns) if not jukkan.empty else set()
    te_cols = set(teiki.columns) if not teiki.empty else set()
    if not jukkan.empty:
        for _, r in jukkan.iterrows():
            cid = r.get("顧客番号", "")
            oid = r.get("受注番号", "")
            day = r.get("受注日", "")
            if "請求先（住所フル）" in jukkan.columns:
                adr.append(
                    pd.DataFrame(
                        [
                            {
                                "顧客番号": cid,
                                "関連番号": oid,
                                "関連種別": "受注",
                                "住所区分": "請求先",
                                "郵便番号": r.get("請求先（郵便番号フル）", ""),
                                "都道府県": r.get("請求先（都道府県）", ""),
                                "住所": _address_line_with_ecf_fallback(
                                    r, "請求先（住所フル）", ju_cols
                                ),
                                "電話番号": r.get("請求先（電話番号フル）", ""),
                                "基準日": day,
                            }
                        ]
                    )
                )
            if "お届け先（住所フル）" in jukkan.columns:
                adr.append(
                    pd.DataFrame(
                        [
                            {
                                "顧客番号": cid,
                                "関連番号": oid,
                                "関連種別": "受注",
                                "住所区分": "お届け先",
                                "郵便番号": r.get("お届け先（郵便番号フル）", ""),
                                "都道府県": r.get("お届け先（都道府県）", ""),
                                "住所": _address_line_with_ecf_fallback(
                                    r, "お届け先（住所フル）", ju_cols
                                ),
                                "電話番号": r.get("お届け先（電話番号フル）", ""),
                                "基準日": day,
                            }
                        ]
                    )
                )
    if not teiki.empty:
        for _, r in teiki.iterrows():
            cid = r.get("顧客番号", "")
            sid = r.get("定期受注番号", "")
            day = r.get("更新日", "")
            adr.append(
                pd.DataFrame(
                    [
                        {
                            "顧客番号": cid,
                            "関連番号": sid,
                            "関連種別": "定期受注",
                            "住所区分": "お届け先",
                            "郵便番号": r.get("お届け先（郵便番号フル）", ""),
                            "都道府県": r.get("お届け先（都道府県）", ""),
                            "住所": _address_line_with_ecf_fallback(
                                r, "お届け先（住所フル）", te_cols
                            ),
                            "電話番号": r.get("お届け先（電話番号フル）", ""),
                            "基準日": day,
                        }
                    ]
                )
            )
    if not adr:
        return pd.DataFrame(
            columns=[
                "顧客番号",
                "関連番号",
                "関連種別",
                "住所区分",
                "郵便番号",
                "都道府県",
                "住所",
                "電話番号",
                "基準日",
            ]
        )
    return pd.concat(adr, ignore_index=True)


def build_order_lines(
    jukkan: pd.DataFrame,
    mall: pd.DataFrame,
    tiktok: pd.DataFrame,
) -> pd.DataFrame:
    out: list[pd.DataFrame] = []
    if not jukkan.empty:
        j_cust = (
            jukkan["顧客番号"]
            if "顧客番号" in jukkan.columns
            else pd.Series("", index=jukkan.index, dtype=str)
        )
        j = pd.DataFrame(
            {
                "受注番号": jukkan.get("受注番号", ""),
                "顧客番号": j_cust,
                "注文ソース": "ec",
                "受注日": jukkan.get("受注日", ""),
                "合計金額": jukkan.get("支払い合計", jukkan.get("合計", "")),
                "支払い方法": jukkan.get("支払い方法", ""),
                "受注種別": jukkan.get("受注種別", ""),
                "受注ステータス": jukkan.get("対応状況", ""),
                "送料": jukkan.get("送料", ""),
                "決済状況": jukkan.get("決済状況", ""),
                "モール名": "",
                "取消日": jukkan.get("取消日", ""),
                "商品コード": jukkan.get("購入商品（商品コード）", ""),
                "SKU": jukkan.get("購入商品（SKUコード）", ""),
                "商品名": jukkan.get("購入商品（商品名）", ""),
                "単価": jukkan.get("購入商品（単価）", ""),
                "個数": jukkan.get("購入商品（個数）", ""),
                "カテゴリ": jukkan.get("購入商品（商品カテゴリー）", ""),
                "原価": jukkan.get("購入商品（原価）", ""),
            }
        )
        out.append(j)
    if not mall.empty:
        if "注文ID" in mall.columns:
            m_cust = mall["注文ID"].map(lambda x: f"MALL_{x}")
        else:
            m_cust = pd.Series("", index=mall.index, dtype=str)
        m = pd.DataFrame(
            {
                "受注番号": mall.get("モール注文番号", mall.get("注文ID", "")),
                "顧客番号": m_cust,
                "注文ソース": "mall",
                "受注日": mall.get("注文取込日時", mall.get("モール注文日時", "")),
                "合計金額": mall.get("商品合計金額", ""),
                "支払い方法": mall.get("決済方法区分", ""),
                "受注種別": "",
                "受注ステータス": mall.get("注文ステータス", ""),
                "送料": mall.get("配送料合計", ""),
                "決済状況": "",
                "モール名": mall.get("モール名", ""),
                "取消日": "",
                "商品コード": mall.get("商品ID", ""),
                "SKU": mall.get("SKUコード", ""),
                "商品名": mall.get("商品名", ""),
                "単価": mall.get("商品単価", ""),
                "個数": mall.get("注文個数", ""),
                "カテゴリ": "",
                "原価": "",
            }
        )
        out.append(m)
    if not tiktok.empty:
        amt = tiktok.get("注文金額", "").map(_strip_money)
        if "注文ID" in tiktok.columns:
            t_cust = tiktok["注文ID"].map(lambda x: f"TIKTOK_{x}")
        else:
            t_cust = pd.Series("", index=tiktok.index, dtype=str)
        t = pd.DataFrame(
            {
                "受注番号": tiktok.get("注文ID", ""),
                "顧客番号": t_cust,
                "注文ソース": "tiktok",
                "受注日": tiktok.get("注文作成時刻", ""),
                "合計金額": amt,
                "支払い方法": tiktok.get("支払い方法", ""),
                "受注種別": "",
                "受注ステータス": tiktok.get("注文状況", ""),
                "送料": (
                    tiktok["割引後の送料"].astype(str).map(_strip_money)
                    if "割引後の送料" in tiktok.columns
                    else pd.Series("", index=tiktok.index, dtype=str)
                ),
                "決済状況": "",
                "モール名": "",
                "取消日": tiktok.get("キャンセル日時", ""),
                "商品コード": "",
                "SKU": tiktok.get("セラーSKU", ""),
                "商品名": tiktok.get("商品名", ""),
                "単価": tiktok.get("SKU小計（割引後）", "").map(_strip_money),
                "個数": tiktok.get("数量", ""),
                "カテゴリ": tiktok.get("商品カテゴリー", ""),
                "原価": "",
            }
        )
        out.append(t)
    if not out:
        return pd.DataFrame()
    return pd.concat(out, ignore_index=True)


def _map_cancel_text_to_reason_id(
    erp_reason: str,
    reason_map: dict[str, str],
    details_ordered: list[str],
) -> str:
    """ERP 側の長文理由を、05 マスタ（分類１ベースのキー）の理由IDに寄せる。"""
    t = str(erp_reason or "").strip()
    if not t:
        return ""
    if t in reason_map:
        return reason_map[t]
    for d in details_ordered:
        if not d:
            continue
        if d in t or t in d:
            return reason_map[d]
    return ""


def build_subscription(
    teiki: pd.DataFrame,
    reason_map: dict[str, str],
    reason_details_order: list[str],
) -> pd.DataFrame:
    if teiki.empty:
        return pd.DataFrame(
            columns=[
                "定期受注番号",
                "定期受注ID",
                "顧客番号",
                "媒体",
                "ステータス",
                "定期回数",
                "サイクル",
                "支払い方法",
                "広告URLグループ",
                "広告主",
                "次回配送予定日",
                "キャンセル日",
                "停止理由",
                "キャンセル理由",
                "定期解約理由ID",
                "定期突合用理由テキスト",
            ]
        )

    def _cycle(row: pd.Series) -> str:
        parts = [
            row.get("配送サイクル", ""),
            row.get("何ヶ月ごと", ""),
            row.get("何日に", ""),
            row.get("何日ごと", ""),
        ]
        return " / ".join(p for p in parts if str(p).strip())

    def _cancel_text_row(row: pd.Series) -> str:
        c = str(row.get("キャンセル理由", "") or "").strip()
        s = str(row.get("停止理由", "") or "").strip()
        return c if c else s

    t_cust = (
        teiki["顧客番号"]
        if "顧客番号" in teiki.columns
        else pd.Series("", index=teiki.index, dtype=str)
    )
    s = pd.DataFrame(
        {
            "定期受注番号": _df_col_str(teiki, "定期受注番号"),
            "定期受注ID": _df_col_str(teiki, "定期受注ID"),
            "顧客番号": t_cust,
            "媒体": _df_col_str(teiki, "媒体"),
            "ステータス": _df_col_str(teiki, "ステータス"),
            "定期回数": _df_col_str(teiki, "定期回数"),
            "サイクル": teiki.apply(_cycle, axis=1),
            "支払い方法": _df_col_str(teiki, "支払い方法"),
            "広告URLグループ": _df_col_str(teiki, "広告URLグループ"),
            "広告主": _df_col_str(teiki, "広告主"),
            "次回配送予定日": _df_col_str(teiki, "次回配送予定日"),
            "キャンセル日": _df_col_str(teiki, "キャンセル日"),
            "停止理由": _df_col_str(teiki, "停止理由"),
            "キャンセル理由": _df_col_str(teiki, "キャンセル理由"),
            "定期突合用理由テキスト": teiki.apply(_cancel_text_row, axis=1),
        }
    )
    s["定期解約理由ID"] = s["定期突合用理由テキスト"].map(
        lambda x: _map_cancel_text_to_reason_id(
            str(x).strip(), reason_map, reason_details_order
        )
    )
    return s[
        [
            "定期受注番号",
            "定期受注ID",
            "顧客番号",
            "媒体",
            "ステータス",
            "定期回数",
            "サイクル",
            "支払い方法",
            "広告URLグループ",
            "広告主",
            "次回配送予定日",
            "キャンセル日",
            "停止理由",
            "キャンセル理由",
            "定期解約理由ID",
            "定期突合用理由テキスト",
        ]
    ]


def _mail_category_column(mail: pd.DataFrame) -> str | None:
    """分類１（全角1）または分類1。"""
    for key in ("分類１", "分類1"):
        if key in mail.columns:
            return key
    return None


# 分類１に **「解約」** を含む行のみ対象（注文キャンセル等の「キャンセル」のみは除外）
_MAIL_KAIYAKU_CATEGORY1_KEYWORD: str = "解約"

# 分類１に含まれていてもマスタ・突合対象から除外（定期便の変更・初回のみのキャンセル等）
_REASON_MASTER_EXCLUDE_CLASS1_SUBSTRINGS: tuple[str, ...] = (
    "お届け日・サイクル・支払方法変更",
    "初回キャンセル",
)


def _filter_mail_kaiyaku_category1(mail: pd.DataFrame) -> pd.DataFrame:
    """分類１に「解約」を含む行のみ（注文キャンセル系の分類は含めない）。対象外ラベルは除外。"""
    col = _mail_category_column(mail)
    if col is None or mail.empty:
        return pd.DataFrame()
    s = mail[col].astype(str)
    mask = s.str.contains(_MAIL_KAIYAKU_CATEGORY1_KEYWORD, na=False, regex=False)
    sub = mail[mask].copy()
    if sub.empty:
        return sub
    col = _mail_category_column(sub)
    if col is None:
        return pd.DataFrame()
    for ex in _REASON_MASTER_EXCLUDE_CLASS1_SUBSTRINGS:
        s2 = sub[col].astype(str)
        sub = sub[~s2.str.contains(ex, na=False, regex=False)].copy()
    return sub


def _split_class1_segments(raw: str) -> list[str]:
    """分類１を > または ＞ で分割し、前後空白を除いたセグメント列を返す。"""
    s = str(raw or "").strip()
    if not s:
        return []
    parts = [p.strip() for p in re.split(r"[>＞]", s)]
    return [p for p in parts if p]


def _class1_segments_padded(raw: str, n: int) -> list[str]:
    parts = _split_class1_segments(raw)
    out = parts[:n]
    while len(out) < n:
        out.append("")
    return out


def _empty_reason_master_columns() -> list[str]:
    cols = ["理由ID"]
    cols.extend(f"分類１_{i}" for i in range(1, _CLASS1_SEGMENT_LEVELS + 1))
    return cols


def build_reason_master(
    mail: pd.DataFrame,
) -> tuple[pd.DataFrame, dict[str, str], list[str]]:
    """
    mailexport のうち分類１に「解約」を含む行のみを対象にマスタ化（「キャンセル」のみは対象外）。
    分類１（除外後のユニーク値）を「>」「＞」で分割した各階層を分類１_1 … に格納。
    分類１に「お届け日・サイクル・支払方法変更」「初回キャンセル」を含む行はマスタ対象外。
    """
    mail_sub = _filter_mail_kaiyaku_category1(mail)
    col1 = _mail_category_column(mail)
    if mail_sub.empty or not col1:
        return pd.DataFrame(columns=_empty_reason_master_columns()), {}, []

    s1 = mail_sub[col1].astype(str).str.strip()
    uniq_details = sorted(
        {
            u
            for u in s1.tolist()
            if u
            and not any(
                ex in u for ex in _REASON_MASTER_EXCLUDE_CLASS1_SUBSTRINGS
            )
        }
    )
    if not uniq_details:
        return pd.DataFrame(columns=_empty_reason_master_columns()), {}, []

    nl = _CLASS1_SEGMENT_LEVELS

    records: list[dict[str, Any]] = []
    mapping: dict[str, str] = {}

    for i, u in enumerate(uniq_details):
        rid = f"CR{i + 1:04d}"
        mapping[u] = rid
        segs = _class1_segments_padded(u, nl)
        rec: dict[str, Any] = {"理由ID": rid}
        for j in range(nl):
            rec[f"分類１_{j + 1}"] = segs[j]
        records.append(rec)

    df = pd.DataFrame(records)
    return df, mapping, uniq_details


def merge_customer_subscription_fields(
    customer: pd.DataFrame,
    teiki: pd.DataFrame,
    mail_df: pd.DataFrame,
    reason_map: dict[str, str],
    reason_details_order: list[str],
) -> pd.DataFrame:
    """定期・メール（分類１に「解約」を含む行のみ）から定期解約フラグ・定期解約理由IDを付与（名寄せ前）。"""
    out = customer.copy()
    n = len(out)
    if n == 0:
        out["定期解約フラグ"] = pd.Series(dtype=str)
        out["定期解約理由ID"] = pd.Series(dtype=str)
        return out

    flags = ["0"] * n
    rids = [""] * n
    cid_to_i: dict[str, int] = {}
    for i in range(n):
        cid_to_i[str(out.at[i, "顧客番号"]).strip()] = i

    if not teiki.empty and "顧客番号" in teiki.columns:
        t = _pick_last_by_customer(
            teiki, "顧客番号", "更新日" if "更新日" in teiki.columns else None
        )
        for _, r in t.iterrows():
            cid = str(r.get("顧客番号", "") or "").strip()
            if cid not in cid_to_i:
                continue
            i = cid_to_i[cid]
            ccr = str(r.get("キャンセル理由", "") or "").strip()
            stp = str(r.get("停止理由", "") or "").strip()
            stat = str(r.get("ステータス", "") or "").strip()
            tx = ccr or stp
            flag_teiki = bool(tx) or any(
                k in stat for k in ("停止", "解約", "キャンセル", "退会")
            )
            rid = ""
            if tx:
                rid = _map_cancel_text_to_reason_id(tx, reason_map, reason_details_order)
            if flag_teiki:
                flags[i] = "1"
            if rid:
                rids[i] = rid

    mail_sub = _filter_mail_kaiyaku_category1(mail_df)
    col1 = _mail_category_column(mail_df)
    if not mail_sub.empty and col1:
        for i in range(n):
            em = _primary_email(str(out.at[i, "メールアドレス"] or ""))
            if not em:
                continue
            best_rid = ""
            for _, mr in mail_sub.iterrows():
                ems = _customer_emails_from_mail_row(mr)
                if em not in ems:
                    continue
                cat = str(mr.get(col1, "") or "").strip()
                rid = reason_map.get(cat, "") or _map_cancel_text_to_reason_id(
                    cat, reason_map, reason_details_order
                )
                flags[i] = "1"
                if rid:
                    best_rid = rid
            if best_rid:
                rids[i] = best_rid

    out["定期解約フラグ"] = flags
    out["定期解約理由ID"] = rids
    return out


def _agg_max_numeric_str(series: pd.Series) -> str:
    """名寄せ集約用: 顧客購入回数など数値文字列の最大。"""
    n = pd.to_numeric(series.astype(str).str.strip(), errors="coerce")
    if n.notna().any():
        m = float(n.max())
        if pd.notna(m):
            if m == int(m):
                return str(int(m))
            return str(m)
    return ""


def _dedupe_customer_master_rows(df: pd.DataFrame) -> pd.DataFrame:
    """顧客番号単位で重複除去。`顧客タイプ名` 等は `_customer_snapshot_at` が最も新しい行を採用。
    定期解約フラグはいずれかが 1 なら 1。定期解約理由IDは時系列で後ろから最初の非空。"""
    snap = "_customer_snapshot_at"
    if df.empty or "顧客番号" not in df.columns:
        return df
    if "定期解約フラグ" not in df.columns:
        return df.drop_duplicates(subset=["顧客番号"], keep="first")

    work = df.copy()
    if snap not in work.columns:
        work[snap] = pd.NaT
    work[snap] = pd.to_datetime(work[snap], errors="coerce")
    work = work.sort_values(["顧客番号", snap], na_position="first")

    pieces: list[pd.Series] = []
    for _, sub in work.groupby("顧客番号", sort=False):
        last = sub.iloc[-1].copy()
        if "顧客購入回数" in sub.columns:
            last["顧客購入回数"] = _agg_max_numeric_str(sub["顧客購入回数"])
        last["定期解約フラグ"] = (
            "1" if (sub["定期解約フラグ"].astype(str) == "1").any() else "0"
        )
        rid = ""
        for x in reversed(sub["定期解約理由ID"].tolist()):
            if str(x).strip():
                rid = str(x).strip()
                break
        last["定期解約理由ID"] = rid
        if snap in last.index:
            last = last.drop(labels=[snap])
        pieces.append(last)

    return pd.DataFrame(pieces).reset_index(drop=True)


def build_promotion(
    jukkan: pd.DataFrame,
    mall: pd.DataFrame,
    tiktok: pd.DataFrame,
) -> pd.DataFrame:
    out: list[pd.DataFrame] = []
    if not jukkan.empty:
        out.append(
            pd.DataFrame(
                {
                    "受注番号": jukkan.get("受注番号", ""),
                    "購入URL": jukkan.get("購入URL", ""),
                    "広告URLグループ": jukkan.get("広告URLグループ名", ""),
                    "媒体": jukkan.get("媒体", ""),
                    "クーポン": jukkan.get("クーポン", ""),
                    "デバイス": jukkan.get("デバイス", ""),
                    "受注経路": jukkan.get("受注経路", ""),
                }
            )
        )
    if not mall.empty:
        out.append(
            pd.DataFrame(
                {
                    "受注番号": mall.get("モール注文番号", mall.get("注文ID", "")),
                    "購入URL": "",
                    "広告URLグループ": "",
                    "媒体": mall.get("モール名", ""),
                    "クーポン": mall.get("クーポン利用合計", ""),
                    "デバイス": "",
                    "受注経路": "モール",
                }
            )
        )
    if not tiktok.empty and "注文ID" in tiktok.columns:
        out.append(
            pd.DataFrame(
                {
                    "受注番号": tiktok["注文ID"].astype(str),
                    "購入URL": "",
                    "広告URLグループ": "",
                    "媒体": "TikTok",
                    "クーポン": "",
                    "デバイス": "",
                    "受注経路": "tiktok",
                }
            )
        )
    if not out:
        return pd.DataFrame()
    return pd.concat(out, ignore_index=True)


def build_platform_first_bought(
    order_lines: pd.DataFrame,
    promotion: pd.DataFrame,
) -> pd.DataFrame:
    """注文明細（受注日）と販促チャネルを受注番号で突合し、顧客ごと最古の受注の `媒体` を返す。"""
    empty = pd.DataFrame(columns=["顧客番号", "初回購入媒体"])
    if order_lines.empty or promotion.empty:
        return empty
    if "受注番号" not in order_lines.columns or "顧客番号" not in order_lines.columns:
        return empty
    if "受注番号" not in promotion.columns or "媒体" not in promotion.columns:
        return empty
    ol = order_lines[["受注番号", "顧客番号", "受注日"]].drop_duplicates(
        subset=["受注番号"], keep="first"
    )
    pr = promotion[["受注番号", "媒体"]].drop_duplicates(
        subset=["受注番号"], keep="first"
    )
    m = ol.merge(pr, on="受注番号", how="inner")
    if m.empty:
        return empty
    m = m.copy()
    m["_dt"] = pd.to_datetime(m["受注日"], errors="coerce")
    m = m[m["_dt"].notna()]
    if m.empty:
        return empty
    idx = m.groupby("顧客番号", sort=False)["_dt"].idxmin()
    out = m.loc[idx, ["顧客番号", "媒体"]].rename(columns={"媒体": "初回購入媒体"})
    return out


def _resolve_effective_crm_brand(args: argparse.Namespace) -> str:
    """
    CLI の --crm-brand を最優先。未指定時は crm_s3_sources.json の crm_brand、
    なければ --brand-mapping 先の JSON の crm_brand / target_brand。
    """
    if args.crm_brand and str(args.crm_brand).strip():
        return str(args.crm_brand).strip()
    cfg_path = args.s3_config.resolve()
    if cfg_path.is_file():
        try:
            scfg = load_s3_sources_config(cfg_path)
            eb = str(scfg.get("crm_brand") or "").strip()
            if eb:
                return eb
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            pass
    bm = args.brand_mapping.resolve()
    if bm.is_file():
        try:
            with bm.open(encoding="utf-8") as f:
                raw: Any = json.load(f)
            if isinstance(raw, dict):
                eb = str(raw.get("crm_brand") or raw.get("target_brand") or "").strip()
                if eb:
                    return eb
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            pass
    return ""


def main() -> int:
    try:
        data_locs = load_data_locations(_SCRIPT_DIR)
    except (FileNotFoundError, ValueError, OSError, json.JSONDecodeError) as e:
        print(f"[config error] data_locations: {e}", file=sys.stderr)
        return 2

    crm_tables_bucket = str(data_locs["crm_tables_bucket"]).strip()
    set_default_crm_tables_bucket(crm_tables_bucket)

    p = argparse.ArgumentParser(description="CRM 6 テーブル CSV を生成")
    p.add_argument(
        "--s3-config",
        type=Path,
        default=_SCRIPT_DIR / "crm_s3_sources.json",
        help="CRM 入力用: バケット・objects・appflow 等（既定: スクリプト直下の crm_s3_sources.json）",
    )
    p.add_argument(
        "--out-dir",
        type=Path,
        default=_SCRIPT_DIR / "crm_out",
        help="出力先ディレクトリ",
    )
    p.add_argument(
        "--s3-output-bucket",
        default=crm_tables_bucket,
        help="生成 CSV をアップロードする S3 バケット（省略時は data_locations.json の crm_tables_bucket）",
    )
    p.add_argument(
        "--s3-output-prefix",
        default="",
        help="出力オブジェクトキーのプレフィックス（例: crm_out/）",
    )
    p.add_argument(
        "--s3-output-region",
        default=None,
        help="S3 PutObject（成果物・バックアップ）のリージョン（省略時は環境変数または boto 既定）",
    )
    p.add_argument(
        "--crm-brand",
        default=None,
        metavar="BRAND",
        help="指定ブランドの行のみ（受注・定期・モール・TikTok）。省略時は crm_s3_sources.json の crm_brand、なければ --brand-mapping の crm_brand/target_brand",
    )
    p.add_argument(
        "--brand-mapping",
        type=Path,
        default=_SCRIPT_DIR / "brand_mapping.json",
        help="--crm-brand 省略時の補助（JSON に crm_brand / target_brand）。絞り込み実行時のマッピング本体は crm_s3_sources の objects.brand_mapping（S3）",
    )
    p.add_argument(
        "--appflow-mapping",
        type=Path,
        default=_SCRIPT_DIR / "appflow_source_mapping.json",
        help="crm_s3_sources.json に appflow があるとき: 従来列名→AppFlow列名の JSON パス",
    )
    args = p.parse_args()

    effective_crm_brand = _resolve_effective_crm_brand(args)

    out_dir = args.out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    backup_region = (
        str(args.s3_output_region).strip()
        if args.s3_output_region and str(args.s3_output_region).strip()
        else (os.environ.get("AWS_DEFAULT_REGION", "").strip() or None)
    )
    out_prefix_early = str(args.s3_output_prefix or "").strip().strip("/")
    try:
        backup_existing_crm_outputs_on_s3(
            src_bucket=crm_tables_bucket,
            out_key_prefix=out_prefix_early,
            region=backup_region,
            dest_bucket=crm_tables_bucket,
        )
    except Exception as e:
        print(f"[error] S3 バックアップ失敗: {e}", file=sys.stderr)
        return 1

    cfg: dict[str, Any] | None = None
    cfg_path = args.s3_config
    if not cfg_path.is_file():
        sample = _SCRIPT_DIR / "crm_s3_sources.sample.json"
        print(
            f"S3 設定が見つかりません: {cfg_path}\n"
            f"次をコピーして編集してください: {sample}",
            file=sys.stderr,
        )
        return 2
    try:
        cfg = load_s3_sources_config(cfg_path)
        texts = fetch_all_crm_source_texts(cfg)
    except Exception as e:
        print(f"[s3 error] {e}", file=sys.stderr)
        return 1
    appflow_on = bool(str((cfg.get("appflow") or {}).get("bucket") or "").strip())
    need = (
        ("mall", "tiktok", "mail")
        if appflow_on
        else ("jukkan", "teiki", "mall", "tiktok", "mail")
    )
    for k in need:
        if k not in texts:
            print(f"S3 設定の objects に {k} がありません", file=sys.stderr)
            return 2
    if appflow_on:
        try:
            jukkan, teiki = _dataframes_jukkan_teiki_from_appflow(
                cfg, args.appflow_mapping
            )
        except Exception as e:
            print(f"[appflow error] {e}", file=sys.stderr)
            return 1
    else:
        jukkan = _load_csv_from_text(texts["jukkan"])
        teiki = _load_csv_from_text(texts["teiki"])
    mall = _load_csv_from_text(texts["mall"])
    tiktok = _load_tiktok_from_text(texts["tiktok"])
    mail_df = _load_csv_from_text(texts["mail"])
    if effective_crm_brand:
        bm_text = ensure_brand_mapping_on_s3(
            cfg, target_brand=effective_crm_brand
        )
        if not bm_text.strip():
            print(
                "ブランド絞り込み時は、crm_s3_sources.json の objects に "
                '"brand_mapping": "<バケット内のJSONオブジェクト名>" を追加してください。',
                file=sys.stderr,
            )
            hint = brand_mapping_s3_uri(cfg)
            if hint:
                print(f"  参照予定 URI: {hint}", file=sys.stderr)
            return 2
        try:
            mapping = load_brand_mapping_from_text(bm_text, strict=True)
        except ValueError as e:
            print(f"[error] ブランドマッピング: {e}", file=sys.stderr)
            return 1
        jukkan, teiki, mall, tiktok = apply_transactional_brand_filter(
            jukkan,
            teiki,
            mall,
            tiktok,
            target_brand=effective_crm_brand,
            mapping=mapping,
        )

    reason_df, reason_map, reason_details_order = build_reason_master(mail_df)

    if effective_crm_brand:
        mp_src = brand_mapping_s3_uri(cfg) or str(args.brand_mapping.resolve())
        print(
            f"[info] CRM ブランド絞り込み: {effective_crm_brand} ({mp_src})"
        )

    customer_master = build_customer_master(jukkan, teiki, mall, tiktok)
    customer_master = enrich_customer_master_with_mail(customer_master, mail_df)
    customer_master = merge_customer_subscription_fields(
        customer_master,
        teiki,
        mail_df,
        reason_map,
        reason_details_order,
    )

    _cmap = build_customer_canonical_id_mapping(
        jukkan, teiki, mall, tiktok, customer_master
    )
    customer_master = customer_master.copy()
    customer_master["顧客番号"] = remap_customer_id_series(
        customer_master["顧客番号"], _cmap
    )
    customer_master = _dedupe_customer_master_rows(customer_master)

    address_df = build_customer_address(jukkan, teiki)
    address_df = address_df.copy()
    address_df["顧客番号"] = remap_customer_id_series(address_df["顧客番号"], _cmap)

    sku_map = load_sku_product_name_mapping(
        _SCRIPT_DIR / "sku_product_name_mapping.json"
    )
    order_lines_df = build_order_lines(jukkan, mall, tiktok)
    order_lines_df = attach_canonical_product_names(order_lines_df, sku_map)
    order_lines_df = order_lines_df.copy()
    order_lines_df["顧客番号"] = remap_customer_id_series(
        order_lines_df["顧客番号"], _cmap
    )

    promotion_df = build_promotion(jukkan, mall, tiktok)
    pf_bought = build_platform_first_bought(order_lines_df, promotion_df)
    if pf_bought.empty:
        customer_master = customer_master.copy()
        customer_master["初回購入媒体"] = ""
    else:
        customer_master = customer_master.merge(
            pf_bought, on="顧客番号", how="left"
        )
    customer_master["初回購入媒体"] = (
        customer_master["初回購入媒体"].fillna("").astype(str)
    )

    subscription_df = build_subscription(teiki, reason_map, reason_details_order)
    subscription_df = subscription_df.copy()
    subscription_df["顧客番号"] = remap_customer_id_series(
        subscription_df["顧客番号"], _cmap
    )

    tables: list[tuple[str, str, pd.DataFrame]] = [
        ("01_顧客マスター.csv", "01_customer", customer_master),
        ("02_顧客住所配送先.csv", "02_address", address_df),
        ("03_注文明細商品.csv", "03_order_line", order_lines_df),
        ("04_定期購入管理.csv", "04_subscription", subscription_df),
        ("05_解約理由マスター.csv", "05_reason", reason_df),
        ("06_販促チャネル.csv", "06_promotion", promotion_df),
    ]

    out_region = args.s3_output_region or (
        str(cfg.get("region") or "").strip() or None
        if cfg is not None
        else None
    )
    out_bucket = str(args.s3_output_bucket or "").strip()
    out_prefix = str(args.s3_output_prefix or "").strip().strip("/")

    for name, table_key, df in tables:
        fp = out_dir / name
        out_df = _apply_physical_names(df, table_key)
        out_df.to_csv(fp, index=False, encoding="utf-8-sig")
        print(f"[ok] {fp} ({len(out_df)} rows)")
        if out_bucket:
            key = f"{out_prefix}/{name}" if out_prefix else name
            text = fp.read_text(encoding="utf-8-sig")
            try:
                put_object_text(out_bucket, key, text, out_region)
                print(f"     -> s3://{out_bucket}/{key}")
            except Exception as e:
                print(f"[s3 upload error] {e}", file=sys.stderr)
                return 1

    src = f"S3 ({args.s3_config.name})"
    print(
        f"[info] 入力ソース: {src} / mailexport {len(mail_df)} 行を顧客マスターに反映（履歴集計・MAIL_* 行追加）"
    )
    if out_bucket:
        print(f"[info] S3 出力: s3://{out_bucket}/" + (f"{out_prefix}/" if out_prefix else ""))
    return 0


if __name__ == "__main__":
    code = main()
    if code:
        raise SystemExit(code)
