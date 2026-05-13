"""CSV/テキストファイルのバイト読み込み（エンコーディング自動判定）。"""

from __future__ import annotations

from pathlib import Path

# Shift_JIS/CP932 のあと JIS X 0213（メール本文で CP932 無効バイトになることがある）
ENCODING_CANDIDATES = (
    "utf-8-sig",
    "utf-8",
    "cp932",
    "shift_jis",
    "shift_jis_2004",
    "shift_jisx0213",
    "euc-jp",
)


def decode_bytes_to_text(raw: bytes, *, label: str = "") -> tuple[str, str, int]:
    """
    バイト列をテキストにデコードする。
    (テキスト, エンコーディングラベル, 置換文字 U+FFFD の個数)
    """
    last_err: Exception | None = None
    for enc in ENCODING_CANDIDATES:
        try:
            return raw.decode(enc), enc, 0
        except (UnicodeDecodeError, LookupError) as e:
            last_err = e
    try:
        text = raw.decode("cp932", errors="replace")
        n = text.count("\ufffd")
        return text, "cp932+replace", n
    except Exception:
        pass
    try:
        text = raw.decode("utf-8", errors="replace")
        n = text.count("\ufffd")
        return text, "utf-8+replace", n
    except Exception:
        pass
    src = label or "bytes"
    raise UnicodeDecodeError(
        "unknown", b"", 0, 1, f"Could not decode {src}: {last_err}"
    )


def read_text_bytes(path: Path) -> tuple[str, str, int]:
    """
    (テキスト, エンコーディングラベル, 置換文字 U+FFFD の個数)
    厳密デコードできない箇所は cp932/utf-8 の replace で読む。
    """
    return decode_bytes_to_text(path.read_bytes(), label=str(path))
