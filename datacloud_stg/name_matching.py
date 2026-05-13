"""
Data Cloud 要件「名寄せルール」に基づく処理。

顧客:
  第1優先: メールアドレス
  第2優先: 電話番号（メールで結べない／複数メールの補助）
  第3優先: 姓名相当 + 郵便番号

SKU（商品）:
  商品名の一致、または sku_product_name_mapping.json による別名→正規名マッピング
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

import pandas as pd

# ---------------------------------------------------------------------------
# 正規化
# ---------------------------------------------------------------------------

_WS = re.compile(r"[\s\u3000]+")


def normalize_email(value: object) -> str:
    s = str(value or "").strip().lower()
    if not s or "@" not in s:
        return ""
    return s


def normalize_phone_jp(value: object) -> str:
    """国内向け: 数字のみ 10〜11 桁を主キーにする。"""
    s = str(value or "")
    digits = re.sub(r"\D", "", s)
    if not digits:
        return ""
    if digits.startswith("81") and len(digits) >= 12:
        digits = digits[2:]
    if digits.startswith("0"):
        pass
    elif len(digits) == 10:
        digits = "0" + digits
    if 10 <= len(digits) <= 11:
        return digits
    return ""


def normalize_postal_jp(value: object) -> str:
    d = re.sub(r"\D", "", str(value or ""))
    if len(d) >= 7:
        return d[:7]
    return d if len(d) == 7 else ""


def normalize_name_key(value: object) -> str:
    s = str(value or "").strip()
    if not s:
        return ""
    s = _WS.sub("", s)
    return s


def name_postal_key(name: object, postal: object) -> str:
    nk = normalize_name_key(name)
    pk = normalize_postal_jp(postal)
    if nk and len(pk) == 7:
        return f"{pk}|{nk}"
    return ""


# ---------------------------------------------------------------------------
# Union–Find
# ---------------------------------------------------------------------------


class UnionFind:
    def __init__(self) -> None:
        self._p: dict[str, str] = {}

    def find(self, x: str) -> str:
        if x not in self._p:
            self._p[x] = x
        if self._p[x] != x:
            self._p[x] = self.find(self._p[x])
        return self._p[x]

    def union(self, a: str, b: str) -> None:
        if not a or not b:
            return
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self._p[rb] = ra


# ---------------------------------------------------------------------------
# 顧客 ID 名寄せ
# ---------------------------------------------------------------------------


def _is_synthetic_customer_id(cid: str) -> bool:
    return cid.startswith(("MALL_", "TIKTOK_", "MAIL_"))


def pick_canonical_customer_id(ids: list[str]) -> str:
    """同一人物クラスタの代表 ID。EC 顧客番号（非合成）を優先。"""
    uniq = sorted({str(x) for x in ids if str(x).strip()})
    if not uniq:
        return ""
    real = [x for x in uniq if not _is_synthetic_customer_id(x)]
    if real:
        return min(real)
    return min(uniq)


def _iter_identity_rows(
    jukkan: pd.DataFrame,
    teiki: pd.DataFrame,
    mall: pd.DataFrame,
    tiktok: pd.DataFrame,
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []

    if not jukkan.empty and "顧客番号" in jukkan.columns:
        for _, r in jukkan.iterrows():
            cid = str(r.get("顧客番号", "") or "").strip()
            if not cid:
                continue
            em = normalize_email(r.get("メールアドレス", ""))
            ph_b = normalize_phone_jp(r.get("請求先（電話番号フル）", ""))
            ph_d = normalize_phone_jp(r.get("お届け先（電話番号フル）", ""))
            ph = ph_b or ph_d
            nk = name_postal_key(
                r.get("請求先（名前フル）", ""),
                r.get("請求先（郵便番号フル）", ""),
            )
            if not nk:
                nk = name_postal_key(
                    r.get("お届け先（名前フル）", ""),
                    r.get("お届け先（郵便番号フル）", ""),
                )
            rows.append(
                {"id": cid, "email": em, "phone": ph, "name_postal": nk}
            )

    if not teiki.empty and "顧客番号" in teiki.columns:
        for _, r in teiki.iterrows():
            cid = str(r.get("顧客番号", "") or "").strip()
            if not cid:
                continue
            em = normalize_email(r.get("メールアドレス", ""))
            ph = normalize_phone_jp(r.get("お届け先（電話番号フル）", ""))
            nk = name_postal_key(
                r.get("お届け先（名前フル）", ""),
                r.get("お届け先（郵便番号フル）", ""),
            )
            rows.append(
                {"id": cid, "email": em, "phone": ph, "name_postal": nk}
            )

    if not mall.empty and "注文ID" in mall.columns:
        for _, r in mall.iterrows():
            oid = str(r.get("注文ID", "") or "").strip()
            if not oid:
                continue
            cid = f"MALL_{oid}"
            ph = normalize_phone_jp(r.get("注文者電話番号", ""))
            nk = name_postal_key(
                r.get("注文者氏名", ""),
                r.get("注文者郵便番号", ""),
            )
            rows.append(
                {"id": cid, "email": "", "phone": ph, "name_postal": nk}
            )

    if not tiktok.empty and "注文ID" in tiktok.columns:
        for _, r in tiktok.iterrows():
            oid = str(r.get("注文ID", "") or "").strip()
            if not oid:
                continue
            cid = f"TIKTOK_{oid}"
            ph = normalize_phone_jp(r.get("電話番号", ""))
            nm = str(r.get("受取人", "") or "").strip() or (
                str(r.get("姓", "") or "").strip() + str(r.get("名", "") or "").strip()
            )
            nk = name_postal_key(nm, r.get("郵便番号", ""))
            rows.append(
                {"id": cid, "email": "", "phone": ph, "name_postal": nk}
            )

    return rows


def _merge_buckets(uf: UnionFind, key_to_ids: dict[str, list[str]]) -> None:
    for _k, ids in key_to_ids.items():
        ids = [x for x in ids if x]
        if len(ids) < 2:
            continue
        base = ids[0]
        for other in ids[1:]:
            uf.union(base, other)


def build_customer_canonical_id_mapping(
    jukkan: pd.DataFrame,
    teiki: pd.DataFrame,
    mall: pd.DataFrame,
    tiktok: pd.DataFrame,
    customer_master: pd.DataFrame,
) -> dict[str, str]:
    """
    受注・定期・モール・TikTok および顧客マスター行から同一人物クラスタを構築し、
    旧顧客番号 -> 代表顧客番号 の対応を返す。
    """
    uf = UnionFind()
    rows = _iter_identity_rows(jukkan, teiki, mall, tiktok)

    # マスター上の MAIL_* など（メールで EC と結べる）
    if not customer_master.empty and "顧客番号" in customer_master.columns:
        for _, r in customer_master.iterrows():
            cid = str(r.get("顧客番号", "") or "").strip()
            if not cid:
                continue
            em = normalize_email(r.get("メールアドレス", ""))
            if em:
                rows.append({"id": cid, "email": em, "phone": "", "name_postal": ""})

    by_email: dict[str, list[str]] = defaultdict(list)
    by_phone: dict[str, list[str]] = defaultdict(list)
    by_np: dict[str, list[str]] = defaultdict(list)

    for rec in rows:
        cid = rec["id"]
        if rec["email"]:
            by_email[rec["email"]].append(cid)
        if rec["phone"]:
            by_phone[rec["phone"]].append(cid)
        if rec["name_postal"]:
            by_np[rec["name_postal"]].append(cid)

    # 優先度は「同一キー内は同一人」として、メール / 電話 / 氏名+郵便の順でバケット結合
    # （異なるキー種別間は、共通 ID がいれば union で伝播）
    _merge_buckets(uf, by_email)
    _merge_buckets(uf, by_phone)
    _merge_buckets(uf, by_np)

    # クラスタごとに代表 ID
    components: dict[str, list[str]] = defaultdict(list)
    seen_ids: set[str] = set()
    for rec in rows:
        seen_ids.add(rec["id"])
    if not customer_master.empty and "顧客番号" in customer_master.columns:
        for v in customer_master["顧客番号"].astype(str):
            v = v.strip()
            if v:
                seen_ids.add(v)

    for cid in seen_ids:
        root = uf.find(cid)
        components[root].append(cid)

    mapping: dict[str, str] = {}
    for _root, ids in components.items():
        uniq_ids = sorted(set(ids))
        canon = pick_canonical_customer_id(uniq_ids)
        for old in uniq_ids:
            mapping[old] = canon

    # 自分自身
    for cid in seen_ids:
        mapping.setdefault(cid, cid)

    return mapping


def remap_customer_id_series(s: pd.Series, mapping: dict[str, str]) -> pd.Series:
    def _m(x: object) -> str:
        k = str(x or "").strip()
        return mapping.get(k, k) if k else ""

    return s.map(lambda x: _m(x))


def load_sku_product_name_mapping(path: Path) -> dict[str, str]:
    """商品名（完全一致キー）-> 名寄せ後の正規名。ファイルが無ければ空。"""
    if not path.is_file():
        return {}
    try:
        with path.open(encoding="utf-8") as f:
            raw: Any = json.load(f)
        if not isinstance(raw, dict):
            return {}
        out: dict[str, str] = {}
        for k, v in raw.items():
            ks = str(k).strip()
            if ks.startswith("_"):
                continue
            vs = str(v).strip()
            if ks and vs:
                out[ks] = vs
        return out
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return {}


def attach_canonical_product_names(
    df: pd.DataFrame, mapping: dict[str, str]
) -> pd.DataFrame:
    """論理列「商品名_名寄せ」を追加（マッピングに無い場合は商品名と同じ）。"""

    def _one(name: object) -> str:
        s = str(name or "").strip()
        if not s:
            return ""
        return mapping.get(s, s)

    out = df.copy()
    if "商品名" not in out.columns:
        out["商品名_名寄せ"] = ""
        return out
    out["商品名_名寄せ"] = out["商品名"].map(_one)
    return out
