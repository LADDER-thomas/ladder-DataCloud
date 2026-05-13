"""
日本語CSVヘッダー → 英語スネークケース（Data Cloud 向け）。
和名の意味に合わせ、ソース内で一意になるよう調整する。
"""

from __future__ import annotations

import re
# --- 内側ラベル（括弧内）の共通対応 ---
_INNER = {
    "名前フル": "name_full",
    "カナフル": "kana_full",
    "郵便番号フル": "postal_code_full",
    "住所フル": "address_full",
    "電話番号フル": "phone_full",
    "FAXフル": "fax_full",
    "名前1": "first_name",
    "名前2": "last_name",
    "カナ1": "kana_1",
    "カナ2": "kana_2",
    "郵便番号1": "postal_code_1",
    "郵便番号2": "postal_code_2",
    "都道府県": "prefecture",
    "住所1": "address_line_1",
    "住所2": "address_line_2",
    "住所3": "address_line_3",
    "電話番号1": "phone_1",
    "電話番号2": "phone_2",
    "電話番号3": "phone_3",
    "FAX1": "fax_1",
    "FAX2": "fax_2",
    "FAX3": "fax_3",
    "住所フル空白あり": "address_full_spaced",
    "郵便番号フル・ハイフンあり": "postal_code_hyphenated",
    "電話番号フル・ハイフンあり": "phone_hyphenated",
    "FAXフル・ハイフンあり": "fax_hyphenated",
}

_PURCHASE_INNER = {
    "商品名": "product_name",
    "商品名：印": "product_name_stamp",
    "SKUコード": "sku_code",
    "個数": "quantity",
    "通常価格": "list_price",
    "単価": "unit_price",
    "商品コード": "product_code",
    "販売価格": "sale_price",
    "商品カテゴリー": "category",
    "メーカー": "manufacturer",
    "原価": "cost",
}


def _uniq(names: list[str]) -> list[str]:
    seen: dict[str, int] = {}
    out = []
    for n in names:
        base = n
        if base not in seen:
            seen[base] = 1
            out.append(base)
        else:
            seen[base] += 1
            out.append(f"{base}_{seen[base]}")
    return out


def _bracket_parts(s: str) -> tuple[str, str] | None:
    m = re.match(r"^(.+)[（(](.+)[）)]$", s)
    if not m:
        return None
    return m.group(1).strip(), m.group(2).strip()


def japanese_header_to_english(name: str, _source_id: str = "") -> str:
    """1列分の英名（ソース内で別途一意化）。_source_id は将来ソース別に分岐する場合に使用。"""
    s = name.strip()
    if not s:
        return "empty_column"

    # 完全一致（製品名・記号混じり）
    exact: dict[str, str] = {
        "受注ID": "order_id",
        "受注番号": "order_number",
        "受注ラベル": "order_label",
        "顧客番号": "customer_code",
        "購入URL": "purchase_url",
        "購入オファー": "purchase_offer",
        "定期受注番号": "subscription_order_number",
        "定期受注ラベル": "subscription_order_label",
        "対応状況": "handling_status",
        "メールアドレス": "email",
        "小計": "subtotal",
        "小計8%": "subtotal_tax_8pct",
        "小計10%": "subtotal_tax_10pct",
        "送料": "shipping_fee",
        "手数料": "service_fee",
        "消費税": "consumption_tax",
        "消費税8%": "consumption_tax_8pct",
        "消費税10%": "consumption_tax_10pct",
        "合計": "total",
        "合計8%": "total_tax_8pct",
        "合計10%": "total_tax_10pct",
        "支払い合計": "payment_total",
        "配送業者": "carrier",
        "配送伝票番号": "tracking_number",
        "作成日": "created_at",
        "更新日": "updated_at",
        "受注日": "ordered_at",
        "発送予定日": "scheduled_ship_date",
        "配送予定日": "scheduled_delivery_date",
        "発送日": "shipped_date",
        "配送日": "delivered_date",
        "お届け時間": "delivery_time_slot",
        "支払い方法": "payment_method",
        "受注種別": "order_type",
        "定期内受注（N番目）": "subscription_order_sequence",
        "お届け時間コード": "delivery_time_code",
        "メモ": "memo",
        "商品ラベル": "product_label",
        "ラッピング": "gift_wrapping",
        "通信欄": "message",
        "決済状況": "payment_status",
        "初回督促日": "first_reminder_date",
        "最終督促日": "last_reminder_date",
        "督促回数": "reminder_count",
        "支払期限": "payment_due_date",
        "仮売上日": "provisional_sales_date",
        "実売上日": "confirmed_sales_date",
        "入金日": "deposit_date",
        "取消日": "cancellation_date",
        "取引ID": "transaction_id",
        "取引パスワード": "transaction_password",
        "広告URLグループ名": "ad_url_group_name",
        "広告主名": "advertiser_name",
        "次回配送予定日": "next_scheduled_delivery_date",
        "性別": "gender",
        "生年月日": "birth_date",
        "会員ランク名": "membership_rank_name",
        "割引(ポイント含む)": "discount_incl_points",
        "割引8%(ポイント含む)": "discount_8pct_incl_points",
        "割引10%(ポイント含む)": "discount_10pct_incl_points",
        "割引": "discount",
        "割引8%": "discount_8pct",
        "割引10%": "discount_10pct",
        "利用ポイント": "points_used",
        "利用ポイント8%": "points_used_8pct",
        "利用ポイント10%": "points_used_10pct",
        "付与ポイント": "points_granted",
        "合計ポイント": "points_total",
        "ポイントの有効期限": "points_expiration",
        "顧客購入回数": "customer_purchase_count",
        "次回支払い方法": "next_payment_method",
        "次回お届け時間": "next_delivery_time",
        "次回発送予定日": "next_scheduled_ship_date",
        "前回発送予定日": "previous_scheduled_ship_date",
        "前回配送予定日": "previous_scheduled_delivery_date",
        "媒体": "channel",
        "顧客タイプ名": "platform_last_bought",
        "商品合計金額": "product_total_amount",
        "同梱物": "bundled_items",
        "その他": "other",
        "調整金額": "adjustment_amount",
        "定期受注メモ": "subscription_order_memo",
        "要対応受注": "order_needs_attention",
        "要対応理由": "order_attention_reason",
        "決済番号": "payment_number",
        "定期回数": "subscription_cycle_count",
        "発送回数": "ship_count",
        "出荷リスト": "picking_list",
        "出荷リスト出力日": "picking_list_exported_at",
        "最新の決済履歴（エラー内容）": "latest_payment_history_error",
        "配送サイクル": "delivery_cycle",
        "配送サイクルの固定": "delivery_cycle_fixed",
        "何ヶ月ごと": "every_n_months",
        "何日に": "on_day_of_month",
        "何日ごと": "every_n_days",
        "定期ステータス": "subscription_status",
        "お約束回数（定期）": "subscription_promised_count",
        "残りお約束回数（定期）": "subscription_promised_remaining",
        "顧客メモ": "customer_memo",
        "お問い合わせ履歴": "inquiry_history",
        "クーポン": "coupon",
        "LINE ID": "line_id",
        "ソーシャルPLUS ID": "social_plus_id",
        "顧客ID": "customer_id",
        "顧客ラベル": "customer_label",
        "IPアドレス": "ip_address",
        "デバイス": "device",
        "配送業者コード": "carrier_code",
        "定期最新受注フラグ": "is_latest_subscription_order",
        "受注備考1": "order_note_1",
        "受注備考2": "order_note_2",
        "定期受注備考1": "subscription_note_1",
        "定期受注備考2": "subscription_note_2",
        "招待コード": "invite_code",
        "定期設定による販売価格割引": "subscription_sale_discount",
        "付与予定ポイント": "points_to_grant",
        "at score 与信結果": "at_score_credit_result",
        "O-PLUX 与信結果": "o_plux_credit_result",
        "連携用受注番号": "linked_order_number",
        "リファラ": "referrer",
        "O-PLUX 判定理由": "o_plux_reason",
        "受注経路": "order_route",
        "apps 経路": "apps_route",
        "【ポイント適応箇所課税後】8%対象小計(税抜)": "point_adj_subtotal_8pct_ex_tax",
        "【ポイント適応箇所課税後】8%対象小計にかかる消費税": "point_adj_tax_on_subtotal_8pct",
        "【ポイント適応箇所課税後】10%対象小計(税抜)": "point_adj_subtotal_10pct_ex_tax",
        "【ポイント適応箇所課税後】10%対象小計にかかる消費税": "point_adj_tax_on_subtotal_10pct",
        "Spider AF 審査結果": "spider_af_review_result",
        "Spider AF 判定理由": "spider_af_reason",
        "Spider AF コンバージョンID": "spider_af_conversion_id",
        "規格": "spec",
        "広告コード": "ad_code",
        "受取場所 第1希望": "pickup_location_preference_1",
        "受取場所 第2希望": "pickup_location_preference_2",
        "チャイム": "doorbell",
        "在庫ロケーション": "inventory_location",
        "ASUKA 審査結果": "asuka_review_result",
        # 定期受注
        "定期受注ID": "subscription_order_id",
        "定期内受注数": "orders_in_subscription_count",
        "ステータス": "status",
        "停止理由": "suspend_reason",
        "キャンセル理由": "cancel_reason",
        "キャンセル日": "canceled_at",
        "削除日": "deleted_at",
        "何回目の曜日": "nth_weekday",
        "何曜日": "weekday",
        "自動受注作成件数": "auto_order_create_count",
        "支払い回数": "payment_installments",
        "停止日": "suspended_at",
        "要対応定期受注": "subscription_needs_attention",
        "要対応理由（定期）": "subscription_attention_reason",
        "受注メモ": "order_memo",
        "停止からの経過日数": "days_since_suspend",
        "キャンセルからの経過日数": "days_since_cancel",
        "広告主": "advertiser",
        "広告URLグループ": "ad_url_group",
        "販売URL": "sales_url",
        "連携用定期受注番号": "linked_subscription_order_number",
        # モール
        "モール名": "mall_name",
        "ショップID": "shop_id",
        "ショップ名": "shop_name",
        "モール注文番号": "mall_order_number",
        "モール注文日時": "mall_ordered_at",
        "注文取込日時": "order_imported_at",
        "注文ステータス": "order_status",
        "決済方法区分": "payment_method_type",
        "注文者氏名": "orderer_name",
        "注文者郵便番号": "orderer_postal_code",
        "都道府県": "prefecture",
        "市区町村": "city",
        "町名・番地以降": "street_address",
        "注文者電話番号": "orderer_phone",
        "注文備考": "order_note",
        "配送料合計": "shipping_total",
        "決済手数料合計": "payment_fee_total",
        "ラッピング手数料合計": "wrapping_fee_total",
        "消費税合計": "tax_total",
        "ポイント利用合計": "points_used_total",
        "クーポン利用合計": "coupon_used_total",
        "出荷ステータス": "fulfillment_status",
        "保留状態": "hold_status",
        "倉庫": "warehouse",
        "送付先氏名": "ship_to_name",
        "送付先郵便番号": "ship_to_postal_code",
        "送付先都道府県": "ship_to_prefecture",
        "送付先市区町村": "ship_to_city",
        "送付先町名・番地以降": "ship_to_street",
        "送付先電話番号": "ship_to_phone",
        "配送方法": "shipping_method",
        "お届け指定日": "requested_delivery_date",
        "お届け指定時間帯": "requested_delivery_time_slot",
        "配送備考": "shipping_note",
        "倉庫備考": "warehouse_note",
        "ギフト備考": "gift_note",
        "出荷依頼番号": "ship_request_number",
        "出荷依頼日時": "ship_requested_at",
        "配送会社": "shipping_company",
        "SKUコード": "sku_code",
        "商品ID": "product_id",
        "商品名": "product_name",
        "注文個数": "order_quantity",
        "商品単価": "unit_price",
        "税率": "tax_rate",
        "消費税": "consumption_tax",
        "税込別": "tax_included_flag",
        "配送サービス種別": "delivery_service_type",
        "セット区分": "set_type",
        "セット構成品商品ID": "set_component_product_id",
        "セット構成品名": "set_component_name",
        "構成品数": "component_quantity",
        "置き配場所": "drop_off_location",
        # TikTok
        "注文状況": "order_status",
        "注文のサブ状況": "order_sub_status",
        "キャンセル/返品のタイプ": "cancel_return_type",
        "通常の注文または予約注文": "order_or_preorder",
        "SKU ID": "sku_id",
        "セラーSKU": "seller_sku",
        "バリエーション": "variation",
        "数量": "quantity",
        "返品済みのSKU数": "returned_sku_qty",
        "SKUの元の価格": "sku_original_price",
        "SKU小計（割引前）": "sku_subtotal_before_discount",
        "プラットフォームが資金提供を行うSKU割引": "platform_sku_discount",
        "セラーSKU割引": "seller_sku_discount",
        "SKU小計（割引後）": "sku_subtotal_after_discount",
        "割引後の送料": "shipping_after_discount",
        "元の送料": "original_shipping",
        "セラー送料割引": "seller_shipping_discount",
        "プラットフォーム送料割引": "platform_shipping_discount",
        "プラットフォーム割引": "platform_discount",
        "注文金額": "order_amount",
        "注文の返金額": "refund_amount",
        "注文作成時刻": "order_created_at",
        "注文の支払い日時": "order_paid_at",
        "発送準備完了日時": "ready_to_ship_at",
        "発送日時": "shipped_at",
        "配達日時": "delivered_at",
        "キャンセル日時": "canceled_at",
        "キャンセル元：": "canceled_by",
        "キャンセル理由": "cancel_reason",
        "フルフィルメントタイプ": "fulfillment_type",
        "倉庫名": "warehouse_name",
        "追跡ID": "tracking_id",
        "配達オプションのタイプ": "delivery_option_type",
        "配送業者名": "carrier_name",
        "カスタマーのユーザー名": "customer_username",
        "受取人": "recipient_name",
        "名": "first_name",
        "姓": "last_name",
        "国": "country",
        "郵便番号": "postal_code",
        "市区町村": "city",
        "町名": "town",
        "詳細住所1": "address_detail_1",
        "詳細住所2": "address_detail_2",
        "電話番号": "phone",
        "重量（kg）": "weight_kg",
        "商品カテゴリー": "product_category",
        "荷物ID": "package_id",
        "セラーメモ": "seller_memo",
        "配送先情報": "shipping_address_text",
        "統合リスト": "combined_list",
        "カスタマーからのメッセージ": "customer_message",
        # メール
        "メールID": "mail_id",
        "メールID枝番": "mail_id_branch",
        "From名前": "from_name",
        "Fromアドレス": "from_address",
        "To名前": "to_name",
        "Toアドレス": "to_address",
        "件名": "subject",
        "本文": "body",
        "応対内容(電話応対)": "phone_response_note",
        "メールヘッダ全体": "mail_headers_raw",
        "担当者名": "assignee_name",
        "To/From設定": "to_from_setting",
        "メール種別": "mail_type",
        "メール状態": "mail_status",
        "フォルダ名": "folder_name",
        "分類１": "category_1",
        "分類２": "category_2",
        "分類３": "category_3",
        "受送信時刻": "sent_received_at",
        "返信所要時間（分）：返信終了時刻 - 受信時刻": "reply_lead_time_minutes",
        "返信メール作成所要時間（分）：返信終了時刻 - 返信開始時刻": "reply_authoring_time_minutes",
        "承認所要時間（分）：承認時刻 - 承認依頼時刻": "approval_lead_time_minutes",
        "返信開始時刻": "reply_started_at",
        "返信時刻": "replied_at",
        "承認依頼時刻": "approval_requested_at",
        "承認時刻": "approved_at",
        "コメント": "comment",
        "ラベル１": "label_1",
        "ラベル２": "label_2",
        "ラベル３": "label_3",
        "ラベル４": "label_4",
        "ラベル５": "label_5",
    }
    if s in exact:
        return exact[s]

    bp = _bracket_parts(s)
    if bp:
        outer, inner = bp
        inner_en = _INNER.get(inner) or _PURCHASE_INNER.get(inner)
        if outer == "請求先" and inner_en:
            return f"billing_{inner_en}"
        if outer == "お届け先" and inner_en:
            return f"shipping_{inner_en}"
        if outer == "購入商品" and inner_en:
            return f"line_item_{inner_en}"

    m = re.match(r"^自由項目(\d+)$", s)
    if m:
        return f"custom_field_{int(m.group(1))}"

    m = re.match(r"^商品自由項目(\d+)$", s)
    if m:
        return f"product_custom_field_{m.group(1)}"

    # フォールバック: ASCII化
    safe = re.sub(r"[^\w]+", "_", s, flags=re.ASCII)
    safe = re.sub(r"_+", "_", safe).strip("_").lower()
    if not safe:
        safe = "col_unknown"
    return f"ja_{safe}"[:120]


def build_english_headers(
    japanese_headers: list[str],
    source_id: str,
    overrides: dict[str, str] | None = None,
) -> list[str]:
    """
    和名ヘッダー配列から英名ヘッダー配列を生成（重複は _2, _3 を付与）。
    overrides: 設定ファイルによる上書き（和名キー → 英名）。
    """
    ovr = overrides or {}
    raw: list[str] = []
    for h in japanese_headers:
        if h in ovr:
            raw.append(ovr[h])
        else:
            raw.append(japanese_header_to_english(h, source_id))
    return _uniq(raw)
