"""CRM 入力 CSV を S3 GetObject で取得する。"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from botocore.exceptions import ClientError

from aws_session import get_boto3_session
from csv_text import decode_bytes_to_text

_default_crm_tables_bucket: str | None = None


def set_default_crm_tables_bucket(name: str) -> None:
    """build_crm_tables が data_locations.json 読込後に呼ぶ。brand_mapping 省略時の既定バケットに使う。"""
    global _default_crm_tables_bucket
    b = str(name or "").strip()
    if not b:
        raise ValueError("set_default_crm_tables_bucket: バケット名が空です")
    _default_crm_tables_bucket = b


def _fallback_brand_mapping_bucket() -> str:
    if not _default_crm_tables_bucket:
        raise RuntimeError(
            "ブランドマッピング用の既定バケットが未設定です。"
            "build_crm_tables 起動時に data_locations.json を読み "
            "set_default_crm_tables_bucket を呼び出してください。"
        )
    return _default_crm_tables_bucket


def load_s3_sources_config(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def _join_s3_key(prefix: str, object_name: str) -> str:
    p = (prefix or "").strip().strip("/")
    o = object_name.lstrip("/")
    return f"{p}/{o}" if p else o


def _brand_mapping_storage(cfg: dict[str, Any]) -> tuple[str, str, str | None] | None:
    """
    ``objects.brand_mapping`` のオブジェクト名から、(bucket, key, region) を返す。
    バケットは ``brand_mapping_bucket``、未指定時は data_locations.json の crm_tables_bucket
    （build_crm_tables が set_default_crm_tables_bucket で登録）。
    キーは ``brand_mapping_prefix`` + オブジェクト名。
    リージョンは ``brand_mapping_region``、なければ設定の ``region``。
    """
    objects = cfg.get("objects") or {}
    name = objects.get("brand_mapping")
    if name is None or str(name).strip() == "":
        return None
    mb = str(cfg.get("brand_mapping_bucket") or "").strip()
    if not mb:
        mb = _fallback_brand_mapping_bucket()
    mp = str(cfg.get("brand_mapping_prefix") or "").strip()
    key = _join_s3_key(mp, str(name).strip())
    mr = str(cfg.get("brand_mapping_region") or "").strip()
    region = mr or str(cfg.get("region") or "").strip() or None
    return (mb, key, region)


def get_object_text(bucket: str, key: str, region: str | None) -> str:
    """S3 GetObject し、本文をテキストとして返す（文字コードは csv_text と同様に自動判定）。"""
    session = get_boto3_session(region)
    client = session.client("s3")
    uri = f"s3://{bucket}/{key}"
    try:
        resp = client.get_object(Bucket=bucket, Key=key)
    except ClientError as e:
        code = (e.response or {}).get("Error", {}).get("Code", "")
        if code in ("NoSuchKey", "404"):
            raise RuntimeError(
                f"NoSuchKey: バケット内に次のオブジェクトがありません。\n"
                f"  {uri}\n"
                f"対処: crm_s3_sources.json の bucket / prefix / objects を実際のキーに合わせる。\n"
                f"キー確認例: aws s3 ls s3://{bucket}/ --recursive"
            ) from e
        raise RuntimeError(f"S3 GetObject 失敗: {uri} — {e}") from e
    raw: bytes = resp["Body"].read()
    text, _enc, _n = decode_bytes_to_text(raw, label=uri)
    return text


# build_crm_tables が Put する成果物（バックアップ対象キー末尾）
CRM_TABLE_OUTPUT_NAMES: tuple[str, ...] = (
    "01_顧客マスター.csv",
    "02_顧客住所配送先.csv",
    "03_注文明細商品.csv",
    "04_定期購入管理.csv",
    "05_解約理由マスター.csv",
    "06_販促チャネル.csv",
)


def s3_object_exists(bucket: str, key: str, region: str | None) -> bool:
    session = get_boto3_session(region)
    client = session.client("s3")
    try:
        client.head_object(Bucket=bucket, Key=key)
        return True
    except ClientError as e:
        code = (e.response or {}).get("Error", {}).get("Code", "")
        if code in ("404", "NoSuchKey", "NotFound"):
            return False
        raise


def backup_existing_crm_outputs_on_s3(
    *,
    src_bucket: str,
    out_key_prefix: str,
    region: str | None,
    dest_bucket: str,
) -> int:
    """
    ``src_bucket`` + ``out_key_prefix`` 上に既存の成果物 CSV がある場合のみ、
    ``s3://{dest_bucket}/backup/YYYY-MM-DD/HHMMSS/`` へ CopyObject する。
    返り値はコピーしたオブジェクト数。
    """
    b = str(src_bucket or "").strip()
    if not b:
        return 0
    db = str(dest_bucket or "").strip()
    if not db:
        raise ValueError("backup_existing_crm_outputs_on_s3: dest_bucket が空です")
    pfx = str(out_key_prefix or "").strip().strip("/")
    now = datetime.now()
    stamp = f"backup/{now.strftime('%Y-%m-%d')}/{now.strftime('%H%M%S')}"
    session = get_boto3_session(region)
    client = session.client("s3")
    n = 0
    for name in CRM_TABLE_OUTPUT_NAMES:
        src_key = _join_s3_key(pfx, name)
        if not s3_object_exists(b, src_key, region):
            continue
        dst_key = f"{stamp}/{name}"
        uri_src = f"s3://{b}/{src_key}"
        uri_dst = f"s3://{db}/{dst_key}"
        try:
            client.copy_object(
                Bucket=db,
                Key=dst_key,
                CopySource={"Bucket": b, "Key": src_key},
            )
        except ClientError as e:
            raise RuntimeError(
                f"S3 バックアップ CopyObject 失敗: {uri_src} -> {uri_dst} — {e}"
            ) from e
        n += 1
    if n:
        print(f"[info] S3 上の既存成果物をバックアップしました: s3://{db}/{stamp}/ ({n} 件)")
    else:
        loc = f"s3://{b}/{pfx}/" if pfx else f"s3://{b}/"
        print(f"[info] S3 バックアップ: 対象なし（{loc} に成果物 CSV が未配置）")
    return n


def get_object_text_if_exists(bucket: str, key: str, region: str | None) -> str | None:
    """GetObject し、NoSuchKey のときは None。それ以外のエラーは例外。"""
    session = get_boto3_session(region)
    client = session.client("s3")
    uri = f"s3://{bucket}/{key}"
    try:
        resp = client.get_object(Bucket=bucket, Key=key)
    except ClientError as e:
        code = (e.response or {}).get("Error", {}).get("Code", "")
        if code in ("NoSuchKey", "404"):
            return None
        raise RuntimeError(f"S3 GetObject 失敗: {uri} — {e}") from e
    raw: bytes = resp["Body"].read()
    text, _enc, _n = decode_bytes_to_text(raw, label=uri)
    return text


def put_object_json_utf8(
    bucket: str,
    key: str,
    text: str,
    region: str | None,
) -> None:
    """S3 PutObject（JSON 本文 UTF-8、BOM なし）。"""
    session = get_boto3_session(region)
    client = session.client("s3")
    uri = f"s3://{bucket}/{key}"
    try:
        client.put_object(
            Bucket=bucket,
            Key=key,
            Body=text.encode("utf-8"),
            ContentType="application/json; charset=utf-8",
        )
    except ClientError as e:
        raise RuntimeError(f"S3 PutObject 失敗: {uri} — {e}") from e


def fetch_all_crm_source_texts(cfg: dict[str, Any]) -> dict[str, str]:
    """
    設定の objects ごとに GetObject し、論理名 -> CSV テキスト を返す。
    ``brand_mapping`` は商品マッピング JSON 用のため本関数では取得せず、
    :func:`fetch_brand_mapping_text` を使用する。
    """
    bucket = str(cfg.get("bucket") or "").strip()
    prefix = str(cfg.get("prefix") or "").strip()
    region = str(cfg.get("region") or "").strip() or None
    objects = cfg.get("objects") or {}
    if not bucket:
        raise ValueError("crm_s3_sources: bucket が空です")
    appflow_on = bool(str((cfg.get("appflow") or {}).get("bucket") or "").strip())
    out: dict[str, str] = {}
    for logical, name in objects.items():
        if str(logical).startswith("_"):
            continue
        if str(logical) == "brand_mapping":
            continue
        if appflow_on and str(logical) in ("jukkan", "teiki"):
            continue
        key = _join_s3_key(prefix, str(name).strip())
        out[str(logical)] = get_object_text(bucket, key, region)
    return out


def fetch_brand_mapping_text(cfg: dict[str, Any]) -> str:
    """
    ``objects.brand_mapping`` で指定された S3 オブジェクト本文を返す。
    バケットは ``brand_mapping_bucket``、省略時は data_locations.json の crm_tables_bucket。
    未設定のときは空文字列。
    """
    loc = _brand_mapping_storage(cfg)
    if loc is None:
        return ""
    bucket, key, region = loc
    return get_object_text(bucket, key, region)


def ensure_brand_mapping_on_s3(
    cfg: dict[str, Any],
    *,
    target_brand: str,
) -> str:
    """
    ``objects.brand_mapping`` のオブジェクトが S3 にあればその本文を返す。
    **無い場合**はローカルファイルは読まず、規定スキーマの JSON を **生成して PutObject** し、同じ本文を返す。
    ``brand_mapping`` 未設定時は空文字列。
    """
    from brand_filter import _default_brand_mapping, _normalize_brand_mapping_dict

    loc = _brand_mapping_storage(cfg)
    if loc is None:
        return ""
    bucket, key, region = loc
    uri = f"s3://{bucket}/{key}"

    existing = get_object_text_if_exists(bucket, key, region)
    if existing is not None:
        return existing

    tb = str(target_brand).strip()
    base = _default_brand_mapping()
    if tb:
        base["target_brand"] = tb
        cats = [tb]
        if tb == "P3":
            cats.append("P3 Booster")
        base["category_includes_brand"] = cats

    normalized = _normalize_brand_mapping_dict(base)
    payload: dict[str, Any] = {
        "_comment": (
            "build_crm_tables が S3 に初回生成した雛形です。"
            "sku / product_code 等を商品マッピングマスタに合わせて編集してください。"
        ),
        **normalized,
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    put_object_json_utf8(bucket, key, text, region)
    print(f"[info] ブランドマッピング JSON を S3 に新規作成しました: {uri}")
    return text


def brand_mapping_s3_uri(cfg: dict[str, Any]) -> str | None:
    """ログ用: brand_mapping オブジェクトの s3 URI。未設定時は None。"""
    loc = _brand_mapping_storage(cfg)
    if loc is None:
        return None
    bucket, key, _region = loc
    return f"s3://{bucket}/{key}"


def put_object_text(
    bucket: str,
    key: str,
    text: str,
    region: str | None,
) -> None:
    """S3 PutObject（本文は UTF-8 BOM 付きバイト列として保存。Excel 互換）。"""
    session = get_boto3_session(region)
    client = session.client("s3")
    uri = f"s3://{bucket}/{key}"
    try:
        client.put_object(
            Bucket=bucket,
            Key=key,
            Body=text.encode("utf-8-sig"),
            ContentType="text/csv; charset=utf-8",
        )
    except ClientError as e:
        raise RuntimeError(f"S3 PutObject 失敗: {uri} — {e}") from e
