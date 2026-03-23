"""
Lab 01: Customer Support Agent

学習目標:
- stop_reason による agentic loop の制御
- isError パターンによるツールエラー表現
- Hook / Gate による deterministic な escalation
- Prompt vs Code での制約の使い分け
"""

import json
import os
import argparse
from typing import Any

import anthropic

# ────────────────────────────────────────────────
# 設定値 (業務ルールは code で担保する)
# ────────────────────────────────────────────────
REFUND_THRESHOLD = 500.0  # この金額を超える返金は自動処理不可

# ────────────────────────────────────────────────
# 擬似データベース
# ────────────────────────────────────────────────
CUSTOMERS_DB = {
    "CUST-001": {
        "id": "CUST-001",
        "name": "田中 太郎",
        "email": "tanaka@example.com",
        "is_vip": False,
        "is_verified": True,
    },
    "CUST-002": {
        "id": "CUST-002",
        "name": "山田 花子",
        "email": "yamada@example.com",
        "is_vip": True,
        "is_verified": True,
    },
}

ORDERS_DB = {
    "ORD-001": {
        "id": "ORD-001",
        "customer_id": "CUST-001",
        "items": [{"name": "ノートPC", "price": 120000, "qty": 1}],
        "total": 120000,
        "status": "delivered",
        "can_refund": True,
    },
    "ORD-002": {
        "id": "ORD-002",
        "customer_id": "CUST-002",
        "items": [{"name": "マウス", "price": 3000, "qty": 2}],
        "total": 6000,
        "status": "delivered",
        "can_refund": True,
    },
    "ORD-003": {
        "id": "ORD-003",
        "customer_id": "CUST-001",
        "items": [{"name": "キーボード", "price": 8000, "qty": 1}],
        "total": 8000,
        "status": "processing",
        "can_refund": False,
    },
}

# ────────────────────────────────────────────────
# ツール定義 (単一責任 + 明確な description)
# ────────────────────────────────────────────────
TOOLS = [
    {
        "name": "get_customer",
        "description": (
            "顧客IDで顧客情報を取得します。"
            "顧客IDは 'CUST-' で始まる文字列です。"
            "顧客が存在しない場合は isError: true を返します。"
            "このツールは読み取り専用で、データを変更しません。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "customer_id": {
                    "type": "string",
                    "description": "顧客ID (例: CUST-001)",
                }
            },
            "required": ["customer_id"],
        },
    },
    {
        "name": "lookup_order",
        "description": (
            "注文IDで注文情報を取得します。"
            "注文IDは 'ORD-' で始まる文字列です。"
            "返金処理の前に必ずこのツールで注文を確認してください。"
            "注文が存在しない場合は isError: true を返します。"
            "このツールは読み取り専用で、データを変更しません。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "order_id": {
                    "type": "string",
                    "description": "注文ID (例: ORD-001)",
                }
            },
            "required": ["order_id"],
        },
    },
    {
        "name": "process_refund",
        "description": (
            "注文の返金処理を行います。"
            "事前に lookup_order で注文を確認してから呼び出してください。"
            f"返金金額が ${REFUND_THRESHOLD} を超える場合は自動処理できないため、"
            "isError: true と requires_human: true が返ります。"
            "その場合は escalate_to_human を呼び出してください。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "order_id": {
                    "type": "string",
                    "description": "注文ID",
                },
                "amount": {
                    "type": "number",
                    "description": "返金金額 (JPY)",
                },
                "reason": {
                    "type": "string",
                    "description": "返金理由",
                },
            },
            "required": ["order_id", "amount", "reason"],
        },
    },
    {
        "name": "escalate_to_human",
        "description": (
            "人間のオペレーターにケースをエスカレーションします。"
            "以下の場合に使用してください: "
            "1) 返金金額が自動処理閾値を超える場合, "
            "2) 顧客が解決できない問題を抱えている場合, "
            "3) 不正利用の疑いがある場合。"
            "エスカレーション後、このセッションは終了します。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "エスカレーション理由",
                },
                "context": {
                    "type": "object",
                    "description": "引き継ぎに必要なコンテキスト情報",
                },
                "priority": {
                    "type": "string",
                    "enum": ["low", "normal", "high", "urgent"],
                    "description": "優先度",
                },
            },
            "required": ["reason", "priority"],
        },
    },
]

# ────────────────────────────────────────────────
# ツール実装
# ────────────────────────────────────────────────

def get_customer(customer_id: str) -> dict[str, Any]:
    """顧客情報取得 (read-only)"""
    customer = CUSTOMERS_DB.get(customer_id)
    if not customer:
        return {
            "isError": True,
            "retryable": False,
            "content": [{"type": "text", "text": f"Customer not found: {customer_id}"}],
        }
    return {"isError": False, "customer": customer}


def lookup_order(order_id: str) -> dict[str, Any]:
    """注文情報取得 (read-only)"""
    order = ORDERS_DB.get(order_id)
    if not order:
        return {
            "isError": True,
            "retryable": False,
            "content": [{"type": "text", "text": f"Order not found: {order_id}"}],
        }
    return {"isError": False, "order": order}


def process_refund(order_id: str, amount: float, reason: str) -> dict[str, Any]:
    """
    返金処理

    ★ Gate: 閾値チェックは prompt ではなく code で担保
    """
    order = ORDERS_DB.get(order_id)
    if not order:
        return {
            "isError": True,
            "retryable": False,
            "content": [{"type": "text", "text": f"Order not found: {order_id}"}],
        }

    if not order["can_refund"]:
        return {
            "isError": True,
            "retryable": False,
            "content": [
                {
                    "type": "text",
                    "text": f"Order {order_id} is not eligible for refund (status: {order['status']})",
                }
            ],
        }

    # ★ GATE: 金額閾値チェック (deterministic)
    if amount > REFUND_THRESHOLD:
        return {
            "isError": True,
            "retryable": False,
            "requires_human": True,
            "content": [
                {
                    "type": "text",
                    "text": (
                        f"Refund amount ¥{amount:,.0f} exceeds automatic processing threshold "
                        f"of ¥{REFUND_THRESHOLD:,.0f}. Human review required."
                    ),
                }
            ],
        }

    # 実際の返金処理 (擬似実装)
    print(f"  [REFUND EXECUTED] Order: {order_id}, Amount: ¥{amount:,.0f}, Reason: {reason}")
    return {
        "isError": False,
        "refund_id": f"REF-{order_id}-001",
        "order_id": order_id,
        "amount": amount,
        "status": "completed",
        "message": f"返金処理が完了しました。¥{amount:,.0f} を返金いたします。",
    }


def escalate_to_human(reason: str, priority: str, context: dict | None = None) -> dict[str, Any]:
    """人間へのエスカレーション"""
    print(f"\n  [ESCALATION] Priority: {priority.upper()}")
    print(f"  Reason: {reason}")
    if context:
        print(f"  Context: {json.dumps(context, ensure_ascii=False, indent=2)}")
    return {
        "isError": False,
        "escalation_id": "ESC-2024-001",
        "status": "escalated",
        "message": f"ケースをエスカレーションしました。担当者が対応いたします。(優先度: {priority})",
    }


# ────────────────────────────────────────────────
# ツール実行ディスパッチャー
# ────────────────────────────────────────────────

def execute_tool(tool_name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
    """ツール名に応じてツールを実行する"""
    tool_map = {
        "get_customer": get_customer,
        "lookup_order": lookup_order,
        "process_refund": process_refund,
        "escalate_to_human": escalate_to_human,
    }

    tool_fn = tool_map.get(tool_name)
    if not tool_fn:
        return {
            "isError": True,
            "content": [{"type": "text", "text": f"Unknown tool: {tool_name}"}],
        }

    return tool_fn(**tool_input)


# ────────────────────────────────────────────────
# Agentic Loop
# ────────────────────────────────────────────────

def run_support_agent(user_message: str, verbose: bool = True) -> str:
    """
    顧客サポートエージェントの agentic loop

    stop_reason による制御:
    - end_turn: 処理完了 → ループ終了
    - tool_use: ツール呼び出し → 実行して結果を返す
    """
    client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

    system_prompt = """あなたは顧客サポートエージェントです。

## 役割
顧客の問い合わせに対して、適切なツールを使用して問題を解決します。

## ツール使用の原則
1. 顧客情報は必ず get_customer で確認する
2. 注文に関する操作の前に lookup_order で注文を確認する
3. 返金処理には process_refund を使用する
4. 自動処理できない場合は escalate_to_human を使用する

## 注意事項
- ツールが isError: true を返した場合は、エラー内容に応じて適切に対処する
- requires_human: true が返った場合は、必ず escalate_to_human を呼び出す
- 顧客に対して丁寧かつ明確に状況を説明する"""

    messages = [{"role": "user", "content": user_message}]

    iteration = 0
    max_iterations = 10  # 無限ループ防止

    while iteration < max_iterations:
        iteration += 1

        if verbose:
            print(f"\n--- Iteration {iteration} ---")

        response = client.messages.create(
            model="claude-opus-4-5",
            max_tokens=1024,
            system=system_prompt,
            tools=TOOLS,
            messages=messages,
        )

        if verbose:
            print(f"stop_reason: {response.stop_reason}")

        # ────── stop_reason による分岐 ──────
        if response.stop_reason == "end_turn":
            # 完了: テキスト応答を返す
            text_blocks = [b for b in response.content if b.type == "text"]
            final_response = text_blocks[0].text if text_blocks else "(応答なし)"
            return final_response

        elif response.stop_reason == "tool_use":
            # ツール呼び出し: 実行して結果を返す
            tool_use_blocks = [b for b in response.content if b.type == "tool_use"]

            # assistant の応答を会話に追加
            messages.append({"role": "assistant", "content": response.content})

            # 全ツールを実行して結果を収集
            tool_results = []
            for tool_block in tool_use_blocks:
                if verbose:
                    print(f"  Tool: {tool_block.name}({json.dumps(tool_block.input, ensure_ascii=False)})")

                result = execute_tool(tool_block.name, tool_block.input)

                if verbose:
                    print(f"  Result: {json.dumps(result, ensure_ascii=False)}")

                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_block.id,
                        "content": json.dumps(result, ensure_ascii=False),
                    }
                )

            # ツール結果を会話に追加
            messages.append({"role": "user", "content": tool_results})

        else:
            # max_tokens 等
            return f"(処理が中断されました: stop_reason={response.stop_reason})"

    return "(最大ループ回数に達しました)"


# ────────────────────────────────────────────────
# シナリオ定義
# ────────────────────────────────────────────────

SCENARIOS = {
    "normal": {
        "description": "通常の返金シナリオ (閾値以内)",
        "message": (
            "こんにちは。顧客ID CUST-001 です。"
            "注文 ORD-002 のマウスが不良品でした。"
            "全額返金をお願いしたいのですが。"
        ),
    },
    "escalation": {
        "description": "閾値超えの返金シナリオ (エスカレーション)",
        "message": (
            "顧客ID CUST-001 です。"
            "注文 ORD-001 のノートPCが届いてすぐに壊れました。"
            "120,000円の全額返金を要求します。"
        ),
    },
    "auth_fail": {
        "description": "存在しない顧客・注文のシナリオ",
        "message": (
            "顧客ID CUST-999 です。"
            "注文 ORD-999 の返金をお願いします。"
        ),
    },
    "not_refundable": {
        "description": "返金不可の注文 (処理中ステータス)",
        "message": (
            "顧客ID CUST-001 です。"
            "注文 ORD-003 をキャンセルして返金してほしいです。"
        ),
    },
}


def main():
    parser = argparse.ArgumentParser(description="Customer Support Agent Lab")
    parser.add_argument(
        "--scenario",
        choices=list(SCENARIOS.keys()),
        default="normal",
        help="実行するシナリオ",
    )
    parser.add_argument("--quiet", action="store_true", help="詳細出力を抑制")
    args = parser.parse_args()

    scenario = SCENARIOS[args.scenario]
    print(f"\n{'='*60}")
    print(f"シナリオ: {scenario['description']}")
    print(f"{'='*60}")
    print(f"顧客メッセージ:\n  {scenario['message']}")
    print(f"{'='*60}")

    response = run_support_agent(
        user_message=scenario["message"],
        verbose=not args.quiet,
    )

    print(f"\n{'='*60}")
    print("エージェント最終応答:")
    print(f"{'='*60}")
    print(response)


if __name__ == "__main__":
    main()
