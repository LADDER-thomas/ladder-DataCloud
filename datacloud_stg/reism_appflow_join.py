"""
stg-appflow の NDJSON（Account / Order / OrderItem / CatalogItem / DistributionPrice）を
すべて結合し、本店受注・定期の論理列（jukkan / teiki 中間表現）相当の UTF-8 BOM CSV テキストを返す。

方針は ``reism_to_legacy_column_rules.json`` の ``business_decisions`` に従う。
"""

from __future__ import annotations

import io
import json
from typing import Any

import pandas as pd

from aws_session import get_boto3_session
from crm_s3 import _join_s3_key

JUKKAN_LEGACY_COLUMNS: tuple[str, ...] = (
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
    "ecf_shipping_full_address__c",
)

TEIKI_LEGACY_COLUMNS: tuple[str, ...] = (
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
    "ecf_shipping_full_address__c",
)

_DEFAULT_PREFIXES: dict[str, str] = {
    "account": "la-sand-Account/",
    "order": "la-sand-reismobj__Order__c/",
    "order_item": "la-sand-reismobj__OrderItem__c/",
    "catalog_item": "la-sand-reismobj__CatalogItem__c/",
    "distribution_price": "la-sand-reismobj__DistributionPrice__c/",
}


def _appflow_block(cfg: dict[str, Any]) -> dict[str, Any]:
    return cfg.get("appflow") or {}


def _subscription_record_type_ids(cfg: dict[str, Any]) -> set[str]:
    """定期（teiki）行に含める ``reismobj__Order__c.RecordTypeId`` の集合（Setup で確認）。"""
    raw = _appflow_block(cfg).get("subscription_record_type_ids")
    if raw is None:
        return set()
    if isinstance(raw, str):
        s = raw.strip()
        return {s} if s else set()
    if isinstance(raw, (list, tuple)):
        return {str(x).strip() for x in raw if str(x).strip()}
    return set()


def _region(cfg: dict[str, Any]) -> str | None:
    b = _appflow_block(cfg)
    r = str(b.get("region") or "").strip()
    return r or str(cfg.get("region") or "").strip() or None


def _prefixes(cfg: dict[str, Any]) -> dict[str, str]:
    b = _appflow_block(cfg)
    raw = b.get("ndjson_prefixes")
    if isinstance(raw, dict) and raw:
        out: dict[str, str] = {}
        for k, v in raw.items():
            ks = str(k).strip()
            if ks.startswith("_"):
                continue
            vs = str(v).strip()
            if vs:
                out[ks] = vs
        return {**_DEFAULT_PREFIXES, **out}
    return dict(_DEFAULT_PREFIXES)


def _s3_client(cfg: dict[str, Any]):
    return get_boto3_session(_region(cfg)).client("s3", region_name=_region(cfg))


def _latest_object_key(client, bucket: str, folder_prefix: str) -> str | None:
    """``folder_prefix`` 配下で LastModified が最大のオブジェクトキー。"""
    best_k: str | None = None
    best_lm = None
    for page in client.get_paginator("list_objects_v2").paginate(
        Bucket=bucket, Prefix=folder_prefix
    ):
        for o in page.get("Contents") or []:
            k = o["Key"]
            if k.endswith("/"):
                continue
            lm = o["LastModified"]
            if best_lm is None or lm > best_lm:
                best_lm = lm
                best_k = k
    return best_k


def _load_ndjson_df(client, bucket: str, key: str) -> pd.DataFrame:
    resp = client.get_object(Bucket=bucket, Key=key)
    text = resp["Body"].read().decode("utf-8")
    rows: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        rows.append(json.loads(line))
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows)


def _pfx(df: pd.DataFrame, p: str) -> pd.DataFrame:
    if df.empty:
        return df.copy()
    out = df.copy()
    out.columns = [f"{p}_{c}" for c in out.columns]
    return out


def _first_nonempty_cols(df: pd.DataFrame, cols: list[str]) -> pd.Series:
    s = pd.Series("", index=df.index, dtype=str)
    for c in cols:
        if c not in df.columns:
            continue
        v = df[c].fillna("").astype(str).str.strip()
        v = v.replace({"nan": "", "None": "", "<NA>": ""})
        s = s.where(s.str.strip() != "", v)
    return s.fillna("").astype(str)


def _name_concat(last_col: str, first_col: str, df: pd.DataFrame) -> pd.Series:
    a = df[last_col].fillna("").astype(str).str.strip() if last_col in df.columns else ""
    b = df[first_col].fillna("").astype(str).str.strip() if first_col in df.columns else ""
    if isinstance(a, str):
        a = pd.Series([a] * len(df), index=df.index)
    if isinstance(b, str):
        b = pd.Series([b] * len(df), index=df.index)
    out = (a + b).where(a.ne("") & b.ne(""), a.where(a.ne(""), b))
    return out.fillna("").astype(str)


def _dedupe_distribution_price(dp: pd.DataFrame) -> pd.DataFrame:
    if dp.empty or "reismobj__CatalogItem__c" not in dp.columns:
        return dp
    d = dp.copy()
    lm = "LastModifiedDate"
    if lm in d.columns:
        d["_sort"] = pd.to_datetime(d[lm], errors="coerce")
        d = d.sort_values("_sort").drop_duplicates(
            subset=["reismobj__CatalogItem__c"], keep="last"
        )
        d = d.drop(columns=["_sort"], errors="ignore")
    else:
        d = d.drop_duplicates(subset=["reismobj__CatalogItem__c"], keep="last")
    return d


def load_appflow_entity_frames(cfg: dict[str, Any]) -> dict[str, pd.DataFrame]:
    """各論理エンティティを DataFrame で返す（列は生の API 名）。"""
    b = _appflow_block(cfg)
    bucket = str(b.get("bucket") or "").strip()
    if not bucket:
        raise ValueError("appflow.bucket が空です")
    base_prefix = str(b.get("prefix") or "").strip().strip("/")
    client = _s3_client(cfg)
    pfxs = _prefixes(cfg)
    out: dict[str, pd.DataFrame] = {}
    for logical, folder in pfxs.items():
        key_prefix = _join_s3_key(base_prefix, folder)
        key = _latest_object_key(client, bucket, key_prefix)
        if not key:
            print(f"[warn] appflow: プレフィックス下にオブジェクトがありません: s3://{bucket}/{key_prefix}")
            out[logical] = pd.DataFrame()
            continue
        print(f"[info] appflow NDJSON: {logical} <- s3://{bucket}/{key}")
        out[logical] = _load_ndjson_df(client, bucket, key)
    return out


def _df_get(frames: dict[str, pd.DataFrame], key: str) -> pd.DataFrame:
    v = frames.get(key)
    if v is None or not isinstance(v, pd.DataFrame):
        return pd.DataFrame()
    return v


def _join_line_level(frames: dict[str, pd.DataFrame]) -> pd.DataFrame:
    ord_raw = _df_get(frames, "order")
    li_raw = _df_get(frames, "order_item")
    acc_raw = _df_get(frames, "account")
    ci_raw = _df_get(frames, "catalog_item")
    dp_raw = _df_get(frames, "distribution_price")
    if li_raw.empty or ord_raw.empty:
        return pd.DataFrame()
    dp_d = _dedupe_distribution_price(dp_raw)
    ord_p = _pfx(ord_raw, "ord")
    li_p = _pfx(li_raw, "li")
    acc_p = _pfx(acc_raw, "acc")
    ci_p = _pfx(ci_raw, "ci")
    dp_p = _pfx(dp_d, "dp")
    x = li_p.merge(
        ord_p,
        left_on="li_reismobj__OrderId__c",
        right_on="ord_Id",
        how="inner",
    )
    if not acc_p.empty and "ord_reismobj__Account__c" in x.columns and "acc_Id" in acc_p.columns:
        x = x.merge(
            acc_p,
            left_on="ord_reismobj__Account__c",
            right_on="acc_Id",
            how="left",
        )
    if not ci_p.empty and "li_reismobj__CatalogItemId__c" in x.columns and "ci_Id" in ci_p.columns:
        x = x.merge(
            ci_p,
            left_on="li_reismobj__CatalogItemId__c",
            right_on="ci_Id",
            how="left",
        )
    if not dp_p.empty and "ci_Id" in x.columns and "dp_reismobj__CatalogItem__c" in dp_p.columns:
        x = x.merge(
            dp_p,
            left_on="ci_Id",
            right_on="dp_reismobj__CatalogItem__c",
            how="left",
        )
    return x


def _jukkan_from_wide(w: pd.DataFrame) -> pd.DataFrame:
    if w.empty:
        return pd.DataFrame(columns=JUKKAN_LEGACY_COLUMNS)
    out = pd.DataFrame()
    out["顧客番号"] = _first_nonempty_cols(
        w, ["ord_ecf_customer_number__c", "ord_ecf_number__c"]
    )
    out["メールアドレス"] = _first_nonempty_cols(
        w, ["ord_ecf_customer_email__c", "ord_ecf_email__c"]
    )
    out["性別"] = _first_nonempty_cols(w, ["acc_ecf_sex__c"])
    out["生年月日"] = _first_nonempty_cols(w, ["acc_ecf_birth__c"])
    out["会員ランク名"] = _first_nonempty_cols(w, ["acc_ecf_customer_rank_name__c"])
    out["LINE ID"] = _first_nonempty_cols(w, ["acc_ecf_line_id__c"])
    out["更新日"] = _first_nonempty_cols(
        w, ["ord_ecf_order_updated_at__c", "ord_LastModifiedDate"]
    )
    out["顧客購入回数"] = _first_nonempty_cols(
        w, ["acc_ecf_orders_count__c", "ord_ecf_orders_count__c"]
    )
    out["顧客タイプ名"] = _first_nonempty_cols(w, ["acc_ecf_customer_type_name__c"])
    out["顧客ID"] = _first_nonempty_cols(
        w, ["ord_ecf_customer_id__c", "acc_ecf_customer_id__c"]
    )
    out["請求先（都道府県）"] = _first_nonempty_cols(
        w, ["ord_reismobj__BillingState__c", "ord_ecf_prefecture_name__c"]
    )
    out["請求先（郵便番号フル）"] = _first_nonempty_cols(
        w, ["ord_reismobj__BillingPostalCode__c", "ord_ecf_full_zip__c"]
    )
    out["請求先（住所フル）"] = _first_nonempty_cols(w, ["ord_reismobj__BillingStreet__c"])
    out["請求先（電話番号フル）"] = _first_nonempty_cols(
        w, ["ord_reismobj__BillingPhone__c", "ord_ecf_full_tel__c"]
    )
    out["請求先（名前フル）"] = _name_concat(
        "ord_reismobj__BillingLastName__c",
        "ord_reismobj__BillingFirstName__c",
        w,
    )
    out["お届け先（都道府県）"] = _first_nonempty_cols(
        w, ["ord_reismobj__ShippingState__c", "ord_ecf_shipping_prefecture_name__c"]
    )
    out["お届け先（郵便番号フル）"] = _first_nonempty_cols(
        w, ["ord_reismobj__ShippingPostalCode__c", "ord_ecf_shipping_full_zip__c"]
    )
    out["お届け先（住所フル）"] = _first_nonempty_cols(
        w, ["ord_ecf_shipping_full_address__c", "ord_reismobj__ShippingStreet__c"]
    )
    out["お届け先（電話番号フル）"] = _first_nonempty_cols(
        w, ["ord_reismobj__ShippingPhone__c", "ord_ecf_shipping_full_tel__c"]
    )
    _ship_full = _first_nonempty_cols(w, ["ord_ecf_shipping_full_name__c"])
    _ship_concat = _name_concat(
        "ord_reismobj__ShippingLastName__c",
        "ord_reismobj__ShippingFirstName__c",
        w,
    )
    out["お届け先（名前フル）"] = _ship_full.where(
        _ship_full.str.strip() != "", _ship_concat
    )
    out["受注番号"] = _first_nonempty_cols(w, ["ord_reismobj__OrderNumber__c"])
    out["受注日"] = _first_nonempty_cols(w, ["ord_ecf_order_completed_at__c"])
    out["支払い合計"] = _first_nonempty_cols(
        w, ["ord_ecf_payment_total__c", "ord_ecf_total__c"]
    )
    out["合計"] = _first_nonempty_cols(w, ["ord_ecf_total__c"])
    out["支払い方法"] = _first_nonempty_cols(
        w, ["ord_ecf_payment_method_name__c", "ord_reismobj__PaymentMethod__c"]
    )
    out["受注種別"] = _first_nonempty_cols(w, ["ord_ecf_order_kind__c"])
    out["対応状況"] = _first_nonempty_cols(
        w, ["ord_ecf_order_human_state__c", "ord_ecf_order_state__c"]
    )
    out["送料"] = _first_nonempty_cols(
        w, ["ord_ecf_deliv_fee__c", "ord_reismobj__base_shipping_fee__c"]
    )
    out["決済状況"] = _first_nonempty_cols(
        w, ["ord_ecf_payment_human_state__c", "ord_ecf_pay_human_state__c"]
    )
    out["取消日"] = _first_nonempty_cols(w, ["ord_ecf_canceled_at__c"])
    out["購入商品（商品コード）"] = _first_nonempty_cols(
        w, ["li_ecf_product_number__c", "li_reismobj__CatalogItemId__c"]
    )
    out["購入商品（SKUコード）"] = _first_nonempty_cols(
        w,
        [
            "li_ecf_sku__c",
            "li_ecf_variant_sku__c",
            "ci_reismobj__StockKeepingUnit__c",
        ],
    )
    out["購入商品（商品名）"] = _first_nonempty_cols(
        w,
        ["li_ecf_product_name__c", "li_ecf_name__c", "li_CatelogItemText__c", "ci_Name"],
    )
    out["購入商品（単価）"] = _first_nonempty_cols(
        w,
        ["li_ecf_sales_price__c", "li_ecf_price__c", "li_reismobj__UnitPrice__c"],
    )
    out["購入商品（個数）"] = _first_nonempty_cols(
        w, ["li_ecf_quantity__c", "li_reismobj__Quantity__c"]
    )
    out["購入商品（商品カテゴリー）"] = _first_nonempty_cols(
        w, ["li_ecf_product_category_names__c"]
    )
    out["購入商品（原価）"] = _first_nonempty_cols(
        w, ["li_ecf_list_price__c", "dp_reismobj__ListPrice__c"]
    )
    out["購入URL"] = _first_nonempty_cols(w, ["ord_ecf_url__c"])
    out["広告URLグループ名"] = _first_nonempty_cols(w, ["ord_ecf_url_group__c"])
    out["媒体"] = _first_nonempty_cols(w, ["ord_ecf_store__c", "ord_ecf_cv_route__c"])
    out["クーポン"] = _first_nonempty_cols(w, ["ord_ecf_coupon_codes__c"])
    out["デバイス"] = _first_nonempty_cols(w, ["ord_ecf_device_variant__c"])
    out["受注経路"] = _first_nonempty_cols(w, ["ord_ecf_cv_route__c"])
    out["ecf_shipping_full_address__c"] = _first_nonempty_cols(
        w, ["ord_ecf_shipping_full_address__c"]
    )
    for c in out.columns:
        out[c] = out[c].fillna("").astype(str)
    return out.reindex(columns=list(JUKKAN_LEGACY_COLUMNS), fill_value="")


def _first_nonempty_in_df(df: pd.DataFrame, cols: list[str]) -> str:
    if df.empty:
        return ""
    for c in cols:
        if c not in df.columns:
            continue
        for v in df[c].tolist():
            s = str(v).strip() if v is not None and not (isinstance(v, float) and pd.isna(v)) else ""
            if s and s.lower() not in ("none", "nan", "false"):
                return s
    return ""


def _teiki_from_frames(frames: dict[str, pd.DataFrame], cfg: dict[str, Any]) -> pd.DataFrame:
    ord_raw = _df_get(frames, "order")
    li_raw = _df_get(frames, "order_item")
    acc_raw = _df_get(frames, "account")
    if ord_raw.empty:
        return pd.DataFrame(columns=TEIKI_LEGACY_COLUMNS)
    rt_ids = _subscription_record_type_ids(cfg)
    if not rt_ids:
        print(
            "[warn] appflow.subscription_record_type_ids が未設定のため、"
            "定期（teiki）行を出力しません（crm_s3_sources.json の appflow に subscription_record_type_ids を追加してください）"
        )
        return pd.DataFrame(columns=TEIKI_LEGACY_COLUMNS)
    if "RecordTypeId" not in ord_raw.columns:
        print("[warn] Order に RecordTypeId 列がありません。定期（teiki）をスキップします。")
        return pd.DataFrame(columns=TEIKI_LEGACY_COLUMNS)
    subs = ord_raw.loc[ord_raw["RecordTypeId"].astype(str).isin(rt_ids)].copy()
    if subs.empty:
        return pd.DataFrame(columns=TEIKI_LEGACY_COLUMNS)
    subs["_lm"] = pd.to_datetime(subs.get("LastModifiedDate"), errors="coerce")
    subs = subs.sort_values("_lm", na_position="first")
    sn = (
        subs.get("ecf_subs_order_number__c", pd.Series("", index=subs.index))
        .fillna("")
        .astype(str)
        .str.strip()
    )
    subs["_dedupe_key"] = sn.where(sn != "", subs["Id"].astype(str))
    latest = subs.groupby("_dedupe_key", as_index=False).last()
    acc_p = _pfx(acc_raw, "acc")
    o = _pfx(latest, "ord")
    base = o.merge(
        acc_p,
        left_on="ord_reismobj__Account__c",
        right_on="acc_Id",
        how="left",
    )
    rows: list[dict[str, str]] = []
    for _, orow in base.iterrows():
        oid = str(orow.get("ord_Id", "") or "").strip()
        sub_li = (
            li_raw[li_raw["reismobj__OrderId__c"].astype(str) == oid]
            if oid
            else pd.DataFrame()
        )
        sku = _first_nonempty_in_df(
            sub_li, ["ecf_sku__c", "ecf_variant_sku__c"]
        )
        pcode = _first_nonempty_in_df(sub_li, ["ecf_product_number__c"])
        pcat = _first_nonempty_in_df(sub_li, ["ecf_product_category_names__c"])
        def g(col: str) -> str:
            v = orow.get(col, "")
            if v is None or (isinstance(v, float) and pd.isna(v)):
                return ""
            return str(v).strip()

        cyc_a = g("ord_ecf_scheduled_to_be_delivered_every_x_month__c")
        cyc_b = g("ord_ecf_scheduled_to_be_delivered_on_xth_day__c")
        cyc_c = g("ord_ecf_scheduled_to_be_delivered_every_x_day__c")
        rows.append(
            {
                "顧客番号": g("ord_ecf_customer_number__c") or g("ord_ecf_number__c"),
                "メールアドレス": g("ord_ecf_customer_email__c") or g("ord_ecf_email__c"),
                "性別": g("acc_ecf_sex__c"),
                "生年月日": g("acc_ecf_birth__c"),
                "会員ランク名": g("acc_ecf_customer_rank_name__c"),
                "LINE ID": g("acc_ecf_line_id__c"),
                "更新日": g("ord_ecf_subsorder_updated_at__c") or g("ord_LastModifiedDate"),
                "顧客購入回数": g("acc_ecf_orders_count__c") or g("ord_ecf_orders_count__c"),
                "顧客タイプ名": g("acc_ecf_customer_type_name__c"),
                "顧客ID": g("ord_ecf_customer_id__c"),
                "お届け先（都道府県）": g("ord_ecf_shipping_prefecture_name__c")
                or g("ord_reismobj__ShippingState__c"),
                "お届け先（郵便番号フル）": g("ord_ecf_shipping_full_zip__c")
                or g("ord_reismobj__ShippingPostalCode__c"),
                "お届け先（住所フル）": g("ord_ecf_shipping_full_address__c")
                or g("ord_reismobj__ShippingStreet__c"),
                "お届け先（電話番号フル）": g("ord_ecf_shipping_full_tel__c")
                or g("ord_reismobj__ShippingPhone__c"),
                "お届け先（名前フル）": g("ord_ecf_shipping_full_name__c")
                or (
                    g("ord_reismobj__ShippingLastName__c")
                    + g("ord_reismobj__ShippingFirstName__c")
                ),
                "定期受注番号": g("ord_ecf_subs_order_number__c"),
                "定期受注ID": g("ord_ecf_subsorder_id__c") or g("ord_ecf_subs_order_id__c"),
                "媒体": g("ord_ecf_store__c"),
                "ステータス": g("ord_ecf_subsorder_human_state__c")
                or g("ord_ecf_subsorder_state__c"),
                "定期回数": g("ord_ecf_subsorder_times__c"),
                "配送サイクル": g("ord_ecf_scheduled_delivery_time__c"),
                "何ヶ月ごと": cyc_a,
                "何日に": cyc_b,
                "何日ごと": cyc_c,
                "支払い方法": g("ord_ecf_payment_method_name__c"),
                "広告URLグループ": g("ord_ecf_url_group__c"),
                "広告主": g("ord_ecf_advertiser__c"),
                "次回配送予定日": g("ord_ecf_subsorder_scheduled_to_be_delivered__c")
                or g("ord_ecf_scheduled_to_be_delivered_at__c"),
                "キャンセル日": g("ord_ecf_canceled_at__c"),
                "停止理由": g("ord_ecf_suspend_reasons__c"),
                "キャンセル理由": g("ord_ecf_cancel_reasons__c"),
                "購入商品（SKUコード）": sku,
                "購入商品（商品コード）": pcode,
                "購入商品（商品カテゴリー）": pcat,
                "ecf_shipping_full_address__c": g("ord_ecf_shipping_full_address__c"),
            }
        )
    return pd.DataFrame(rows, columns=list(TEIKI_LEGACY_COLUMNS))


def _df_to_csv_text(df: pd.DataFrame) -> str:
    buf = io.StringIO()
    df.to_csv(buf, index=False, encoding="utf-8-sig")
    return buf.getvalue()


def legacy_jukkan_teiki_from_appflow_ndjson(cfg: dict[str, Any]) -> tuple[str, str]:
    """
    ``crm_s3_sources.json`` の ``appflow.bucket`` から NDJSON を取得し結合、
    本店受注・定期の論理列相当の CSV テキスト（UTF-8 BOM）を返す。
    """
    frames = load_appflow_entity_frames(cfg)
    wide = _join_line_level(frames)
    juk = _jukkan_from_wide(wide)
    tei = _teiki_from_frames(frames, cfg)
    return _df_to_csv_text(juk), _df_to_csv_text(tei)
