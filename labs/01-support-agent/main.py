"""
Lab 01: Customer Support Agent (Claude Agent SDK 版)

学習目標:
- Claude Agent SDK の `ClaudeSDKClient` でエージェントを実行する
- `@tool` + `create_sdk_mcp_server()` でカスタムツールを MCP として登録する
- `PreToolUse` フックで deterministic に返金閾値をブロックする
- `PostToolUse` フックで監査と出力正規化を行う
- セッション状態によるプログラム的前提条件ゲートを実装する
- Prompt vs Code での制約の使い分けを理解する
- アンチパターンとその問題点を確認する

★ Anthropic Client SDK 版との違い
- 旧: 手動 while ループ + `stop_reason` の分岐 + 自前のツールディスパッチ
- 新: Agent SDK がツール呼び出しとループを管理する
- 旧: JSON schema dict を API に渡す
- 新: `@tool` でツールを定義し、MCP サーバーに登録する
- 旧: フック概念を Python 内で擬似実装していた
- 新: `HookMatcher` + Python hook callback を正式 API として使う
"""

from __future__ import annotations

import argparse
import datetime
import json
from typing import Any

import anyio
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, create_sdk_mcp_server, tool
from claude_agent_sdk.types import (
    AssistantMessage,
    HookContext,
    HookInput,
    HookJSONOutput,
    HookMatcher,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)

# ────────────────────────────────────────────────
# 設定値 (業務ルールは code で担保する)
# ────────────────────────────────────────────────
REFUND_THRESHOLD = 10000.0  # この金額を超える返金は自動処理不可

# ────────────────────────────────────────────────
# セッション状態 (プログラム的前提条件ゲートに使用)
# ────────────────────────────────────────────────
session_state: dict[str, Any] = {
    "customer_verified": False,
    "verified_customer_id": None,
}

# 学習表示フラグ
LEARN_MODE = False
VERBOSE_MODE = True

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
        # Unix タイムスタンプ (PostToolUse フックで ISO 8601 に正規化)
        "created_at": 1704067200,
        "delivered_at": 1704326400,
        "status_code": 200,
    },
    "ORD-002": {
        "id": "ORD-002",
        "customer_id": "CUST-001",
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

STATUS_CODE_DESCRIPTIONS = {
    102: "Processing - 処理中",
    200: "OK - 正常完了",
    201: "Created - 作成済み",
    400: "Bad Request - 不正なリクエスト",
    404: "Not Found - 見つかりません",
    500: "Internal Server Error - サーバーエラー",
}


# ────────────────────────────────────────────────
# ヘルパー関数
# ────────────────────────────────────────────────
def build_tool_result(payload: dict[str, Any], *, is_error: bool) -> dict[str, Any]:
    """
    Agent SDK の MCP ツール戻り値を構築する。

    重要:
    - SDK が理解するのはトップレベルの `is_error`
    - ラボで学習する業務上のエラー表現は JSON 本文の `isError`
    - この 2 つを分離することで、SDK の型に従いつつ試験で問われる
      `isError` パターンもそのまま学べるようにする

    Args:
        payload: ツールの業務上の返却データ。JSON 文字列にして text block に格納する。
        is_error: SDK 向けのエラーフラグ。Claude がツール失敗として扱うかを決める。

    Returns:
        MCP ツール戻り値 dict:
        {
            "content": [{"type": "text", "text": "<JSON文字列>"}],
            "is_error": bool,
        }
    """
    return {
        "content": [
            {"type": "text", "text": json.dumps(payload, ensure_ascii=False)}
        ],
        "is_error": is_error,
    }


def normalize_value(obj: Any) -> tuple[Any, list[str]]:
    """再帰的にデータを正規化する。"""
    notes: list[str] = []

    def _normalize(value: Any) -> Any:
        if isinstance(value, dict):
            normalized: dict[str, Any] = {}
            for key, item in value.items():
                if (
                    key.endswith("_at")
                    and isinstance(item, int)
                    and 1_000_000_000 <= item <= 9_999_999_999
                ):
                    iso = datetime.datetime.fromtimestamp(
                        item,
                        tz=datetime.timezone.utc,
                    ).isoformat()
                    normalized[key] = iso
                    notes.append(f"{key}: {item} (Unix) → {iso} (ISO 8601)")
                elif key == "status_code" and isinstance(item, int):
                    description = STATUS_CODE_DESCRIPTIONS.get(item, "Unknown")
                    normalized[key] = item
                    normalized["status_code_description"] = description
                    notes.append(f'status_code: {item} → "{description}"')
                else:
                    normalized[key] = _normalize(item)
            return normalized
        if isinstance(value, list):
            return [_normalize(x) for x in value]
        return value

    return _normalize(obj), notes


# ────────────────────────────────────────────────
# ビジネスロジック本体
# decorated tool とは分離しておくと、ローカル検証しやすい
# ────────────────────────────────────────────────
def handle_get_customer(customer_id: str) -> dict[str, Any]:
    customer = CUSTOMERS_DB.get(customer_id)
    if not customer:
        return {
            "isError": True,
            "retryable": False,
            "error": f"Customer not found: {customer_id}",
        }

    session_state["customer_verified"] = True
    session_state["verified_customer_id"] = customer_id

    if LEARN_MODE:
        print(
            "\n  📌 [LEARN] get_customer 成功"
            "\n     session_state['customer_verified'] = True に設定"
            "\n     これにより process_refund の前提条件をコードで保証できる"
        )

    return {"isError": False, "customer": customer}


def handle_lookup_order(order_id: str) -> dict[str, Any]:
    """
    注文情報を生データのまま返す。

    created_at / delivered_at / status_code の正規化はツール本体では行わず、
    PostToolUse フックで一括実施する。これにより「取得」と「正規化」の
    関心を分離し、バックエンド差異の吸収をフック側に集約できる。
    """
    order = ORDERS_DB.get(order_id)
    if not order:
        return {
            "isError": True,
            "retryable": False,
            "error": f"Order not found: {order_id}",
        }
    return {"isError": False, "order": order}


def handle_process_refund(order_id: str, amount: float, reason: str) -> dict[str, Any]:
    """
    返金処理。

    Gate 1: 顧客確認の前提条件チェック
    Gate 2: 閾値チェック (通常は PreToolUse で先に止める。ここは防御的バックアップ)
    """
    if not session_state["customer_verified"]:
        if LEARN_MODE:
            print(
                "\n  📌 [LEARN] プログラム的前提条件ゲート発動"
                "\n     prompt で順序を指示するだけでは不十分なので、"
                "\n     process_refund 側でも customer_verified を確認している"
            )
        return {
            "isError": True,
            "retryable": True,
            "prerequisite_missing": "customer_verification",
            "error": (
                "PREREQUISITE GATE BLOCKED: process_refund requires get_customer "
                "to be called successfully first. 顧客確認が完了していません。"
            ),
        }

    order = ORDERS_DB.get(order_id)
    if not order:
        return {
            "isError": True,
            "retryable": False,
            "error": f"Order not found: {order_id}",
        }

    if not order["can_refund"]:
        return {
            "isError": True,
            "retryable": False,
            "error": (
                f"Order {order_id} is not eligible for refund "
                f"(status: {order['status']})"
            ),
        }

    if amount > REFUND_THRESHOLD:
        if LEARN_MODE:
            print(
                "\n  📌 [LEARN] バックアップ閾値チェック発動"
                "\n     通常は PreToolUse フックが先に止めるが、"
                "\n     ツール本体にも防御的チェックを残して二重で守る"
            )
        return {
            "isError": True,
            "retryable": False,
            "requires_human": True,
            "error": (
                f"Refund amount ¥{amount:,.0f} exceeds automatic processing threshold "
                f"of ¥{REFUND_THRESHOLD:,.0f}. Human review required."
            ),
        }

    print(
        f"  [REFUND EXECUTED] Order: {order_id}, Amount: ¥{amount:,.0f}, Reason: {reason}"
    )
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


def handle_escalate_to_human(
    reason: str,
    priority: str,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    print(f"\n  [ESCALATION] Priority: {priority.upper()}")
    print(f"  Reason: {reason}")

    if context:
        print("\n  ┌─────────────────────────────────────────────┐")
        print("  │        STRUCTURED HANDOFF SUMMARY           │")
        print("  ├─────────────────────────────────────────────┤")
        fields = [
            ("customer_id", "顧客ID"),
            ("order_id", "注文ID"),
            ("root_cause", "根本原因"),
            ("refund_amount", "返金金額"),
            ("recommended_action", "推奨アクション"),
            ("conversation_summary", "会話要約"),
        ]
        for key, label in fields:
            value = context.get(key, "(未設定)")
            if key == "refund_amount" and isinstance(value, (int, float)):
                value = f"¥{value:,.0f}"
            value_str = str(value)
            if len(value_str) > 40:
                value_str = value_str[:37] + "..."
            print(f"  │ {label:<12}: {value_str:<32}│")
        print("  └─────────────────────────────────────────────┘")

        missing = [key for key, _ in fields if key not in context]
        if missing:
            print(f"\n  ⚠️  Missing recommended handoff fields: {', '.join(missing)}")
    else:
        print("  ⚠️  No structured context provided for handoff.")

    return {
        "isError": False,
        "escalation_id": "ESC-2024-001",
        "status": "escalated",
        "message": f"ケースをエスカレーションしました。(優先度: {priority})",
    }


# ────────────────────────────────────────────────
# Claude Agent SDK カスタムツール
# ────────────────────────────────────────────────
@tool(
    "get_customer",
    (
        "顧客IDで顧客情報を取得します。"
        "顧客IDは 'CUST-' で始まる文字列です。"
        "返金処理の前に必ずこのツールを呼び出して顧客を確認してください。"
        "見つからない場合は isError を含むエラー JSON を返します。"
    ),
    {"customer_id": str},
)
async def get_customer_tool(args: dict[str, Any]) -> dict[str, Any]:
    payload = handle_get_customer(args["customer_id"])
    return build_tool_result(payload, is_error=payload["isError"])


@tool(
    "lookup_order",
    (
        "注文IDで注文情報を取得します。"
        "注文IDは 'ORD-' で始まる文字列です。"
        "返金処理の前に必ずこのツールで注文を確認してください。"
        "見つからない場合は isError を含むエラー JSON を返します。"
    ),
    {"order_id": str},
)
async def lookup_order_tool(args: dict[str, Any]) -> dict[str, Any]:
    payload = handle_lookup_order(args["order_id"])
    return build_tool_result(payload, is_error=payload["isError"])


@tool(
    "process_refund",
    (
        "注文の返金処理を行います。"
        "必ず get_customer と lookup_order の後に呼び出してください。"
        f"返金額が ¥{REFUND_THRESHOLD:,.0f} を超える場合、PreToolUse フックがツール実行をブロックします。"
        "その場合は escalate_to_human を使ってください。"
    ),
    {"order_id": str, "amount": float, "reason": str},
)
async def process_refund_tool(args: dict[str, Any]) -> dict[str, Any]:
    payload = handle_process_refund(
        order_id=args["order_id"],
        amount=args["amount"],
        reason=args["reason"],
    )
    return build_tool_result(payload, is_error=payload["isError"])


@tool(
    "escalate_to_human",
    (
        "人間のオペレーターにケースをエスカレーションします。"
        "context には customer_id, order_id, root_cause, refund_amount, "
        "recommended_action, conversation_summary を含めてください。"
        "priority は low/normal/high/urgent のいずれかです。"
    ),
    {"reason": str, "priority": str, "context": dict},
)
async def escalate_to_human_tool(args: dict[str, Any]) -> dict[str, Any]:
    payload = handle_escalate_to_human(
        reason=args["reason"],
        priority=args["priority"],
        context=args.get("context"),
    )
    return build_tool_result(payload, is_error=False)


# ────────────────────────────────────────────────
# Agent SDK Hooks
# ────────────────────────────────────────────────
async def pre_tool_use_refund_check(
    input_data: HookInput,
    tool_use_id: str | None,
    context: HookContext,
) -> HookJSONOutput:
    """返金閾値を deterministic にブロックする PreToolUse フック。"""
    tool_input = input_data.get("tool_input", {})
    amount = tool_input.get("amount", 0)

    if LEARN_MODE:
        print(
            f"\n  📌 [LEARN] PreToolUse フック発動: amount=¥{amount:,.0f}"
            "\n     なぜフックか: prompt 指示ではなく実行前フックで副作用を止めるため"
        )

    if amount > REFUND_THRESHOLD:
        if VERBOSE_MODE:
            print(
                f"\n  🚫 [HOOK BLOCKED] 返金額 ¥{amount:,.0f} が閾値 "
                f"¥{REFUND_THRESHOLD:,.0f} を超過"
            )
        return {
            "systemMessage": "🚫 返金処理はポリシーによりブロックされました",
            "reason": "高額返金は人手確認が必要です",
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": (
                    f"返金額 ¥{amount:,.0f} が自動処理閾値 ¥{REFUND_THRESHOLD:,.0f} を超過しています。"
                    "escalate_to_human を使用してください。"
                ),
                "additionalContext": (
                    "高額返金は人手レビュー対象です。"
                    " 必要な context を付けて escalate_to_human を呼び出してください。"
                ),
            },
        }

    if LEARN_MODE:
        print("     → 閾値以内のため process_refund を許可")

    return {}


async def post_tool_use_audit_and_normalize(
    input_data: HookInput,
    tool_use_id: str | None,
    context: HookContext,
) -> HookJSONOutput:
    """
    PostToolUse フック。

    役割:
    - 監査ログ的な出力
    - 返ってきた MCP ツール結果を正規化して `updatedMCPToolOutput` で差し替える

    公式 SDK の型上、PostToolUse の書き換えは `updatedMCPToolOutput` を使う。
    このラボでは学習効果を優先し、正規化が意味を持つ lookup_order / process_refund
    のみに matcher を絞っている。全ツール一律ではなく「どの出力を正規化すべきか」
    を設計判断として意識するため。
    """
    tool_name = input_data.get("tool_name", "")
    tool_response = input_data.get("tool_response")
    timestamp = datetime.datetime.now(tz=datetime.timezone.utc).isoformat()

    if VERBOSE_MODE:
        print(f"  [AUDIT] [{timestamp}] tool_used={tool_name}")

    if not isinstance(tool_response, dict):
        return {}

    content = tool_response.get("content")
    if not isinstance(content, list) or not content:
        return {}

    first_block = content[0]
    if not isinstance(first_block, dict) or first_block.get("type") != "text":
        return {}

    raw_text = first_block.get("text")
    if not isinstance(raw_text, str):
        return {}

    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError:
        return {}

    normalized, notes = normalize_value(payload)
    if not notes:
        return {}

    updated_block = dict(first_block)
    updated_block["text"] = json.dumps(normalized, ensure_ascii=False)
    updated_output = dict(tool_response)
    updated_output["content"] = [updated_block, *content[1:]]

    if LEARN_MODE:
        print(
            f"\n  📌 [LEARN] PostToolUse フック発動: {tool_name}"
            "\n     なぜ PostToolUse か: ツール本体から正規化ロジックを分離し、"
            "\n     バックエンド差異をエージェントに見せる直前で統一できる"
        )
        for note in notes:
            print(f"       • {note}")

    return {
        "reason": "ツール結果を正規化して Claude に返しました",
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": "ツール結果は正規化済みです。ISO 8601 と説明付き status_code を優先してください。",
            "updatedMCPToolOutput": updated_output,
        },
    }


# ────────────────────────────────────────────────
# メッセージ表示
# ────────────────────────────────────────────────
def process_message(message: Any) -> str | None:
    """SDK メッセージを表示しつつ、最終応答候補のテキストを返す。"""
    final_text: str | None = None

    if isinstance(message, AssistantMessage):
        for block in message.content:
            if isinstance(block, TextBlock):
                final_text = block.text
                if VERBOSE_MODE:
                    print(f"  Claude: {block.text}")
            elif isinstance(block, ToolUseBlock) and VERBOSE_MODE:
                print(f"  Tool: {block.name}({json.dumps(block.input, ensure_ascii=False)})")
    elif isinstance(message, ResultMessage):
        if VERBOSE_MODE:
            print(f"  [RESULT] stop_reason={message.stop_reason}, turns={message.num_turns}")
        if message.result and not final_text:
            final_text = message.result
    elif isinstance(message, ToolResultBlock) and VERBOSE_MODE:
        print(f"  ToolResult: {message.content}")

    return final_text


# ────────────────────────────────────────────────
# Agent 実行
# ────────────────────────────────────────────────
async def run_support_agent(user_message: str) -> str:
    """
    Claude Agent SDK で顧客サポートエージェントを実行する。

    旧実装では stop_reason を自分で見て while ループを制御していたが、
    Agent SDK では SDK がツール呼び出しとループを管理する。
    その代わり `max_turns` を安全ネットとして設定する。
    """
    session_state["customer_verified"] = False
    session_state["verified_customer_id"] = None

    server = create_sdk_mcp_server(
        name="support",
        version="1.0.0",
        tools=[
            get_customer_tool,
            lookup_order_tool,
            process_refund_tool,
            escalate_to_human_tool,
        ],
    )

    system_prompt = """あなたは顧客サポートエージェントです。

## 役割
顧客の問い合わせに対して、適切なツールを使って問題を解決します。

## ツール使用の原則
1. 返金処理の前に必ず get_customer で顧客確認を行う
2. 注文操作の前に必ず lookup_order で注文を確認する
3. 自動返金できる場合だけ process_refund を使う
4. 自動処理できない場合は escalate_to_human を使う

## エスカレーション時の context
customer_id, order_id, root_cause, refund_amount, recommended_action, conversation_summary を含めること。

## エラー処理
- ツール本文の JSON に isError: true が含まれていたら、内容に応じて対処する
- 高額返金で process_refund がブロックされた場合は escalate_to_human を使う
- 顧客には丁寧かつ明確に説明する
"""

    options = ClaudeAgentOptions(
        system_prompt=system_prompt,
        max_turns=10,
        mcp_servers={"support": server},
        allowed_tools=[
            "mcp__support__get_customer",
            "mcp__support__lookup_order",
            "mcp__support__process_refund",
            "mcp__support__escalate_to_human",
        ],
        hooks={
            "PreToolUse": [
                HookMatcher(
                    matcher="mcp__support__process_refund",
                    hooks=[pre_tool_use_refund_check],
                )
            ],
            "PostToolUse": [
                HookMatcher(
                    matcher="mcp__support__lookup_order|mcp__support__process_refund",
                    hooks=[post_tool_use_audit_and_normalize],
                )
            ],
        },
    )

    if LEARN_MODE:
        print(
            "\n  📌 [LEARN] Agent SDK 実行開始"
            "\n     旧実装の stop_reason ベースの手動ループは SDK 内部に移った"
            "\n     ただし max_turns は安全ネットとして依然重要"
        )

    final_response = "(応答なし)"
    async with ClaudeSDKClient(options=options) as client:
        await client.query(user_message)
        async for message in client.receive_response():
            maybe_text = process_message(message)
            if maybe_text:
                final_response = maybe_text

    return final_response


# ────────────────────────────────────────────────
# アンチパターンのデモ
# ────────────────────────────────────────────────
def run_support_agent_with_antipatterns() -> None:
    """API 呼び出し不要のアンチパターンデモ。"""
    sep = "─" * 60

    print(f"\n{'='*60}")
    print("  アンチパターン デモ (API 不要・シミュレーション)")
    print(f"{'='*60}")

    print(f"\n{sep}")
    print("【アンチパターン 1】テキスト文字列でループ終了を判断する")
    print(sep)
    print("\n❌ WRONG: assistant のテキストに '処理完了' が含まれているかで終了を判断\n")

    simulated_responses = [
        ("返金処理を完了しました。", "本当に完了"),
        ("処理完了しましたが、エスカレーションが必要です。", "まだ作業が必要"),
        ("処理完了できませんでした。", "失敗したのに '処理完了' を含む"),
        ("Refund processing done.", "英語応答は検知されない"),
        ("処 理 完 了", "スペース入りは検知されない"),
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
        "\n     stop_reason は API / SDK が提供する意味的シグナル。"
    )

    print(f"\n{sep}")
    print("【アンチパターン 2】max_turns=2 を主たる停止機構にする")
    print(sep)
    print("\n❌ WRONG: 低すぎる反復上限に頼ると複数ツール呼び出しで途中終了する\n")

    typical_tool_sequence = [
        ("get_customer", "顧客情報取得"),
        ("lookup_order", "注文情報取得"),
        ("process_refund", "返金処理"),
        ("escalate_to_human", "エスカレーション"),
    ]
    for i, (tool_name, desc) in enumerate(typical_tool_sequence, 1):
        too_early = "⛔ 反復上限で強制終了!" if i > 2 else "✅"
        print(f"    Turn {i}: {tool_name} ({desc}) {too_early}")

    print(
        "\n  💡 問題: Agent SDK では max_turns は安全ネットであり、主要停止機構ではない。"
        "\n     実際の終了判定は stop_reason / SDK の完了判定に委ねるべき。"
    )

    print(f"\n{sep}")
    print("【アンチパターン 3】プロンプトのみで処理順序を制御しようとする")
    print(sep)
    print(
        "\n❌ WRONG: '必ず get_customer を先に呼ぶ' と prompt に書くだけでは"
        "\n   確率的にしか守られない\n"
    )

    prompt_only_risks = [
        ("モデルのサンプリング変動", "同じ prompt でも毎回同じ順序とは限らない"),
        ("コンテキスト長の増加", "長い会話では冒頭指示が軽視される"),
        ("競合する指示", "複数ルールの優先順位が不安定になる"),
        ("将来のモデル更新", "バージョン差分で挙動が変わりうる"),
        ("プロンプトインジェクション", "悪意のある入力で指示が崩れる可能性がある"),
    ]
    for risk, description in prompt_only_risks:
        print(f"  ⚠️  {risk}: {description}")

    print(
        "\n✅ CORRECT: session_state で前提条件をプログラム的に強制する"
        "\n   1. customer_verified=False で初期化"
        "\n   2. get_customer 成功時に True に更新"
        "\n   3. process_refund は False なら isError を返す"
        "\n   4. prompt に依存せず順序保証できる"
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
        "message": "",
    },
}


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Customer Support Agent Lab (Claude Agent SDK)",
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
        help="実行するシナリオ (default: normal)",
    )
    parser.add_argument(
        "--scenario",
        choices=list(SCENARIOS.keys()),
        dest="scenario_legacy",
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--learn", action="store_true", help="学習ノートを表示する")
    parser.add_argument("--quiet", action="store_true", help="詳細出力を抑制")
    args = parser.parse_args()

    global LEARN_MODE, VERBOSE_MODE
    LEARN_MODE = args.learn
    VERBOSE_MODE = not args.quiet

    mode = args.scenario_legacy or args.mode

    if mode == "antipatterns":
        run_support_agent_with_antipatterns()
        return

    scenario = SCENARIOS[mode]
    print(f"\n{'='*60}")
    print(f"シナリオ: {scenario['description']}")
    if LEARN_MODE:
        print("学習モード: ON (--learn)")
    print(f"{'='*60}")
    print(f"顧客メッセージ:\n  {scenario['message']}")
    print(f"{'='*60}")

    response = await run_support_agent(scenario["message"])

    print(f"\n{'='*60}")
    print("エージェント最終応答:")
    print(f"{'='*60}")
    print(response)


if __name__ == "__main__":
    anyio.run(main)
