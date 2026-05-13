#!/usr/bin/env python3
"""
テスト実行用: build_crm_tables.py を同ディレクトリの crm_s3_sources.json 前提で起動する。

入力はすべて S3（crm_s3_sources.json の bucket / objects / appflow）。
既存の CRM 成果物バケット（data_locations.json の crm_tables_bucket）上の成果物は
CopyObject で backup/ に退避し、成果物はローカルと S3（同バケット）に出力する。

使用例:
  cd datacloud_prd
  python run_crm_local.py
  python run_crm_local.py --out-dir .\\crm_out_test
  python run_crm_local.py --no-preview
  python run_crm_local.py --crm-brand P3
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
_BUILD = _SCRIPT_DIR / "build_crm_tables.py"


def _preview_csv_dir(out_dir: Path, head_lines: int) -> None:
    """生成 CSV の先頭行を標準出力に表示（内容確認用）。"""
    files = sorted(out_dir.glob("*.csv"))
    if not files:
        print("(出力 CSV がありません)", file=sys.stderr)
        return
    for fp in files:
        print(f"\n{'=' * 60}\n# {fp.name}\n{'=' * 60}")
        try:
            raw = fp.read_text(encoding="utf-8-sig")
        except OSError as e:
            print(f"(読み取りエラー: {e})", file=sys.stderr)
            continue
        lines = raw.splitlines()
        for line in lines[:head_lines]:
            print(line)
        if len(lines) > head_lines:
            print(f"... 全 {len(lines)} 行（先頭 {head_lines} 行のみ表示）")


def main() -> int:
    p = argparse.ArgumentParser(
        description="CRM ビルドのテスト実行（S3 入力・プレビュー付き）"
    )
    p.add_argument(
        "--out-dir",
        type=Path,
        default=_SCRIPT_DIR / "crm_out_test",
        help="出力先ディレクトリ（既定: datacloud_prd/crm_out_test）",
    )
    p.add_argument(
        "--no-preview",
        action="store_true",
        help="生成後に CSV 先頭を標準出力へ出さない（既定はプレビューする）",
    )
    p.add_argument(
        "--preview-lines",
        type=int,
        default=12,
        metavar="N",
        help="プレビューで表示する行数（既定: 12）",
    )
    p.add_argument(
        "--crm-brand",
        default=None,
        metavar="BRAND",
        help="build_crm_tables に渡す --crm-brand（例: P3）",
    )
    p.add_argument(
        "--brand-mapping",
        type=Path,
        default=None,
        help="build_crm_tables の --brand-mapping（省略時はビルド側既定）",
    )
    p.add_argument(
        "--s3-config",
        type=Path,
        default=None,
        help="build_crm_tables の --s3-config（省略時はビルド側既定）",
    )
    args = p.parse_args()

    out_dir = args.out_dir.resolve()

    if not _BUILD.is_file():
        print(f"見つかりません: {_BUILD}", file=sys.stderr)
        return 2

    cmd = [
        sys.executable,
        str(_BUILD),
        "--out-dir",
        str(out_dir),
    ]
    if args.crm_brand:
        cmd.extend(["--crm-brand", str(args.crm_brand).strip()])
    if args.brand_mapping is not None:
        cmd.extend(["--brand-mapping", str(args.brand_mapping.resolve())])
    if args.s3_config is not None:
        cmd.extend(["--s3-config", str(args.s3_config.resolve())])
    print("[run_crm_local] 実行コマンド:")
    print(" ", subprocess.list2cmdline(cmd))
    print(f"[run_crm_local] 出力: {out_dir}")
    print()

    r = subprocess.run(cmd, cwd=str(_SCRIPT_DIR))
    if r.returncode != 0:
        return r.returncode

    print()
    print(f"[run_crm_local] 完了。出力: {out_dir}")
    if not args.no_preview:
        _preview_csv_dir(out_dir, max(1, args.preview_lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
