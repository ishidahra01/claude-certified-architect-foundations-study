"""
Lab 03: Structured Extraction Pipeline (Claude Agent SDK 版)

学習目標:
- Claude Agent SDK で structured extraction を実装する
- custom MCP tool を1つに絞って structured output を強制する
- nullable/optional の使い分けを理解する
- semantic validation (calculated_total vs stated_total) を実装する
- エラーコンテキスト付き validation-retry ループを実装する
- human review routing の判断ロジックを実装する
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from typing import Any

import anyio
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, create_sdk_mcp_server, tool
from claude_agent_sdk.types import AssistantMessage, ResultMessage, TextBlock, ToolUseBlock
from dotenv import load_dotenv

load_dotenv()

# ────────────────────────────────────────────────
# JSON Schema 定義
# ────────────────────────────────────────────────
INVOICE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "invoice_number": {
            "type": "string",
            "description": "請求書番号 (例: INV-2024-001)",
        },
        "vendor_name": {
            "type": "string",
            "description": "請求元企業名",
        },
        "invoice_date": {
            "type": ["string", "null"],
            "description": "請求日 YYYY-MM-DD 形式。読み取れない場合は null",
        },
        "due_date": {
            "type": ["string", "null"],
            "description": "支払期限 YYYY-MM-DD 形式。記載がない場合は null",
        },
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
                        "description": "quantity * unit_price と一致する金額",
                    },
                },
                "required": ["description", "quantity", "unit_price", "amount"],
            },
        },
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
        "stated_total": {
            "type": "number",
            "description": "請求書に記載された合計金額をそのまま転記",
        },
        "calculated_total": {
            "type": "number",
            "description": "line_items.amount の合計 + tax_amount から計算した合計金額",
        },
        "discount_amount": {
            "type": "number",
            "description": "割引額 (割引がある場合のみ)",
        },
        "payment_method": {
            "type": "string",
            "enum": ["bank_transfer", "credit_card", "cash", "other"],
            "description": "支払い方法",
        },
        "payment_method_detail": {
            "type": ["string", "null"],
            "description": "payment_method が other の場合の詳細",
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

DISCREPANCY_THRESHOLD = 1.0
ESCALATION_THRESHOLD = 100.0
MAX_RETRIES = 3

SYSTEM_PROMPT = """あなたは請求書データ抽出エージェントです。

必須ルール:
1. 回答テキストだけで済ませず、必ず extract_invoice ツールを使って構造化データを返す
2. stated_total は請求書の記載値をそのまま転記する
3. calculated_total は line_items.amount の合計 + tax_amount から計算する
4. stated_total と calculated_total が一致しなくても両方ともそのまま埋める
5. invoice_date, due_date, subtotal, tax_rate, tax_amount, payment_method_detail は読めない/存在しない場合は null を使う
6. discount_amount は割引が明示されているときだけ含める。なければ省略する
7. payment_method が other の場合は payment_method_detail も埋める
"""


@dataclass
class ValidationResult:
    is_valid: bool
    errors: list[str]
    warnings: list[str]


@tool(
    "extract_invoice",
    (
        "請求書テキストから構造化データを抽出します。"
        "入力スキーマの各フィールドを正確に埋めてください。"
        "stated_total は請求書記載値、calculated_total は行項目合計から計算した値です。"
        "回答テキストではなく必ずこのツールを使って返してください。"
    ),
    INVOICE_SCHEMA,
)
async def extract_invoice_tool(args: dict[str, Any]) -> dict[str, Any]:
    """抽出された構造化データを受け取り、検証パイプラインへ渡す。"""
    return {
        "content": [
            {
                "type": "text",
                "text": "Structured invoice extraction received for validation.",
            }
        ]
    }


def validate_invoice(data: dict[str, Any]) -> ValidationResult:
    """請求書データの semantic validation。"""
    errors: list[str] = []
    warnings: list[str] = []

    line_items = data.get("line_items", [])
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

    tax_amount = data.get("tax_amount") or 0.0
    expected_calculated = items_total + tax_amount
    if abs(data["calculated_total"] - expected_calculated) > DISCREPANCY_THRESHOLD:
        errors.append(
            f"calculated_total mismatch: "
            f"expected {expected_calculated:.2f} "
            f"(items_sum={items_total:.2f} + tax={tax_amount:.2f}), "
            f"got {data['calculated_total']:.2f}"
        )

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

    if data.get("payment_method") == "other" and not data.get("payment_method_detail"):
        warnings.append("payment_method is 'other' but payment_method_detail is not provided")

    return ValidationResult(
        is_valid=len(errors) == 0,
        errors=errors,
        warnings=warnings,
    )


def route_result(data: dict[str, Any], validation: ValidationResult) -> str:
    """検証結果に基づいて処理ルートを決定する。"""
    if len(validation.errors) > 1:
        return "human_review"

    total_discrepancy = abs(data.get("stated_total", 0) - data.get("calculated_total", 0))
    if total_discrepancy > ESCALATION_THRESHOLD:
        return "human_review"

    if data.get("invoice_date") is None:
        return "human_review"

    if validation.errors:
        return "human_review"

    return "auto_process"


def build_retry_prompt(
    invoice_text: str,
    attempt: int,
    previous_data: dict[str, Any] | None = None,
    previous_validation: ValidationResult | None = None,
) -> str:
    """各試行用のプロンプトを構築する。"""
    prompt = (
        "以下の請求書テキストを読み取り、extract_invoice ツールを使って構造化データを返してください。\n\n"
        f"請求書テキスト:\n{invoice_text.strip()}\n"
    )

    if attempt > 1 and previous_data and previous_validation:
        prompt += (
            "\n前回の抽出結果には validation error がありました。"
            "同じツールを使って修正済みの構造化データを返してください。\n\n"
            f"前回の抽出結果:\n{json.dumps(previous_data, ensure_ascii=False, indent=2)}\n\n"
            "修正が必要な点:\n"
            + "\n".join(f"- {error}" for error in previous_validation.errors)
            + "\n"
        )

    return prompt


async def run_extraction_attempt(
    invoice_text: str,
    attempt: int,
    previous_data: dict[str, Any] | None = None,
    previous_validation: ValidationResult | None = None,
    verbose: bool = True,
) -> dict[str, Any]:
    """1回の抽出試行を Agent SDK で実行する。"""
    server = create_sdk_mcp_server(
        name="invoice",
        version="1.0.0",
        tools=[extract_invoice_tool],
    )
    options = ClaudeAgentOptions(
        system_prompt=SYSTEM_PROMPT,
        mcp_servers={"invoice": server},
        allowed_tools=["mcp__invoice__extract_invoice"],
        max_turns=4,
    )

    prompt = build_retry_prompt(
        invoice_text=invoice_text,
        attempt=attempt,
        previous_data=previous_data,
        previous_validation=previous_validation,
    )

    extracted_data: dict[str, Any] | None = None
    async with ClaudeSDKClient(options=options) as client:
        await client.query(prompt)
        async for message in client.receive_response():
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, ToolUseBlock) and block.name == "mcp__invoice__extract_invoice":
                        extracted_data = block.input
                        if verbose:
                            print(f"  Extracted via tool: {json.dumps(extracted_data, ensure_ascii=False, indent=2)}")
                    elif isinstance(block, TextBlock) and verbose:
                        print(f"  Claude: {block.text}")
            elif isinstance(message, ResultMessage) and verbose:
                print(f"  [RESULT] stop_reason={message.stop_reason}, turns={message.num_turns}")

    if extracted_data is None:
        raise RuntimeError("No extract_invoice tool call was produced by the agent")

    return extracted_data


async def extract_with_retry(
    invoice_text: str,
    verbose: bool = True,
) -> tuple[dict[str, Any], ValidationResult]:
    """エラーコンテキスト付き validation-retry ループ。"""
    last_validation = ValidationResult(is_valid=False, errors=["Not extracted yet"], warnings=[])
    last_data: dict[str, Any] = {}

    for attempt in range(1, MAX_RETRIES + 1):
        if verbose:
            print(f"\n--- 抽出試行 {attempt}/{MAX_RETRIES} ---")

        extracted_data = await run_extraction_attempt(
            invoice_text=invoice_text,
            attempt=attempt,
            previous_data=last_data if attempt > 1 else None,
            previous_validation=last_validation if attempt > 1 else None,
            verbose=verbose,
        )
        validation = validate_invoice(extracted_data)
        last_data = extracted_data
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

    if verbose:
        print(f"\n  ! Max retries ({MAX_RETRIES}) reached. Using last result.")
    return last_data, last_validation


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


async def main() -> None:
    parser = argparse.ArgumentParser(description="Structured Extraction Pipeline Lab (Claude Agent SDK)")
    parser.add_argument(
        "--scenario",
        choices=list(SAMPLE_INVOICES.keys()),
        default="normal",
    )
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    scenario = SAMPLE_INVOICES[args.scenario]
    print(f"\n{'='*60}")
    print(f"シナリオ: {scenario['description']}")
    print(f"{'='*60}")
    print(f"請求書テキスト:\n{scenario['text']}")

    extracted, validation = await extract_with_retry(
        invoice_text=scenario["text"],
        verbose=not args.quiet,
    )

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
    anyio.run(main)
