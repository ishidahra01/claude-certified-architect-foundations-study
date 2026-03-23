"""
Lab 01: Customer Support Agent

学習目標:
- stop_reason による agentic loop の制御
- isError パターンによるツールエラー表現
- Hook / Gate による deterministic な escalation
- Prompt vs Code での制約の使い分け
- PostToolUse フックによるデータ正規化
- アンチパターンとその問題点の理解
"""

import json
import os
import argparse
import datetime
from typing import Any

import anthropic

# ────────────────────────────────────────────────
# 設定値 (業務ルールは code で担保する)
# ────────────────────────────────────────────────
REFUND_THRESHOLD = 500.0  # この金額を超える返金は自動処理不可

# ────────────────────────────────────────────────
# セッション状態 (プログラム的前提条件ゲートに使用)
# ────────────────────────────────────────────────
session_state: dict[str, Any] = {
    "customer_verified": False,
    "verified_customer_id": None,
}

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
        # Unix タイムスタンプ (PostToolUse フックで ISO 8601 に変換される)
        "created_at": 1704067200,
        "delivered_at": 1704326400,
        "status_code": 200,
    },
    "ORD-002": {
        "id": "ORD-002",
        "customer_id": "CUST-002",
        "items": [{"name": "マウス", "price": 3000, "qty": 2}],
        "total": 6000,
        "status": "delivered",
        "can_refund": True,
        "created_at": 1704153600,
        "delivered_at": 1704412800,
        "status_code": 200,
    },
    "ORD-003": {
        "id": "ORD-003",
        "customer_id": "CUST-001",
        "items": [{"name": "キーボード", "price": 8000, "qty": 1}],
        "total": 8000,
        "status": "processing",
        "can_refund": False,
        "created_at": 1704240000,
        "delivered_at": None,
        "status_code": 102,
    },
}

# ステータスコードの人間可読な説明
STATUS_CODE_DESCRIPTIONS = {
    102: "Processing - 処理中",
    200: "OK - 正常完了",
    201: "Created - 作成済み",
    400: "Bad Request - 不正なリクエスト",
    404: "Not Found - 見つかりません",
    500: "Internal Server Error - サーバーエラー",
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
            "返金処理の前に必ずこのツールを呼び出して顧客を確認してください。"
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
            "事前に get_customer で顧客を確認してから呼び出してください。"
            "事前に lookup_order で注文を確認してから呼び出してください。"
            f"返金金額が ¥{REFUND_THRESHOLD:,.0f} を超える場合は自動処理できないため、"
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
            "context には customer_id, order_id, root_cause, refund_amount, "
            "recommended_action, conversation_summary を含めてください。"
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
                    "description": (
                        "引き継ぎに必要な構造化コンテキスト情報。"
                        "customer_id, order_id, root_cause, refund_amount, "
                        "recommended_action, conversation_summary を含めること。"
                    ),
                    "properties": {
                        "customer_id": {"type": "string"},
                        "order_id": {"type": "string"},
                        "root_cause": {"type": "string"},
                        "refund_amount": {"type": "number"},
                        "recommended_action": {"type": "string"},
                        "conversation_summary": {"type": "string"},
                    },
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
# PostToolUse フック
# ────────────────────────────────────────────────

def post_tool_use_hook(
    tool_name: str,
    result: dict[str, Any],
    learn: bool = False,
) -> dict[str, Any]:
    """
    ツール実行後に呼び出されるフック。

    - Unix タイムスタンプを ISO 8601 形式に変換する
    - 数値ステータスコードに人間可読な説明を追加する

    これにより、異なるバックエンドシステムからのデータ形式を統一し、
    モデルが一貫した形式で情報を処理できるようにする。
    """
    if learn:
        print(
            "\n  📌 [LEARN] PostToolUse フック実行中"
            f"\n     なぜフック後処理か: 異なるシステムから返ってくるデータ形式を統一することで、"
            "\n     モデルが一貫した形式で情報を処理できる"
        )

    normalized = dict(result)
    normalizations: list[str] = []

    # ネストされた dict も含めて再帰的に正規化する
    def normalize_value(obj: Any) -> Any:
        if isinstance(obj, dict):
            new_obj = {}
            for key, val in obj.items():
                # Unix タイムスタンプの変換 (epoch 秒 / _at で終わるキー)
                if (
                    key.endswith("_at")
                    and isinstance(val, int)
                    and 1_000_000_000 <= val <= 9_999_999_999
                ):
                    iso = datetime.datetime.fromtimestamp(
                        val, tz=datetime.timezone.utc
                    ).isoformat()
                    new_obj[key] = iso
                    normalizations.append(
                        f"{key}: {val} (Unix) → {iso} (ISO 8601)"
                    )
                # 数値ステータスコードに説明を追加
                elif key == "status_code" and isinstance(val, int):
                    description = STATUS_CODE_DESCRIPTIONS.get(val, "Unknown")
                    new_obj[key] = val
                    new_obj["status_code_description"] = description
                    normalizations.append(
                        f"status_code: {val} → \"{description}\""
                    )
                else:
                    new_obj[key] = normalize_value(val)
            return new_obj
        elif isinstance(obj, list):
            return [normalize_value(item) for item in obj]
        return obj

    normalized = normalize_value(normalized)

    if normalizations and learn:
        print(f"     正規化した項目:")
        for note in normalizations:
            print(f"       • {note}")
    elif not normalizations and learn:
        print(f"     正規化対象なし (ツール: {tool_name})")

    return normalized


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
    # ★ セッション状態を更新 (プログラム的前提条件ゲートのための状態追跡)
    session_state["customer_verified"] = True
    session_state["verified_customer_id"] = customer_id
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

    ★ Gate 1: 顧客確認の前提条件チェック (プログラム的順序強制)
    ★ Gate 2: 閾値チェックは prompt ではなく code で担保
    """
    # ★ PREREQUISITE GATE: 顧客確認が完了しているかチェック
    if not session_state["customer_verified"]:
        return {
            "isError": True,
            "retryable": True,
            "prerequisite_missing": "customer_verification",
            "content": [
                {
                    "type": "text",
                    "text": (
                        "PREREQUISITE GATE BLOCKED: process_refund requires get_customer "
                        "to be called successfully first. "
                        "顧客確認が完了していません。先に get_customer を呼び出してください。"
                    ),
                }
            ],
        }

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
        "status_code": 200,
        "processed_at": int(datetime.datetime.now(tz=datetime.timezone.utc).timestamp()),
        "message": f"返金処理が完了しました。¥{amount:,.0f} を返金いたします。",
    }


def escalate_to_human(
    reason: str, priority: str, context: dict | None = None
) -> dict[str, Any]:
    """
    人間へのエスカレーション

    ★ 構造化引き継ぎ: context に必要なフィールドが揃っているか確認し、
      フォーマットされたハンドオフサマリーを出力する
    """
    print(f"\n  [ESCALATION] Priority: {priority.upper()}")
    print(f"  Reason: {reason}")

    # 構造化ハンドオフサマリーの出力
    if context:
        print("\n  ┌─────────────────────────────────────────────┐")
        print("  │        STRUCTURED HANDOFF SUMMARY           │")
        print("  ├─────────────────────────────────────────────┤")
        fields = [
            ("customer_id",          "顧客ID"),
            ("order_id",             "注文ID"),
            ("root_cause",           "根本原因"),
            ("refund_amount",        "返金金額"),
            ("recommended_action",   "推奨アクション"),
            ("conversation_summary", "会話要約"),
        ]
        for key, label in fields:
            value = context.get(key, "(未設定)")
            if key == "refund_amount" and isinstance(value, (int, float)):
                value = f"¥{value:,.0f}"
            # 長いテキストは折り返す
            value_str = str(value)
            if len(value_str) > 40:
                value_str = value_str[:37] + "..."
            print(f"  │ {label:<12}: {value_str:<32}│")
        print("  └─────────────────────────────────────────────┘")

        # 設定されていない推奨フィールドを警告
        missing = [k for k, _ in fields if k not in context]
        if missing:
            print(f"\n  ⚠️  Missing recommended handoff fields: {', '.join(missing)}")
    else:
        print("  ⚠️  No structured context provided for handoff.")

    return {
        "isError": False,
        "escalation_id": "ESC-2024-001",
        "status": "escalated",
        "message": f"ケースをエスカレーションしました。担当者が対応いたします。(優先度: {priority})",
    }


# ────────────────────────────────────────────────
# ツール実行ディスパッチャー
# ────────────────────────────────────────────────

def execute_tool(
    tool_name: str,
    tool_input: dict[str, Any],
    learn: bool = False,
) -> dict[str, Any]:
    """ツール名に応じてツールを実行し、PostToolUse フックを適用する"""
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

    # ★ PREREQUISITE GATE 学習ノート
    if learn and tool_name == "process_refund" and not session_state["customer_verified"]:
        print(
            "\n  📌 [LEARN] プログラム的前提条件ゲート発動直前"
            "\n     なぜプログラム的前提条件か: prompt で『先に顧客確認して』と指示しても"
            "\n     確率的にしか守られない。code で順序を強制する"
        )

    raw_result = tool_fn(**tool_input)

    # ★ PostToolUse フックの適用
    normalized_result = post_tool_use_hook(tool_name, raw_result, learn=learn)

    return normalized_result


# ────────────────────────────────────────────────
# Agentic Loop
# ────────────────────────────────────────────────

def run_support_agent(
    user_message: str,
    verbose: bool = True,
    learn: bool = False,
) -> str:
    """
    顧客サポートエージェントの agentic loop

    stop_reason による制御:
    - end_turn: 処理完了 → ループ終了
    - tool_use: ツール呼び出し → 実行して結果を返す
    """
    # セッション状態をリセット
    session_state["customer_verified"] = False
    session_state["verified_customer_id"] = None

    client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

    system_prompt = """あなたは顧客サポートエージェントです。

## 役割
顧客の問い合わせに対して、適切なツールを使用して問題を解決します。

## ツール使用の原則
1. 顧客情報は必ず get_customer で確認する (返金処理の前提条件)
2. 注文に関する操作の前に lookup_order で注文を確認する
3. 返金処理には process_refund を使用する
4. 自動処理できない場合は escalate_to_human を使用する

## エスカレーション時の context フィールド
escalate_to_human を呼ぶ際は context に以下を含めること:
- customer_id: 顧客ID
- order_id: 注文ID
- root_cause: 問題の根本原因
- refund_amount: 要求された返金金額
- recommended_action: 推奨される次のアクション
- conversation_summary: これまでの会話の要約

## 注意事項
- ツールが isError: true を返した場合は、エラー内容に応じて適切に対処する
- requires_human: true が返った場合は、必ず escalate_to_human を呼び出す
- 顧客に対して丁寧かつ明確に状況を説明する"""

    messages = [{"role": "user", "content": user_message}]

    if learn:
        print(
            "\n  📌 [LEARN] Agentic Loop 開始"
            "\n     ・messages リストが会話履歴として機能する"
            "\n     ・stop_reason が API からの意味的シグナル (文字列解析ではない)"
        )

    iteration = 0
    max_iterations = 10  # 無限ループ防止 (安全ネット)

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
            if learn:
                print(
                    "\n  📌 [LEARN] stop_reason == 'end_turn' を検出"
                    "\n     なぜ end_turn を使うか: テキスト内の特定文字列ではなく、"
                    "\n     APIの意味的シグナルでループ終了を判断する"
                )
            text_blocks = [b for b in response.content if b.type == "text"]
            final_response = text_blocks[0].text if text_blocks else "(応答なし)"
            return final_response

        elif response.stop_reason == "tool_use":
            tool_use_blocks = [b for b in response.content if b.type == "tool_use"]

            # assistant の応答を会話に追加 (tool_use block をそのまま保存)
            messages.append({"role": "assistant", "content": response.content})

            if learn:
                print(
                    "\n  📌 [LEARN] assistant の応答を messages に追加"
                    "\n     なぜ: tool_use block をそのまま保存することで、"
                    "\n     モデルは自分が何を要求したかを覚えている"
                )

            # 全ツールを実行して結果を収集
            tool_results = []
            for tool_block in tool_use_blocks:
                if verbose:
                    print(
                        f"  Tool: {tool_block.name}"
                        f"({json.dumps(tool_block.input, ensure_ascii=False)})"
                    )

                result = execute_tool(tool_block.name, tool_block.input, learn=learn)

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

            if learn:
                print(
                    "\n  📌 [LEARN] ツール結果を messages に追加"
                    "\n     なぜ: ツール結果を会話履歴に追加することで、"
                    "\n     モデルは次の判断に新情報を組み込める"
                )

        else:
            # max_tokens 等
            return f"(処理が中断されました: stop_reason={response.stop_reason})"

    return "(最大ループ回数に達しました)"


# ────────────────────────────────────────────────
# アンチパターンのデモ
# ────────────────────────────────────────────────

def run_support_agent_with_antipatterns() -> None:
    """
    アンチパターンのデモ (API 呼び出しなし・自己完結)

    Domain 1 試験要件:
    - Task 1.3: stop_reason を使った正しいループ制御
    - Task 1.4: プログラム的な制約 vs プロンプトベースの制約

    ここでは実際に壊れる様子をシミュレートして学ぶ。
    """
    SEP = "─" * 60

    print(f"\n{'='*60}")
    print("  アンチパターン デモ (API 不要・シミュレーション)")
    print(f"{'='*60}")

    # ──────────────────────────────────────────
    # アンチパターン 1: NL シグナル解析でループ終了を判断
    # ──────────────────────────────────────────
    print(f"\n{SEP}")
    print("【アンチパターン 1】テキスト文字列でループ終了を判断する")
    print(SEP)
    print(
        "\n❌ WRONG: assistant のテキストに '処理完了' が含まれているかで終了を判断\n"
    )

    # シミュレート: モデルが返す可能性があるテキストのバリエーション
    simulated_responses = [
        # (テキスト, 実際の意図)
        ("返金処理を完了しました。", "本当に完了"),
        ("処理完了しましたが、エスカレーションが必要です。", "まだ作業が必要"),
        ("処理完了できませんでした。", "失敗したのに '処理完了' を含む"),
        ("Refund processing done.", "英語応答は検知されない"),
        ("処\u0020理\u0020完\u0020了", "スペース入りは検知されない"),
    ]

    print(f"  {'テキスト':<40} {'NL判定':^8} {'実際の意図'}")
    print(f"  {'─'*40} {'─'*8} {'─'*20}")
    for text, intent in simulated_responses:
        nl_detected = "処理完了" in text
        marker = "✅ 終了" if nl_detected else "🔄 継続"
        false_positive = nl_detected and ("エスカレ" in text or "できませんでした" in text)
        false_negative = not nl_detected and intent == "本当に完了"
        intent_note = "⚠️ 誤判断!" if (false_positive or false_negative) else "OK"
        print(f"  {text:<40} {marker:^10} {intent_note}")

    print(
        "\n  💡 問題: モデルの出力テキストは毎回同じとは限らない。"
        "\n     言語・表現のバリエーションで判定が壊れる。"
        "\n     stop_reason='end_turn' は API が保証する意味的シグナル。"
    )

    print(f"\n✅ CORRECT: stop_reason == 'end_turn' でループ終了を判断する\n")
    correct_stop_reasons = [
        ("end_turn",    "モデルが応答完了と判断    → ループ終了"),
        ("tool_use",    "ツール呼び出しが必要       → ツール実行して継続"),
        ("max_tokens",  "トークン上限到達           → エラー処理"),
        ("stop_sequence","停止シーケンス検出        → 設計による"),
    ]
    for reason, description in correct_stop_reasons:
        print(f"  stop_reason='{reason}': {description}")

    # ──────────────────────────────────────────
    # アンチパターン 2: max_iterations を主たる停止機構として使う
    # ──────────────────────────────────────────
    print(f"\n{SEP}")
    print("【アンチパターン 2】max_iterations=2 を主たる停止機構にする")
    print(SEP)
    print(
        "\n❌ WRONG: max_iterations=2 に頼ると複数ツール呼び出しで途中終了する\n"
    )

    # 典型的なツール呼び出しシーケンスをシミュレート
    typical_tool_sequence = [
        ("get_customer",      "顧客情報取得"),
        ("lookup_order",      "注文情報取得"),
        ("process_refund",    "返金処理"),
        # → 閾値超えで失敗した場合
        ("escalate_to_human", "エスカレーション"),
    ]

    print(f"  典型的な escalation シナリオのツール呼び出しシーケンス:")
    for i, (tool, desc) in enumerate(typical_tool_sequence, 1):
        iteration_num = i  # 各ツール呼び出しは 1 イテレーション消費
        too_early = "⛔ max_iterations=2 で強制終了!" if iteration_num > 2 else "✅"
        print(f"    Iter {iteration_num}: {tool} ({desc}) {too_early}")

    print(
        "\n  💡 問題: max_iterations=2 では escalation シナリオが完了できない。"
        "\n     max_iterations はあくまで安全ネット (無限ループ防止)。"
        "\n     通常の停止は stop_reason='end_turn' で行う。"
        "\n     適切な max_iterations は処理の複雑さに応じて設定 (例: 10〜20)。"
    )

    print(f"\n✅ CORRECT: max_iterations は安全ネットとして高めに設定する")
    print(f"  本ラボでは max_iterations=10 を安全ネットとして使用。")
    print(f"  正常終了は常に stop_reason='end_turn' によって行われる。")

    # ──────────────────────────────────────────
    # アンチパターン 3: プロンプトで順序を制御しようとする
    # ──────────────────────────────────────────
    print(f"\n{SEP}")
    print("【アンチパターン 3】プロンプトのみで処理順序を制御しようとする")
    print(SEP)
    print(
        "\n❌ WRONG: 'get_customer を呼んでから process_refund を呼んでください' と"
        "\n   プロンプトに書くだけでは、確率的にしか守られない\n"
    )

    # プロンプトが守られない可能性のあるシナリオをシミュレート
    prompt_only_risks = [
        ("モデルのサンプリング変動",      "同じプロンプトでも毎回同じ順序とは限らない"),
        ("コンテキスト長の増加",          "長い会話ではプロンプト冒頭の指示が軽視される"),
        ("競合する指示の優先度変動",      "複数の指示が競合すると予測不能な優先度になる"),
        ("将来のモデルバージョン",        "モデル更新で挙動が変わる可能性がある"),
        ("プロンプトインジェクション",    "悪意のある入力でプロンプト指示が上書きされる"),
    ]
    for risk, description in prompt_only_risks:
        print(f"  ⚠️  {risk}: {description}")

    print(
        "\n✅ CORRECT: session_state でプログラム的に前提条件を強制する"
        "\n"
        "\n   本ラボの実装:"
        "\n   1. session_state['customer_verified'] = False で初期化"
        "\n   2. get_customer 成功時に True に設定"
        "\n   3. process_refund はこのフラグを確認 → False なら isError を返す"
        "\n   4. どんなプロンプトが来ても、コードレベルで順序が保証される"
    )

    print(f"\n{'='*60}")
    print("  アンチパターン デモ完了")
    print(f"{'='*60}\n")


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
    "antipatterns": {
        "description": "アンチパターンのデモ (API 不要)",
        "message": "",  # API を呼ばないため不使用
    },
}


def main():
    parser = argparse.ArgumentParser(
        description="Customer Support Agent Lab (Domain 1)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "例:\n"
            "  python main.py --mode normal --learn\n"
            "  python main.py --mode escalation\n"
            "  python main.py --mode antipatterns\n"
            "  python main.py --mode auth_fail --quiet\n"
        ),
    )
    parser.add_argument(
        "--mode",
        choices=list(SCENARIOS.keys()),
        default="normal",
        dest="mode",
        help="実行するシナリオ (default: normal)",
    )
    # 後方互換: --scenario も受け付ける (非推奨)
    parser.add_argument(
        "--scenario",
        choices=list(SCENARIOS.keys()),
        dest="scenario_legacy",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--learn",
        action="store_true",
        help="各ステップで詳細な学習ノートを表示する",
    )
    parser.add_argument("--quiet", action="store_true", help="詳細出力を抑制")
    args = parser.parse_args()

    # --scenario は後方互換として --mode にフォールバック
    mode = args.scenario_legacy or args.mode

    # antipatterns モードは API 不要
    if mode == "antipatterns":
        run_support_agent_with_antipatterns()
        return

    scenario = SCENARIOS[mode]
    print(f"\n{'='*60}")
    print(f"シナリオ: {scenario['description']}")
    if args.learn:
        print(f"学習モード: ON (--learn)")
    print(f"{'='*60}")
    print(f"顧客メッセージ:\n  {scenario['message']}")
    print(f"{'='*60}")

    response = run_support_agent(
        user_message=scenario["message"],
        verbose=not args.quiet,
        learn=args.learn,
    )

    print(f"\n{'='*60}")
    print("エージェント最終応答:")
    print(f"{'='*60}")
    print(response)


if __name__ == "__main__":
    main()
