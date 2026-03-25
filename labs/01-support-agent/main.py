"""
Lab 01: Customer Support Agent (Claude Agent SDK 版)

学習目標:
- Agent SDK の @tool デコレータによるカスタムツール定義
- MCP サーバーとしてのツール登録と allowed_tools による制御
- PreToolUse フックによるポリシー強制 (返金閾値ブロック)
- PostToolUse フックによるデータ正規化 (タイムスタンプ変換、ステータスコード説明)
- セッション状態によるプログラム的前提条件ゲート (Gate)
- 構造化ハンドオフサマリーによるエスカレーション
- Prompt vs Code での制約の使い分け
- アンチパターンとその問題点の理解

★ Anthropic Client SDK → Claude Agent SDK への移行ポイント:

  Client SDK (旧):
    - 手動 while ループ + stop_reason で agentic loop を制御
    - ツールは JSON スキーマ (dict) で定義
    - フックは自前の Python 関数で実装

  Agent SDK (新):
    - SDK 内部でエージェントループを管理 (max_turns で安全ネット)
    - @tool デコレータ + create_sdk_mcp_server() で MCP ツールとして登録
    - HookMatcher で PreToolUse / PostToolUse をネイティブに設定
    - async/await + anyio で非同期実行

★ .claude/hooks/ のシェルスクリプトと .claude/settings.json は
  Claude Code 統合用のフック設定です (この Python スクリプトとは独立)。
  参照: https://platform.claude.com/docs/en/agent-sdk/hooks
"""

import json
import argparse
import datetime
from typing import Any

import anyio

from claude_agent_sdk import (
    tool,
    create_sdk_mcp_server,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    ToolResultBlock,
)


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
# モジュールレベルフラグ
# フック・ツールから参照するためモジュールスコープに配置
# ────────────────────────────────────────────────
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
# データ正規化ヘルパー
#
# PostToolUse フックで使用する共通ロジック。
# - Unix タイムスタンプ (_at キー) → ISO 8601 形式
# - 数値ステータスコード → 人間可読な説明を追加
# ────────────────────────────────────────────────

def normalize_value(obj: Any) -> tuple[Any, list[str]]:
    """
    再帰的にデータを正規化する。

    Returns:
        tuple: (正規化されたオブジェクト, 正規化内容の説明リスト)
    """
    normalizations: list[str] = []

    def _normalize(o: Any) -> Any:
        if isinstance(o, dict):
            new = {}
            for key, val in o.items():
                # Unix タイムスタンプの変換 (epoch 秒 / _at で終わるキー)
                if (
                    key.endswith("_at")
                    and isinstance(val, int)
                    and 1_000_000_000 <= val <= 9_999_999_999
                ):
                    iso = datetime.datetime.fromtimestamp(
                        val, tz=datetime.timezone.utc
                    ).isoformat()
                    new[key] = iso
                    normalizations.append(
                        f"{key}: {val} (Unix) → {iso} (ISO 8601)"
                    )
                # 数値ステータスコードに説明を追加
                elif key == "status_code" and isinstance(val, int):
                    description = STATUS_CODE_DESCRIPTIONS.get(val, "Unknown")
                    new[key] = val
                    new["status_code_description"] = description
                    normalizations.append(
                        f'status_code: {val} → "{description}"'
                    )
                else:
                    new[key] = _normalize(val)
            return new
        elif isinstance(o, list):
            return [_normalize(item) for item in o]
        return o

    normalized = _normalize(obj)
    return normalized, normalizations


# ────────────────────────────────────────────────
# カスタムツール定義 (@tool デコレータ)
#
# ★ Agent SDK のツール定義方法:
#   @tool("ツール名", "説明文", {パラメータ名: 型})
#   async def ツール関数(args: dict) -> dict:
#       return {"content": [{"type": "text", "text": "結果"}]}
#
# ★ Client SDK との違い:
#   - Client SDK: JSON スキーマ dict + execute_tool ディスパッチャー
#   - Agent SDK: @tool デコレータで宣言的に定義 → MCP サーバーに自動登録
#   - Agent SDK: ツール呼び出しは SDK が自動的にディスパッチ
# ────────────────────────────────────────────────

@tool(
    "get_customer",
    (
        "顧客IDで顧客情報を取得します。"
        "顧客IDは 'CUST-' で始まる文字列です。"
        "顧客が存在しない場合は isError: true を返します。"
        "このツールは読み取り専用で、データを変更しません。"
        "返金処理の前に必ずこのツールを呼び出して顧客を確認してください。"
    ),
    {"customer_id": str},
)
async def get_customer_tool(args: dict[str, Any]) -> dict[str, Any]:
    """顧客情報取得 (read-only)"""
    customer_id = args["customer_id"]
    customer = CUSTOMERS_DB.get(customer_id)

    if not customer:
        return {
            "isError": True,
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {
                            "isError": True,
                            "retryable": False,
                            "error": f"Customer not found: {customer_id}",
                        },
                        ensure_ascii=False,
                    ),
                }
            ],
        }

    # ★ セッション状態を更新 (プログラム的前提条件ゲートのための状態追跡)
    session_state["customer_verified"] = True
    session_state["verified_customer_id"] = customer_id

    if LEARN_MODE:
        print(
            f"\n  📌 [LEARN] get_customer 成功: session_state['customer_verified'] = True"
            f"\n     なぜ状態追跡か: process_refund の前提条件をコードレベルで強制するため"
        )

    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(
                    {"isError": False, "customer": customer},
                    ensure_ascii=False,
                ),
            }
        ],
    }


@tool(
    "lookup_order",
    (
        "注文IDで注文情報を取得します。"
        "注文IDは 'ORD-' で始まる文字列です。"
        "返金処理の前に必ずこのツールで注文を確認してください。"
        "注文が存在しない場合は isError: true を返します。"
        "このツールは読み取り専用で、データを変更しません。"
    ),
    {"order_id": str},
)
async def lookup_order_tool(args: dict[str, Any]) -> dict[str, Any]:
    """注文情報取得 (read-only) — 生データを返し PostToolUse で正規化"""
    order_id = args["order_id"]
    order = ORDERS_DB.get(order_id)

    if not order:
        return {
            "isError": True,
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {
                            "isError": True,
                            "retryable": False,
                            "error": f"Order not found: {order_id}",
                        },
                        ensure_ascii=False,
                    ),
                }
            ],
        }

    # ★ 注文データは生のまま返す (PostToolUse フックで正規化される)
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(
                    {"isError": False, "order": order},
                    ensure_ascii=False,
                ),
            }
        ],
    }


@tool(
    "process_refund",
    (
        "注文の返金処理を行います。"
        "事前に get_customer で顧客を確認してから呼び出してください。"
        "事前に lookup_order で注文を確認してから呼び出してください。"
        f"返金金額が ¥{REFUND_THRESHOLD:,.0f} を超える場合は PreToolUse フックが"
        "ツール実行をブロックします。その場合は escalate_to_human を呼び出してください。"
    ),
    {"order_id": str, "amount": float, "reason": str},
)
async def process_refund_tool(args: dict[str, Any]) -> dict[str, Any]:
    """
    返金処理

    ★ Gate 1 (コード内): 顧客確認の前提条件チェック (プログラム的順序強制)
    ★ Gate 2 (PreToolUse フック): 閾値チェック — フックがブロックするため
      ここまで到達する場合は閾値以内が保証される
    ★ Gate 2 バックアップ (コード内): フック無効時の防御策
    """
    order_id = args["order_id"]
    amount = args["amount"]
    reason = args["reason"]

    # ★ PREREQUISITE GATE: 顧客確認が完了しているかチェック
    if not session_state["customer_verified"]:
        if LEARN_MODE:
            print(
                "\n  📌 [LEARN] プログラム的前提条件ゲート発動!"
                "\n     なぜプログラム的前提条件か: prompt で『先に顧客確認して』と指示しても"
                "\n     確率的にしか守られない。code で順序を強制する"
            )
        return {
            "isError": True,
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {
                            "isError": True,
                            "retryable": True,
                            "prerequisite_missing": "customer_verification",
                            "error": (
                                "PREREQUISITE GATE BLOCKED: process_refund requires "
                                "get_customer to be called successfully first. "
                                "顧客確認が完了していません。先に get_customer を呼び出してください。"
                            ),
                        },
                        ensure_ascii=False,
                    ),
                }
            ],
        }

    order = ORDERS_DB.get(order_id)
    if not order:
        return {
            "isError": True,
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {
                            "isError": True,
                            "retryable": False,
                            "error": f"Order not found: {order_id}",
                        },
                        ensure_ascii=False,
                    ),
                }
            ],
        }

    if not order["can_refund"]:
        return {
            "isError": True,
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {
                            "isError": True,
                            "retryable": False,
                            "error": (
                                f"Order {order_id} is not eligible for refund "
                                f"(status: {order['status']})"
                            ),
                        },
                        ensure_ascii=False,
                    ),
                }
            ],
        }

    # ★ 閾値チェック (バックアップ: 通常は PreToolUse フックが事前にブロック)
    if amount > REFUND_THRESHOLD:
        if LEARN_MODE:
            print(
                "\n  📌 [LEARN] 閾値バックアップチェック発動"
                "\n     通常は PreToolUse フックがこの呼び出しを事前にブロックする"
                "\n     ここまで到達した場合はフック無効時の防御策として機能"
            )
        return {
            "isError": True,
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {
                            "isError": True,
                            "retryable": False,
                            "requires_human": True,
                            "error": (
                                f"Refund amount ¥{amount:,.0f} exceeds automatic processing "
                                f"threshold of ¥{REFUND_THRESHOLD:,.0f}. Human review required."
                            ),
                        },
                        ensure_ascii=False,
                    ),
                }
            ],
        }

    # 返金処理実行 (擬似実装)
    print(
        f"  [REFUND EXECUTED] Order: {order_id}, "
        f"Amount: ¥{amount:,.0f}, Reason: {reason}"
    )

    result = {
        "isError": False,
        "refund_id": f"REF-{order_id}-001",
        "order_id": order_id,
        "amount": amount,
        "status": "completed",
        "status_code": 200,
        "processed_at": int(
            datetime.datetime.now(tz=datetime.timezone.utc).timestamp()
        ),
        "message": f"返金処理が完了しました。¥{amount:,.0f} を返金いたします。",
    }

    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(result, ensure_ascii=False),
            }
        ],
    }


@tool(
    "escalate_to_human",
    (
        "人間のオペレーターにケースをエスカレーションします。"
        "以下の場合に使用してください: "
        "1) 返金金額が自動処理閾値を超える場合, "
        "2) 顧客が解決できない問題を抱えている場合, "
        "3) 不正利用の疑いがある場合。"
        "エスカレーション後、このセッションは終了します。"
        "context オブジェクトには以下のフィールドを含めること: "
        "customer_id (顧客ID), order_id (注文ID), root_cause (根本原因), "
        "refund_amount (返金金額), recommended_action (推奨アクション), "
        "conversation_summary (会話要約)。"
        "priority は 'low', 'normal', 'high', 'urgent' のいずれか。"
    ),
    {"reason": str, "priority": str, "context": dict},
)
async def escalate_to_human_tool(args: dict[str, Any]) -> dict[str, Any]:
    """
    人間へのエスカレーション

    ★ 構造化引き継ぎ: context に必要なフィールドが揃っているか確認し、
      フォーマットされたハンドオフサマリーを出力する
    """
    reason = args["reason"]
    priority = args["priority"]
    context = args.get("context")

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
            value_str = str(value)
            if len(value_str) > 40:
                value_str = value_str[:37] + "..."
            print(f"  │ {label:<12}: {value_str:<32}│")
        print("  └─────────────────────────────────────────────┘")

        # 設定されていない推奨フィールドを警告
        missing = [k for k, _ in fields if k not in context]
        if missing:
            print(
                f"\n  ⚠️  Missing recommended handoff fields: "
                f"{', '.join(missing)}"
            )
    else:
        print("  ⚠️  No structured context provided for handoff.")

    result = {
        "isError": False,
        "escalation_id": "ESC-2024-001",
        "status": "escalated",
        "message": (
            f"ケースをエスカレーションしました。"
            f"担当者が対応いたします。(優先度: {priority})"
        ),
    }

    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(result, ensure_ascii=False),
            }
        ],
    }


# ────────────────────────────────────────────────
# Agent SDK フック定義
#
# ★ PreToolUse: ツール実行前にポリシーをチェック
#   - deny を返すとツール呼び出しがブロック → モデルにブロック理由を通知
#   - {} (空 dict) を返すとツール実行を許可
#
# ★ PostToolUse: ツール実行後にデータ処理
#   - ツールは既に実行済みのためブロック不可
#   - データ正規化・監査ログ・通知に使用
#   - hookSpecificOutput.toolResult で正規化済みデータを返す
#
# ★ .claude/settings.json + シェルスクリプトでの設定は
#   Claude Code 統合用 (.claude/hooks/ を参照)
# ────────────────────────────────────────────────

async def pre_tool_use_refund_check(
    input_data: dict[str, Any],
    tool_use_id: str,
    context: Any,
) -> dict[str, Any]:
    """
    PreToolUse フック: 返金額の閾値チェック

    ★ ツール実行前にブロックできるため、不正な副作用を防止
    ★ .claude/hooks/pre_tool_use_refund.sh と同等の Python 実装
    ★ HookMatcher(matcher="mcp__support__process_refund") で
      process_refund 呼び出し時のみ発火する
    """
    tool_name = input_data.get("tool_name", "")
    tool_input = input_data.get("tool_input", {})

    # process_refund 以外はスキップ (matcher で絞っているが念のため)
    if "process_refund" not in tool_name:
        return {}

    amount = tool_input.get("amount", 0)

    if LEARN_MODE:
        print(
            f"\n  📌 [LEARN] PreToolUse フック発動: process_refund"
            f"\n     返金額: ¥{amount:,.0f} / 閾値: ¥{REFUND_THRESHOLD:,.0f}"
            f"\n     なぜ PreToolUse か: ツール実行前にブロックできるため、"
            f"\n     不正な返金処理の実行自体を防止する"
        )

    if amount > REFUND_THRESHOLD:
        if VERBOSE_MODE:
            print(
                f"\n  🚫 [HOOK BLOCKED] 返金額 ¥{amount:,.0f} が閾値 "
                f"¥{REFUND_THRESHOLD:,.0f} を超過 → ツール実行をブロック"
            )
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": (
                    f"返金額 ¥{amount:,.0f} が自動処理閾値 "
                    f"¥{REFUND_THRESHOLD:,.0f} を超過しています。"
                    f"escalate_to_human を使用して人間のオペレーターに"
                    f"エスカレーションしてください。"
                ),
            }
        }

    if LEARN_MODE:
        print(f"     → 閾値以内のため許可")

    return {}


async def post_tool_use_normalize(
    input_data: dict[str, Any],
    tool_use_id: str,
    context: Any,
) -> dict[str, Any]:
    """
    PostToolUse フック: データ正規化 + 監査ログ

    ★ 全ツール結果に対して実行 (HookMatcher の matcher="" で全マッチ)
    ★ .claude/hooks/post_tool_use_audit.sh と同等 + データ正規化

    正規化内容:
    - Unix タイムスタンプ → ISO 8601 形式
    - 数値ステータスコード → 人間可読な説明を追加

    ★ なぜ PostToolUse か:
      異なるバックエンドシステムから返ってくるデータ形式を統一することで、
      モデルが一貫した形式で情報を処理できるようにする。
      ツール本体のコードから正規化ロジックを分離し、関心の分離を実現する。
    """
    tool_name = input_data.get("tool_name", "")

    # ── 監査ログ (全ツール呼び出しを記録) ──
    timestamp = datetime.datetime.now(tz=datetime.timezone.utc).isoformat()
    if VERBOSE_MODE:
        print(f"  [AUDIT] [{timestamp}] tool_used={tool_name}")

    # ── ツール結果の取得・パース ──
    tool_result = input_data.get("tool_result", "")
    if isinstance(tool_result, str):
        try:
            result_data = json.loads(tool_result)
        except (json.JSONDecodeError, TypeError):
            return {}
    elif isinstance(tool_result, dict):
        result_data = tool_result
    else:
        return {}

    # ── データ正規化 ──
    normalized, normalizations = normalize_value(result_data)

    if normalizations:
        if LEARN_MODE:
            print(
                f"\n  📌 [LEARN] PostToolUse フック: データ正規化実行"
                f"\n     ツール: {tool_name}"
                f"\n     なぜ PostToolUse か: ツール本体のコードから正規化ロジックを分離し、"
                f"\n     異なるシステムからのデータ形式を一箇所で統一管理できる"
            )
            print(f"     正規化した項目:")
            for note in normalizations:
                print(f"       • {note}")

        # ★ 正規化結果を hookSpecificOutput で返す
        # Agent SDK はこの結果でツール結果を更新する
        return {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "toolResult": json.dumps(normalized, ensure_ascii=False),
            }
        }
    else:
        if LEARN_MODE:
            print(
                f"  📌 [LEARN] PostToolUse: 正規化対象なし "
                f"(ツール: {tool_name})"
            )

    return {}


# ────────────────────────────────────────────────
# エージェント実行
#
# ★ Agent SDK と Client SDK の agentic loop の違い:
#
#   Client SDK (旧):
#     while iteration < max_iterations:
#         response = client.messages.create(...)
#         if response.stop_reason == "end_turn":  ← 手動判定
#             break
#         elif response.stop_reason == "tool_use":
#             execute_tool(...)  ← 手動ディスパッチ
#
#   Agent SDK (新):
#     async with ClaudeSDKClient(options=options) as client:
#         await client.query(message)
#         async for msg in client.receive_response():
#             # SDK がツール呼び出し・ループ制御を自動管理
#             # max_turns は安全ネットとして機能
# ────────────────────────────────────────────────

async def run_support_agent(user_message: str) -> str:
    """
    顧客サポートエージェントを Agent SDK で実行する。

    ★ SDK が内部でエージェントループを管理:
      - ツール呼び出しの自動ディスパッチ
      - PreToolUse / PostToolUse フックの自動実行
      - max_turns による安全ネット (無限ループ防止)
      - 処理完了の自動判定 (Client SDK の stop_reason == 'end_turn' に相当)
    """
    # セッション状態をリセット
    session_state["customer_verified"] = False
    session_state["verified_customer_id"] = None

    # ★ MCP サーバーとしてツールを登録
    # @tool デコレータで定義した関数を create_sdk_mcp_server() に渡す
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

    # ★ Agent SDK オプション設定
    options = ClaudeAgentOptions(
        system_prompt=system_prompt,
        max_turns=10,  # ★ 安全ネット (無限ループ防止、主要停止は SDK 内部判断)
        mcp_servers={"support": server},
        allowed_tools=[
            # ★ MCP ツール名は "mcp__{サーバー名}__{ツール名}" 形式
            "mcp__support__get_customer",
            "mcp__support__lookup_order",
            "mcp__support__process_refund",
            "mcp__support__escalate_to_human",
        ],
        hooks={
            # ★ PreToolUse: process_refund 呼び出し時に閾値チェック
            "PreToolUse": [
                HookMatcher(
                    matcher="mcp__support__process_refund",
                    hooks=[pre_tool_use_refund_check],
                )
            ],
            # ★ PostToolUse: 全ツール結果をデータ正規化 + 監査ログ
            #   matcher="" で全ツールにマッチ
            "PostToolUse": [
                HookMatcher(
                    matcher="",
                    hooks=[post_tool_use_normalize],
                )
            ],
        },
    )

    if LEARN_MODE:
        print(
            "\n  📌 [LEARN] Agent SDK セットアップ完了"
            "\n     ・@tool デコレータで定義した 4 ツールを MCP サーバーとして登録"
            "\n     ・allowed_tools で使用可能なツールを明示的に制限"
            "\n     ・PreToolUse フックで返金閾値ポリシーを設定"
            "\n     ・PostToolUse フックでデータ正規化 + 監査ログを設定"
            "\n     ・max_turns=10 は安全ネット (主要停止は SDK 内部判断)"
            "\n"
            "\n     Client SDK との違い:"
            "\n     ・手動 while ループ不要 → SDK がループを内部管理"
            "\n     ・stop_reason の手動チェック不要 → SDK が自動判定"
            "\n     ・ツールディスパッチャー不要 → SDK が自動ディスパッチ"
        )

    final_response = "(応答なし)"
    tool_call_count = 0

    # ★ Agent SDK のエージェントループ
    async with ClaudeSDKClient(options=options) as client:
        await client.query(user_message)

        if LEARN_MODE:
            print(
                "\n  📌 [LEARN] Agent SDK のエージェントループ開始"
                "\n     ・SDK が内部でツール呼び出しとレスポンス処理を管理"
                "\n     ・Client SDK と異なり、手動の while ループは不要"
                "\n     ・receive_response() でメッセージストリームを非同期受信"
            )

        async for msg in client.receive_response():
            # ── AssistantMessage: モデルからの応答 ──
            if isinstance(msg, AssistantMessage) and hasattr(msg, "content"):
                for block in msg.content:
                    if isinstance(block, ToolUseBlock):
                        tool_call_count += 1
                        if VERBOSE_MODE:
                            tool_name = getattr(block, "name", "unknown")
                            tool_input = getattr(block, "input", {})
                            print(f"\n--- Tool Call {tool_call_count} ---")
                            print(
                                f"  Tool: {tool_name}"
                                f"({json.dumps(tool_input, ensure_ascii=False)})"
                            )
                    elif isinstance(block, TextBlock):
                        text = getattr(block, "text", "")
                        if text:
                            final_response = text

            # ── ResultMessage: エージェントループ完了 ──
            elif isinstance(msg, ResultMessage):
                if LEARN_MODE:
                    print(
                        "\n  📌 [LEARN] ResultMessage 受信 → エージェントループ完了"
                        "\n     Agent SDK が内部的に処理完了を判断した"
                        "\n     (Client SDK の stop_reason == 'end_turn' に相当)"
                        "\n     手動で stop_reason をチェックする必要はない"
                    )

    return final_response


# ────────────────────────────────────────────────
# アンチパターンのデモ
#
# API 呼び出しなし・自己完結のシミュレーション。
# Agent SDK を使っていても、これらの概念の理解は重要。
# ────────────────────────────────────────────────

def run_support_agent_with_antipatterns() -> None:
    """
    アンチパターンのデモ (API 呼び出しなし・自己完結)

    Domain 1 試験要件:
    - Task 1.3: stop_reason / ループ終了シグナルを使った正しいループ制御
    - Task 1.4: プログラム的な制約 vs プロンプトベースの制約

    ここでは実際に壊れる様子をシミュレートして学ぶ。

    ★ Agent SDK を使う場合でも、これらのアンチパターンを理解することは重要。
      SDK が内部で正しい制御を行っているが、カスタム実装や他の
      フレームワークを使う際にこれらの問題に直面する可能性がある。
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
        false_positive = nl_detected and (
            "エスカレ" in text or "できませんでした" in text
        )
        false_negative = not nl_detected and intent == "本当に完了"
        intent_note = (
            "⚠️ 誤判断!"
            if (false_positive or false_negative)
            else "OK"
        )
        print(f"  {text:<40} {marker:^10} {intent_note}")

    print(
        "\n  💡 問題: モデルの出力テキストは毎回同じとは限らない。"
        "\n     言語・表現のバリエーションで判定が壊れる。"
    )

    print(
        "\n✅ CORRECT:"
        "\n  Client SDK: stop_reason == 'end_turn' でループ終了を判断する"
        "\n  Agent SDK:  SDK が内部でループ終了を自動判定する (ResultMessage)\n"
    )
    correct_signals = [
        ("Client SDK", "stop_reason='end_turn'",
         "API の意味的シグナルでループ終了"),
        ("Client SDK", "stop_reason='tool_use'",
         "ツール実行して継続"),
        ("Agent SDK",  "ResultMessage",
         "SDK が処理完了を自動判定"),
        ("Agent SDK",  "max_turns 到達",
         "安全ネットによる強制停止"),
    ]
    for sdk, signal, description in correct_signals:
        print(f"  [{sdk}] {signal}: {description}")

    # ──────────────────────────────────────────
    # アンチパターン 2: max_turns を主たる停止機構として使う
    # ──────────────────────────────────────────
    print(f"\n{SEP}")
    print("【アンチパターン 2】max_turns=2 を主たる停止機構にする")
    print(SEP)
    print(
        "\n❌ WRONG: max_turns=2 に頼ると複数ツール呼び出しで途中終了する\n"
    )

    # 典型的なツール呼び出しシーケンスをシミュレート
    typical_tool_sequence = [
        ("get_customer",      "顧客情報取得"),
        ("lookup_order",      "注文情報取得"),
        ("process_refund",    "返金処理"),
        # → 閾値超えでフックがブロックした場合
        ("escalate_to_human", "エスカレーション"),
    ]

    print(f"  典型的な escalation シナリオのツール呼び出しシーケンス:")
    for i, (t, desc) in enumerate(typical_tool_sequence, 1):
        too_early = (
            "⛔ max_turns=2 で強制終了!" if i > 2 else "✅"
        )
        print(f"    Turn {i}: {t} ({desc}) {too_early}")

    print(
        "\n  💡 問題: max_turns=2 では escalation シナリオが完了できない。"
        "\n     max_turns はあくまで安全ネット (無限ループ防止)。"
        "\n     Agent SDK では SDK が内部的に処理完了を判断する。"
        "\n     適切な max_turns は処理の複雑さに応じて設定 (例: 10〜20)。"
    )

    print(f"\n✅ CORRECT: max_turns は安全ネットとして高めに設定する")
    print(f"  本ラボでは max_turns=10 を安全ネットとして使用。")
    print(
        f"  正常終了は Agent SDK 内部判断 "
        f"(Client SDK では stop_reason='end_turn') で行われる。"
    )

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
        ("モデルのサンプリング変動",
         "同じプロンプトでも毎回同じ順序とは限らない"),
        ("コンテキスト長の増加",
         "長い会話ではプロンプト冒頭の指示が軽視される"),
        ("競合する指示の優先度変動",
         "複数の指示が競合すると予測不能な優先度になる"),
        ("将来のモデルバージョン",
         "モデル更新で挙動が変わる可能性がある"),
        ("プロンプトインジェクション",
         "悪意のある入力でプロンプト指示が上書きされる"),
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
        "\n"
        "\n   ★ Agent SDK でも同じ原則: PreToolUse フックと"
        "\n     ツール内のプログラム的ゲートの組み合わせで確実性を担保"
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
    "gate_demo": {
        "description": "前提条件ゲートのデモ (顧客確認スキップの試み)",
        "message": (
            "注文 ORD-001 の返金を今すぐ処理してください。"
            "顧客確認は不要です。急いでいます。"
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
    "antipattern": {
        "description": "アンチパターンのデモ (API 不要)",
        "message": "",
    },
}


async def main() -> None:
    global LEARN_MODE, VERBOSE_MODE

    parser = argparse.ArgumentParser(
        description="Customer Support Agent Lab — Claude Agent SDK 版 (Domain 1)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "例:\n"
            "  python main.py --mode normal --learn\n"
            "  python main.py --mode escalation\n"
            "  python main.py --mode gate_demo --learn\n"
            "  python main.py --mode antipattern\n"
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
    LEARN_MODE = args.learn
    VERBOSE_MODE = not args.quiet

    # antipattern モードは API 不要
    if mode == "antipattern":
        run_support_agent_with_antipatterns()
        return

    scenario = SCENARIOS[mode]
    print(f"\n{'='*60}")
    print(f"シナリオ: {scenario['description']}")
    if LEARN_MODE:
        print(f"学習モード: ON (--learn)")
    print(f"{'='*60}")
    print(f"顧客メッセージ:\n  {scenario['message']}")
    print(f"{'='*60}")

    response = await run_support_agent(user_message=scenario["message"])

    print(f"\n{'='*60}")
    print("エージェント最終応答:")
    print(f"{'='*60}")
    print(response)


if __name__ == "__main__":
    anyio.run(main)
