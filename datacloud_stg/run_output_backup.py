"""
実行直前に、出力ディレクトリ直下の既存 CSV を
S3 ``s3://<bucket>/backup/YYYY-MM-DD/HHMMSS/`` にアップロードする（ローカルへの移動はしない）。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from crm_s3 import put_object_text

_BACKUP_KEY_PREFIX = "backup"


def _collect_files_to_archive(
    out_dir: Path,
    patterns: tuple[str, ...],
) -> list[Path]:
    out_dir = out_dir.resolve()
    if not out_dir.is_dir():
        return []
    found: list[Path] = []
    for pat in patterns:
        for p in sorted(out_dir.glob(pat)):
            if p.is_file():
                found.append(p)
    seen: set[Path] = set()
    unique: list[Path] = []
    for p in found:
        rp = p.resolve()
        if rp not in seen:
            seen.add(rp)
            unique.append(p)
    return unique


def upload_existing_csvs_to_s3_backup(
    out_dir: Path,
    *,
    bucket: str,
    region: str | None = None,
    patterns: tuple[str, ...] = ("*.csv",),
) -> str | None:
    """
    ``out_dir`` 直下の既存 CSV を
    ``{bucket}/backup/YYYY-MM-DD/HHMMSS/<ファイル名>`` に PutObject する。

    対象がなければ None。失敗時は例外。
    """
    b = str(bucket or "").strip()
    if not b:
        raise ValueError("backup bucket が空です")

    out_dir = out_dir.resolve()
    files = _collect_files_to_archive(out_dir, patterns)
    if not files:
        print(
            f"[info] S3 バックアップ: 対象なし（{out_dir} に既存の *.csv がないため "
            f"s3://{b}/{_BACKUP_KEY_PREFIX}/ へは何もアップロードしません）"
        )
        return None

    now = datetime.now()
    date_part = now.strftime("%Y-%m-%d")
    time_part = now.strftime("%H%M%S")
    base_prefix = f"{_BACKUP_KEY_PREFIX}/{date_part}/{time_part}"

    for fp in files:
        key = f"{base_prefix}/{fp.name}"
        text = fp.read_text(encoding="utf-8-sig")
        put_object_text(b, key, text, region)

    uri = f"s3://{b}/{base_prefix}/"
    print(f"[info] 既存出力を S3 にバックアップしました: {uri}")
    return uri
