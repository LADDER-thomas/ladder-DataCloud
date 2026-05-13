#!/usr/bin/env python3
"""
既存 CSV（本店/定期に相当する取込ファイル・モール・TikTok・メールエクスポート等。設定で指定）を読み、
和名列で日付正規化・列フィルタを行い、ヘッダーは column_i18n.py の対応で英語（スネークケース）に変換して出力する。
Data Cloud 取り込み向けに UTF-8 / RFC 4180 準拠の CSV とし、任意で S3 へアップロードする。
アップロード先バケットの既定値は data_locations.json の datacloud_export_default_upload_bucket
（datacloud_requirements.json の s3.bucket で上書き可能）。

アクセスキーは本ディレクトリ（例: datacloud_prd）の aws_credentials.json を優先して読む（無いときは環境変数・~/.aws を利用）。
S3 へのアクセスは get_boto3_session 経由。直接 IAM ユーザーで接続する場合は、既定で
STS GetCallerIdentity が ses+ladder@thomas-gr.com ユーザーと一致するか検証する（aws_session.sts_verify_iam_user_arn）。
別 IAM ロールを引き受けたい場合は環境変数 AWS_ASSUME_ROLE_ARN（推奨）または AWS_ROLE_ARN にロール
ARN を設定する（STS AssumeRole）。OIDC（AWS_WEB_IDENTITY_TOKEN_FILE あり）のときは二重に AssumeRole しない。

列名の上書き・日付対象列は datacloud_requirements.json で調整する。
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Iterable, Iterator


def _datacloud_dir() -> Path:
    try:
        return Path(__file__).resolve().parent
    except NameError:
        return Path.cwd()


_SCRIPT_DIR = _datacloud_dir()
# 入力CSVのあるフォルダ（datacloud / datacloud_prd 等の親）。カレントがその配下のときと同等。


def _default_workspace_root(script_dir: Path) -> Path:
    n = script_dir.name
    if n == "datacloud" or n.startswith("datacloud_"):
        return script_dir.parent
    return script_dir


_DEFAULT_WORK_DIR = _default_workspace_root(_SCRIPT_DIR)

if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from aws_session import (
    get_boto3_session,
    load_aws_credentials_from_config,
    sts_verify_iam_user_arn,
    verify_aws_credential_access,
)
from column_i18n import build_english_headers
from csv_text import read_text_bytes
from data_locations import load_data_locations
from run_output_backup import upload_existing_csvs_to_s3_backup

# ---------------------------------------------------------------------------
# 日付正規化（代表的な日本語エクスポート形式 → ISO 日付または日時）
# ---------------------------------------------------------------------------

_DATE_ONLY = re.compile(r"^(\d{4})[/-](\d{1,2})[/-](\d{1,2})$")
_DATETIME_SLASH = re.compile(
    r"^(\d{4})[/-](\d{1,2})[/-](\d{1,2})\s+(\d{1,2}):(\d{1,2})(?::(\d{1,2}))?$"
)
_DATETIME_US = re.compile(
    r"^(\d{1,2})/(\d{1,2})/(\d{4})\s+(\d{1,2}):(\d{1,2}):(\d{1,2})\s*(AM|PM)\s*$",
    re.I,
)


def normalize_date_cell(value: str) -> str:
    s = (value or "").strip()
    if not s or s == '""':
        return ""

    m = _DATE_ONLY.match(s)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return f"{y:04d}-{mo:02d}-{d:02d}"

    m = _DATETIME_SLASH.match(s)
    if m:
        y, mo, d = (
            int(m.group(1)),
            int(m.group(2)),
            int(m.group(3)),
        )
        hh, mm = int(m.group(4)), int(m.group(5))
        ss = int(m.group(6) or 0)
        return f"{y:04d}-{mo:02d}-{d:02d}T{hh:02d}:{mm:02d}:{ss:02d}"

    m = _DATETIME_US.match(s)
    if m:
        mo_, d_, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        hh, mm, ss = int(m.group(4)), int(m.group(5)), int(m.group(6))
        ap = m.group(7).upper()
        if ap == "PM" and hh != 12:
            hh += 12
        if ap == "AM" and hh == 12:
            hh = 0
        return f"{y:04d}-{mo_:02d}-{d_:02d}T{hh:02d}:{mm:02d}:{ss:02d}"

    return s


def should_normalize_column(name: str, date_columns: Iterable[str]) -> bool:
    """日付変換対象は設定 date_columns_iso の列名のみ（誤変換防止）。"""
    return name in set(date_columns)


# ---------------------------------------------------------------------------
# CSV 読み書き
# ---------------------------------------------------------------------------

def iter_csv_rows(
    text: str, dialect: str = "excel"
) -> tuple[list[str], Iterator[list[str]]]:
    """全テキストを一度にパース（メール本文など複数行フィールドに対応）。"""
    stream = io.StringIO(text)
    reader = csv.reader(stream, dialect=dialect)
    rows = iter(reader)
    try:
        header = next(rows)
    except StopIteration:
        return [], iter(())
    return header, rows


def write_csv_utf8(
    out_path: Path,
    header: list[str],
    rows: Iterable[list[str]],
) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(
            f,
            dialect="excel",
            quoting=csv.QUOTE_MINIMAL,
            lineterminator="\n",
        )
        w.writerow(header)
        for row in rows:
            w.writerow(row)


def filter_columns_by_japanese_names(
    header: list[str],
    rows: Iterator[list[str]],
    columns: list[str] | None,
) -> tuple[list[str], Iterator[list[str]]]:
    """列絞り込みは元CSVの和名列名で指定。"""
    if not columns:
        return header, rows
    want = set(columns)
    pick = [i for i, h in enumerate(header) if h in want]

    def _gen() -> Iterator[list[str]]:
        for row in rows:
            yield [row[i] if i < len(row) else "" for i in pick]

    return [header[i] for i in pick], _gen()


def normalize_rows_dates(
    header: list[str],
    rows: Iterator[list[str]],
    date_columns: list[str],
) -> Iterator[list[str]]:
    norm_cols = {
        i for i, h in enumerate(header) if should_normalize_column(h, date_columns)
    }

    def _gen() -> Iterator[list[str]]:
        for row in rows:
            out = []
            for i, cell in enumerate(row):
                if i in norm_cols:
                    out.append(normalize_date_cell(cell))
                else:
                    out.append(cell)
            yield out

    return _gen()


def process_source(
    cfg: dict[str, Any],
    source: dict[str, Any],
    work_dir: Path,
    out_dir: Path,
    dry_run: bool,
) -> Path | None:
    rel = source.get("input_file")
    if not rel:
        print(f"[skip] source without input_file: {source.get('id')}", file=sys.stderr)
        return None
    in_path = (work_dir / rel).resolve()
    if not in_path.is_file():
        print(f"[warn] missing file: {in_path}", file=sys.stderr)
        return None

    text, enc, replace_count = read_text_bytes(in_path)
    if enc.endswith("+replace") and replace_count > 0:
        # 少数の壊れバイトはよくあるため warn は多いときだけ
        if replace_count > 50:
            print(
                f"[warn] {in_path.name}: {enc} — {replace_count} replacement chars (U+FFFD); "
                "consider re-exporting the source file as UTF-8.",
                file=sys.stderr,
            )
        elif replace_count > 5:
            print(
                f"[info] {in_path.name}: {enc} — {replace_count} replacement chars (minor garbled bytes in file).",
                file=sys.stderr,
            )
        # 1〜5 個は実務上問題になりにくいのでログ省略
    dialect = source.get("dialect", "excel")
    header, rows = iter_csv_rows(text, dialect=dialect)
    if not header:
        print(f"[warn] empty csv: {in_path}", file=sys.stderr)
        return None

    date_cols = list(cfg.get("output", {}).get("date_columns_iso", []))
    rows = normalize_rows_dates(header, rows, date_cols)

    columns = source.get("columns")
    header, rows = filter_columns_by_japanese_names(header, rows, columns)

    source_id = str(source.get("id") or "default")
    rename_overrides: dict[str, str] = dict(source.get("column_rename") or {})
    header_en = build_english_headers(header, source_id, rename_overrides)

    out_name = source.get("output_key") or f"{source.get('id', 'export')}.csv"
    out_path = out_dir / out_name
    write_csv_utf8(out_path, header_en, rows)
    print(f"[ok] {in_path.name} ({enc}) -> {out_path} ({len(header_en)} cols, English headers)")
    return out_path


def upload_s3(
    local_path: Path,
    bucket: str,
    key: str,
    region: str,
) -> None:
    from botocore.exceptions import BotoCoreError, ClientError

    client = get_boto3_session(region).client("s3")
    try:
        client.upload_file(str(local_path), bucket, key)
        print(f"[s3] s3://{bucket}/{key}")
    except (ClientError, BotoCoreError) as e:
        print(f"[s3 error] {e}", file=sys.stderr)
        raise


def load_config(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Data Cloud 向け CSV 正規化と S3 アップロード"
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=_SCRIPT_DIR / "datacloud_requirements.json",
        help="要件・入出力定義 JSON（無ければ sample をコピー）",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=_SCRIPT_DIR / "out",
        help="ローカル出力ディレクトリ",
    )
    parser.add_argument(
        "--work-dir",
        type=Path,
        default=_DEFAULT_WORK_DIR,
        help="input_file の相対パスの基準ディレクトリ（既定: datacloud_prd の親＝共有データフォルダ）",
    )
    parser.add_argument("--upload-s3", action="store_true", help="S3 にアップロードする")
    parser.add_argument(
        "--dry-run", action="store_true", help="設定とパスの確認のみ"
    )
    parser.add_argument(
        "--verify-credentials",
        action="store_true",
        help="credential の疎通検証のみ（STS、設定のバケット名があれば S3 は soft チェック）",
    )
    # Jupyter はカーネル接続用に sys.argv に「-f kernel-xxxx.json」を付けるため、未知引数は無視する
    args, _ignored = parser.parse_known_args()

    if args.verify_credentials:
        region = os.environ.get("AWS_DEFAULT_REGION", "ap-northeast-1")
        test_bucket = os.environ.get("DATACLOUD_S3_VERIFY_BUCKET", "").strip() or None
        cfg_path = args.config
        if cfg_path.is_file():
            try:
                cfg_v = load_config(cfg_path)
                s3cfg_v = cfg_v.get("s3") or {}
                region = s3cfg_v.get("region", region)
                if not test_bucket:
                    b = str(s3cfg_v.get("bucket", "") or "").strip()
                    test_bucket = b or None
            except (OSError, json.JSONDecodeError):
                pass
        if not test_bucket:
            try:
                dl_v = load_data_locations(_SCRIPT_DIR)
                test_bucket = (
                    str(dl_v["datacloud_export_default_upload_bucket"]).strip() or None
                )
            except (FileNotFoundError, ValueError, OSError, json.JSONDecodeError):
                pass
        try:
            r = verify_aws_credential_access(
                region,
                test_s3_bucket=test_bucket,
                s3_soft_check=bool(test_bucket),
            )
        except Exception as e:
            print(f"[ng] {e}", file=sys.stderr)
            return 1
        print("[ok] credential verification")
        print(f"  Arn: {r.get('Arn', '')}")
        print(f"  Account: {r.get('Account', '')}")
        print(f"  credentials_source: {r.get('credentials_source', '')}")
        if test_bucket and "s3_ok" in r:
            if r.get("s3_ok"):
                print(f"[ok] S3 HeadBucket s3://{test_bucket}/")
            else:
                print(
                    f"[warn] S3 HeadBucket: {r.get('s3_error', '')}",
                    file=sys.stderr,
                )
        return 0

    cfg_path = args.config
    if not cfg_path.is_file():
        sample = _SCRIPT_DIR / "datacloud_requirements.sample.json"
        print(
            f"設定が見つかりません: {cfg_path}\n"
            f"次をコピーして編集してください: {sample}",
            file=sys.stderr,
        )
        return 2

    cfg = load_config(cfg_path)
    try:
        dl = load_data_locations(_SCRIPT_DIR)
    except (FileNotFoundError, ValueError, OSError, json.JSONDecodeError) as e:
        print(f"[config error] data_locations: {e}", file=sys.stderr)
        return 2

    sources = cfg.get("sources") or []
    s3cfg = cfg.get("s3") or {}
    def_upload = str(dl["datacloud_export_default_upload_bucket"]).strip()
    bucket = str(s3cfg.get("bucket", "") or "").strip() or def_upload
    prefix = (s3cfg.get("prefix") or "").strip("/")
    region = s3cfg.get("region", os.environ.get("AWS_DEFAULT_REGION", "ap-northeast-1"))

    work_dir = args.work_dir.resolve()
    out_dir = args.out_dir.resolve()

    if args.dry_run:
        print(f"work_dir={work_dir}")
        print(f"out_dir={out_dir}")
        print(f"s3_bucket={bucket} prefix={prefix}/ region={region}")
        _assume = os.environ.get("AWS_ASSUME_ROLE_ARN", "").strip() or os.environ.get(
            "AWS_ROLE_ARN", ""
        ).strip()
        if _assume and not os.environ.get("AWS_WEB_IDENTITY_TOKEN_FILE", "").strip():
            print(f"sts_assume_role_arn={_assume}")
        else:
            print("sts_assume_role_arn=(default credential chain)")
        _vu = sts_verify_iam_user_arn()
        print(
            f"iam_user_sts_verify={_vu if _vu else 'off'}"
        )
        _cfg = load_aws_credentials_from_config()
        print(
            "aws_credentials_source="
            + ("aws_credentials.json" if _cfg else "environment_or_shared_credentials")
        )
        print(f"sources={len(sources)}")
        return 0

    out_dir.mkdir(parents=True, exist_ok=True)
    backup_region = str(region).strip() if region else None
    crm_tbl = str(dl["crm_tables_bucket"]).strip()
    try:
        upload_existing_csvs_to_s3_backup(
            out_dir,
            bucket=crm_tbl,
            region=backup_region,
            patterns=("*.csv",),
        )
    except Exception as e:
        print(f"[error] S3 バックアップ失敗: {e}", file=sys.stderr)
        return 1

    produced: list[tuple[Path, str]] = []

    for src in sources:
        out_path = process_source(cfg, src, work_dir, out_dir, args.dry_run)
        if out_path:
            produced.append((out_path, src.get("output_key", out_path.name)))

    if args.upload_s3 and produced:
        for local_path, out_key in produced:
            key = f"{prefix}/{out_key}" if prefix else out_key
            upload_s3(local_path, bucket, key, region)

    return 0


if __name__ == "__main__":
    # Jupyter では SystemExit(0) も「例外」として表示されるため、成功時は送出しない
    _exit_code = main()
    if _exit_code:
        raise SystemExit(_exit_code)
