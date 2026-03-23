"""
Lab 03: Structured Extraction Pipeline

学習目標:
- tool_use + tool_choice で structured output を強制する
- nullable/optional の使い分け
- semantic validation (calculated_total vs stated_total)
- エラーコンテキスト付き validation-retry ループ
- human review routing
"""

import json
import os
import argparse
from dataclasses import dataclass
from typing import Any

import anthropic

# ────────────────────────────────────────────────
# JSON Schema 定義
# ────────────────────────────────────────────────

INVOICE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        # required: 必ず存在する
        "invoice_number": {
            "type": "string",
            "description": "請求書番号 (例: INV-2024-001)",
        },
        "vendor_name": {
            "type": "string",
            "description": "請求元企業名",
        },
        # nullable: 存在するが読み取れない場合は null
        "invoice_date": {
            "type": ["string", "null"],
            "description": "請求日 YYYY-MM-DD 形式。読み取れない場合は null",
        },
        "due_date": {
            "type": ["string", "null"],
            "description": "支払期限 YYYY-MM-DD 形式。記載がない場合は null",
        },
        # 行項目
        "line_items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "description": {"type": "string"},
                    "quantity": {"type": "number"},
                    "unit_price": {"type": "number"},
                    "amount": {
                        "type": "number",
                        "description": "行項目の金額 (quantity * unit_price と一致するはず)",
                    },
                },
                "required": ["description", "quantity", "unit_price", "amount"],
            },
        },
        # nullable: 記載がない場合は null
        "subtotal": {
            "type": ["number", "null"],
            "description": "小計 (税抜)。記載がない場合は null",
        },
        "tax_rate": {
            "type": ["number", "null"],
            "description": "税率 (例: 0.10 = 10%)。記載がない場合は null",
        },
        "tax_amount": {
            "type": ["number", "null"],
            "description": "消費税額。記載がない場合は null",
        },
        # 2 つの合計値: semantic validation のキー
        "stated_total": {
            "type": "number",
            "description": "請求書に記載された合計金額 (そのまま転記)",
        },
        "calculated_total": {
            "type": "number",
            "description": "行項目の合計から計算した合計金額 (line_items の amount の合計 + tax_amount)",
        },
        # optional: 存在しない可能性あり (required に含めない)
        "discount_amount": {
            "type": "number",
            "description": "割引額 (割引がある場合のみ)",
        },
        # other + detail パターン
        "payment_method": {
            "type": "string",
            "enum": ["bank_transfer", "credit_card", "cash", "other"],
            "description": "支払い方法",
        },
        "payment_method_detail": {
            "type": ["string", "null"],
            "description": "payment_method が 'other' の場合の詳細",
        },
    },
    "required": [
        "invoice_number",
        "vendor_name",
        "invoice_date",
        "line_items",
        "stated_total",
        "calculated_total",
        "payment_method",
    ],
}

EXTRACT_INVOICE_TOOL = {
    "name": "extract_invoice",
    "description": (
        "請求書テキストから構造化データを抽出します。"
        "stated_total は請求書に記載された合計金額をそのまま転記し、"
        "calculated_total は行項目から計算した合計金額を記入してください。"
        "この 2 つが一致しない場合でも、そのまま両方を記入してください。"
    ),
    "input_schema": INVOICE_SCHEMA,
}

# ────────────────────────────────────────────────
# バリデーション
# ────────────────────────────────────────────────

DISCREPANCY_THRESHOLD = 1.0      # 1円以上の乖離はエラー
ESCALATION_THRESHOLD = 100.0     # 100円以上の乖離は human review


@dataclass
class ValidationResult:
    is_valid: bool
    errors: list[str]
    warnings: list[str]


def validate_invoice(data: dict[str, Any]) -> ValidationResult:
    """
    請求書データのバリデーション

    1. 行項目の積算チェック (quantity * unit_price = amount)
    2. calculated_total の整合性チェック
    3. stated_total vs calculated_total の乖離チェック
    """
    errors = []
    warnings = []

    line_items = data.get("line_items", [])

    # ── 1. 行項目の積算チェック ──
    items_total = 0.0
    for i, item in enumerate(line_items):
        expected_amount = item["quantity"] * item["unit_price"]
        actual_amount = item["amount"]
        items_total += actual_amount

        if abs(actual_amount - expected_amount) > DISCREPANCY_THRESHOLD:
            errors.append(
                f"Line item {i + 1} amount mismatch: "
                f"'{item['description']}' "
                f"expected {expected_amount:.2f} "
                f"(qty={item['quantity']} × price={item['unit_price']}), "
                f"got {actual_amount:.2f}"
            )

    # ── 2. calculated_total の整合性チェック ──
    tax_amount = data.get("tax_amount") or 0.0
    expected_calculated = items_total + tax_amount

    if abs(data["calculated_total"] - expected_calculated) > DISCREPANCY_THRESHOLD:
        errors.append(
            f"calculated_total mismatch: "
            f"expected {expected_calculated:.2f} "
            f"(items_sum={items_total:.2f} + tax={tax_amount:.2f}), "
            f"got {data['calculated_total']:.2f}"
        )

    # ── 3. stated_total vs calculated_total の乖離チェック ──
    total_discrepancy = abs(data["stated_total"] - data["calculated_total"])
    if total_discrepancy > ESCALATION_THRESHOLD:
        errors.append(
            f"Large total discrepancy: "
            f"stated={data['stated_total']:.2f}, "
            f"calculated={data['calculated_total']:.2f}, "
            f"diff={total_discrepancy:.2f}"
        )
    elif total_discrepancy > DISCREPANCY_THRESHOLD:
        warnings.append(
            f"Minor total discrepancy: "
            f"stated={data['stated_total']:.2f}, "
            f"calculated={data['calculated_total']:.2f}"
        )

    # ── 4. other + detail パターンの確認 ──
    if data.get("payment_method") == "other" and not data.get("payment_method_detail"):
        warnings.append("payment_method is 'other' but payment_method_detail is not provided")

    return ValidationResult(
        is_valid=len(errors) == 0,
        errors=errors,
        warnings=warnings,
    )


# ────────────────────────────────────────────────
# Human Review Routing
# ────────────────────────────────────────────────

def route_result(data: dict[str, Any], validation: ValidationResult) -> str:
    """
    検証結果に基づいて処理ルートを決定する

    Returns: "auto_process" | "human_review" | "reject"
    """
    # 複数エラーは human review
    if len(validation.errors) > 1:
        return "human_review"

    # 大きな乖離は human review
    total_discrepancy = abs(data.get("stated_total", 0) - data.get("calculated_total", 0))
    if total_discrepancy > ESCALATION_THRESHOLD:
        return "human_review"

    # 請求日不明は human review
    if data.get("invoice_date") is None:
        return "human_review"

    # エラーがある場合は human review
    if validation.errors:
        return "human_review"

    return "auto_process"


# ────────────────────────────────────────────────
# Extraction with Retry
# ────────────────────────────────────────────────

MAX_RETRIES = 3


def extract_with_retry(
    invoice_text: str,
    client: anthropic.Anthropic,
    verbose: bool = True,
) -> tuple[dict[str, Any], ValidationResult]:
    """
    エラーコンテキスト付き validation-retry ループ

    単純リトライ (NG):
      同じエラーを繰り返す可能性が高い

    エラーコンテキスト付きリトライ (OK):
      何が問題だったかをモデルに伝えて修正させる
    """
    messages: list[dict[str, Any]] = [
        {
            "role": "user",
            "content": f"以下の請求書からデータを抽出してください:\n\n{invoice_text}",
        }
    ]

    last_validation = ValidationResult(is_valid=False, errors=["Not extracted yet"], warnings=[])

    for attempt in range(1, MAX_RETRIES + 1):
        if verbose:
            print(f"\n--- 抽出試行 {attempt}/{MAX_RETRIES} ---")

        response = client.messages.create(
            model="claude-opus-4-5",
            max_tokens=2048,
            tools=[EXTRACT_INVOICE_TOOL],
            # ★ tool_choice: specific で JSON 出力を強制
            tool_choice={"type": "tool", "name": "extract_invoice"},
            messages=messages,
        )

        # ツール呼び出し結果を取得
        tool_use_block = next(
            (b for b in response.content if b.type == "tool_use"), None
        )
        if not tool_use_block:
            if verbose:
                print("  ERROR: No tool_use block in response")
            continue

        extracted_data = tool_use_block.input
        if verbose:
            print(f"  Extracted: {json.dumps(extracted_data, ensure_ascii=False, indent=2)}")

        # バリデーション
        validation = validate_invoice(extracted_data)
        last_validation = validation

        if verbose:
            if validation.errors:
                print(f"  Errors: {validation.errors}")
            if validation.warnings:
                print(f"  Warnings: {validation.warnings}")

        if validation.is_valid:
            if verbose:
                print("  ✓ Validation passed")
            return extracted_data, validation

        # ★ エラーコンテキストを含めてリトライ
        messages.append({"role": "assistant", "content": response.content})
        messages.append(
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_use_block.id,
                        "content": "Validation failed",
                    },
                    {
                        "type": "text",
                        "text": (
                            "抽出結果に以下の問題があります。修正して再度抽出してください:\n\n"
                            + "\n".join(f"- {e}" for e in validation.errors)
                        ),
                    },
                ],
            }
        )

    if verbose:
        print(f"\n  ! Max retries ({MAX_RETRIES}) reached. Using last result.")

    # 最終結果を返す (routing で human review に回す)
    return extracted_data, last_validation


# ────────────────────────────────────────────────
# サンプル請求書
# ────────────────────────────────────────────────

SAMPLE_INVOICES = {
    "normal": {
        "description": "正常な請求書",
        "text": """
請求書

請求書番号: INV-2024-001
請求日: 2024-03-15
支払期限: 2024-04-15
請求元: 株式会社サンプル

品目:
1. コンサルティングサービス  10時間 × ¥15,000 = ¥150,000
2. ドキュメント作成          5時間 × ¥12,000 = ¥60,000

小計: ¥210,000
消費税 (10%): ¥21,000
合計: ¥231,000

支払方法: 銀行振込
        """,
    },
    "discrepancy": {
        "description": "stated_total と calculated_total が不一致な請求書",
        "text": """
請求書

請求書番号: INV-2024-002
請求日: 2024-03-20
請求元: テスト商事株式会社

品目:
1. ソフトウェアライセンス  1式 × ¥100,000 = ¥100,000
2. 保守サポート            1年 × ¥50,000 = ¥50,000

小計: ¥150,000
消費税 (10%): ¥15,000
合計: ¥175,000  ← ※ 実際は ¥165,000 のはず (意図的な不一致)

支払方法: クレジットカード
        """,
    },
    "missing_fields": {
        "description": "nullable フィールドが欠損した請求書",
        "text": """
請求書

請求書番号: INV-2024-003
（請求日の記載なし）
請求元: 不明商店

品目:
1. 商品A  2個 × ¥3,000 = ¥6,000

合計: ¥6,000

支払方法: その他 (QRコード決済)
        """,
    },
}


def main():
    parser = argparse.ArgumentParser(description="Structured Extraction Pipeline Lab")
    parser.add_argument(
        "--scenario",
        choices=list(SAMPLE_INVOICES.keys()),
        default="normal",
    )
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
    scenario = SAMPLE_INVOICES[args.scenario]

    print(f"\n{'='*60}")
    print(f"シナリオ: {scenario['description']}")
    print(f"{'='*60}")
    print(f"請求書テキスト:\n{scenario['text']}")

    # 抽出 + バリデーション
    extracted, validation = extract_with_retry(
        invoice_text=scenario["text"],
        client=client,
        verbose=not args.quiet,
    )

    # ルーティング
    route = route_result(extracted, validation)

    print(f"\n{'='*60}")
    print("抽出結果:")
    print(f"{'='*60}")
    print(json.dumps(extracted, ensure_ascii=False, indent=2))

    print(f"\n{'='*60}")
    print("バリデーション結果:")
    print(f"{'='*60}")
    print(f"Valid: {validation.is_valid}")
    if validation.errors:
        print(f"Errors: {validation.errors}")
    if validation.warnings:
        print(f"Warnings: {validation.warnings}")

    print(f"\n{'='*60}")
    print(f"処理ルート: {route.upper()}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
