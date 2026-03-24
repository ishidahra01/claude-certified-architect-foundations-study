"""
Lab 05: Tool Design & MCP Integration

学習目標:
- Task 2.1: ツール description の設計 (曖昧 vs 明確, 重複 vs 分離)
- Task 2.2: 構造化エラーレスポンス (errorCategory, isRetryable, ローカル回復)
- Task 2.3: ツールのスコープ制限と tool_choice 設定
- Task 2.4: MCP 設定 (プロジェクト vs ユーザースコープ, Resources)
- Task 2.5: 組み込みツール選択 (Grep/Glob/Read/Write/Edit)
"""

import json
import os
import argparse
import time
from dataclasses import dataclass, field
from typing import Any

import anthropic

from dotenv import load_dotenv
load_dotenv()

# ────────────────────────────────────────────────
# 擬似データ (API キー不要なシナリオで使用)
# ────────────────────────────────────────────────

MOCK_PRODUCTS = {
    "PROD-001": {"id": "PROD-001", "name": "ノートPC", "price": 120000, "stock": 5},
    "PROD-002": {"id": "PROD-002", "name": "マウス", "price": 3000, "stock": 0},
    "PROD-003": {"id": "PROD-003", "name": "キーボード", "price": 8000, "stock": 2},
}

MOCK_ORDERS = {
    "ORD-001": {"id": "ORD-001", "product_id": "PROD-001", "quantity": 1, "status": "delivered"},
    "ORD-002": {"id": "ORD-002", "product_id": "PROD-002", "quantity": 3, "status": "processing"},
    "ORD-003": {"id": "ORD-003", "product_id": "PROD-999", "quantity": 1, "status": "pending"},
}

# ────────────────────────────────────────────────
# Task 2.1: ツール description の設計デモ
# ────────────────────────────────────────────────

# NG: 曖昧・重複した description (モデルが迷うパターン)
AMBIGUOUS_TOOLS = [
    {
        "name": "analyze_content",
        "description": "コンテンツを分析して結果を返します",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
    {
        "name": "analyze_document",
        "description": "ドキュメントを分析して結果を返します",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
]

# OK: 目的・入力・出力・境界がすべて明確な description
CLEAR_TOOLS = [
    {
        "name": "extract_web_results",
        "description": (
            "URLで指定されたWebページのコンテンツを取得・解析します。"
            "入力: URL文字列 (https:// または http:// で始まる)。"
            "出力: ページタイトル、本文テキスト (最大2000字)、メタデータ。"
            "PDFやWordファイルには使用しないでください (analyze_document を使ってください)。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "対象WebページのURL (例: https://example.com/article)",
                }
            },
            "required": ["url"],
        },
    },
    {
        "name": "analyze_document",
        "description": (
            "ローカルまたはS3上のドキュメントファイル (PDF/Word/Excel) を解析します。"
            "入力: ファイルパス (./docs/xxx.pdf) またはS3 URI (s3://bucket/key)。"
            "出力: 構造化テキスト、ページ数、メタデータ。"
            "WebページのURLには使用しないでください (extract_web_results を使ってください)。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "ファイルパスまたはS3 URI",
                }
            },
            "required": ["path"],
        },
    },
]

# OK: 汎用ツールを目的別に分割した例
SPLIT_TOOLS = [
    {
        "name": "extract_data_points",
        "description": (
            "ドキュメントから数値・日付・固有名詞などの構造化データポイントを抽出します。"
            "入力: ドキュメントテキスト文字列。"
            "出力: key-value 形式のデータポイントリスト (例: {\"売上高\": \"1億円\", \"日付\": \"2024-03-15\"})。"
            "要約や事実検証には使用しないでください。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "document_text": {"type": "string"},
                "data_types": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["numbers", "dates", "names", "all"]},
                    "description": "抽出するデータの種類",
                },
            },
            "required": ["document_text"],
        },
    },
    {
        "name": "summarize_content",
        "description": (
            "ドキュメントの主要な論点・結論を200字以内で要約します。"
            "入力: ドキュメントテキスト文字列。"
            "出力: 要約テキスト (200字以内)。"
            "数値データ抽出や事実検証には使用しないでください。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "document_text": {"type": "string"},
                "max_length": {"type": "integer", "description": "要約の最大文字数 (デフォルト: 200)"},
            },
            "required": ["document_text"],
        },
    },
    {
        "name": "verify_claim_against_source",
        "description": (
            "提示された主張がドキュメントの内容と一致するか検証します。"
            "入力: ドキュメントテキスト + 検証する主張 (claim)。"
            "出力: supported/contradicted/not_mentioned + 根拠となる文 (evidence)。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "document_text": {"type": "string"},
                "claim": {"type": "string", "description": "検証する主張"},
            },
            "required": ["document_text", "claim"],
        },
    },
]


def demo_description_quality(client: anthropic.Anthropic, learn: bool = False) -> None:
    """
    Task 2.1: ツール description の質がモデル選択に与える影響をデモ
    """
    _print_section("Task 2.1: ツール description の設計")

    if learn:
        _print_learn("""
【なぜ description が最重要か】
LLM はツール選択を description だけを根拠に行います。
コードの実装がどれほど優れていても、description が曖昧なら
モデルは正しいツールを選べません。

"良いツールを作る" より
"モデルが迷わないツールインターフェースを作る" がツール設計の本質です。
""")

    # テスト1: 曖昧な description での選択
    print("\n[テスト1] 曖昧な description (analyze_content vs analyze_document)")
    print("  クエリ: 'https://example.com/article の内容を分析して'")
    print("  → モデルはどちらを選ぶ? (毎回ランダムに変わる可能性あり)")
    print()

    _print_tools_comparison(
        "NG (曖昧)",
        AMBIGUOUS_TOOLS,
        "OK (明確)",
        CLEAR_TOOLS,
    )

    if learn:
        _print_learn("""
【重複 description によるミスルーティング】
analyze_content と analyze_document が同じような説明を持つと、
モデルは毎回ランダムに選択します。

解決策:
1. ツール名を目的が分かる名前に変更 (analyze_content → extract_web_results)
2. description で「何に使うか」「何に使わないか」を明示する
3. 入力形式の違いを具体的に記述する (URL vs ファイルパス)
""")

    # テスト2: 汎用ツールの分割
    print("\n[テスト2] 汎用ツールの分割 (analyze_document → 3つの専門ツール)")
    _print_tools_list("分割後のツール群 (OK)", SPLIT_TOOLS)

    if learn:
        _print_learn("""
【なぜ汎用ツールを分割するか】
analyze_document (要約もする, データ抽出もする, 検証もする) では
モデルは「このツールで何をすべきか」を推測しなければなりません。

分割後:
- extract_data_points  → 数値・日付・固有名詞の抽出専用
- summarize_content    → 要約専用
- verify_claim_against_source → 事実検証専用

目的別ツールは description を読むだけで使い方が決まるため、
選択精度が飛躍的に上がります。

【試験ポイント】
- analyze_content → extract_web_results (リネームで重複排除)
- 汎用ツール → extract_data_points/summarize_content/verify_claim に分割
- システムプロンプトに "analyze" というキーワードがあると analyze_* ツールへの偏りが生まれる
""")

    # API を使った実際の比較デモ
    user_query = "https://example.com の内容を分析してください"
    print(f"\n[API デモ] 同じクエリに対する2パターンのツール選択")
    print(f"  クエリ: '{user_query}'")

    for label, tools in [("NG (曖昧な description)", AMBIGUOUS_TOOLS), ("OK (明確な description)", CLEAR_TOOLS)]:
        response = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=256,
            tools=tools,
            tool_choice={"type": "any"},
            messages=[{"role": "user", "content": user_query}],
        )
        tool_use = next((b for b in response.content if b.type == "tool_use"), None)
        selected = tool_use.name if tool_use else "(ツール未選択)"
        print(f"  {label}: → {selected}")


# ────────────────────────────────────────────────
# Task 2.2: 構造化エラーレスポンス
# ────────────────────────────────────────────────

@dataclass
class StructuredError:
    """構造化エラーレスポンスのデータモデル"""
    is_error: bool
    error_category: str   # transient / validation / permission / business
    is_retryable: bool
    message: str
    user_message: str = ""
    retry_after_seconds: int = 0
    partial_results: Any = None


def get_product_bad(product_id: str) -> dict:
    """
    NG: 均一なエラーレスポンス
    モデルはエラーの種類を判断できず、適切な回復戦略を選択できない
    """
    product = MOCK_PRODUCTS.get(product_id)
    if not product:
        return {"isError": True, "content": [{"type": "text", "text": "Operation failed"}]}
    return {"isError": False, "product": product}


def get_product_good(product_id: str, simulate_transient: bool = False) -> dict:
    """
    OK: errorCategory を含む構造化エラーレスポンス
    モデルはエラーの種類に応じた回復戦略を選択できる
    """
    # transient: 一時的なDB障害 → リトライで解決する可能性あり
    if simulate_transient:
        return {
            "isError": True,
            "errorCategory": "transient",
            "isRetryable": True,
            "retryAfterSeconds": 3,
            "content": [{"type": "text", "text": "Database temporarily unavailable. Please retry."}],
        }

    product = MOCK_PRODUCTS.get(product_id)

    # validation: 入力値が不正 (IDフォーマット違反)
    if not product_id.startswith("PROD-"):
        return {
            "isError": True,
            "errorCategory": "validation",
            "isRetryable": False,
            "content": [{"type": "text", "text": f"Invalid product ID format: {product_id}"}],
            "userMessage": "商品IDは 'PROD-' で始まる形式です (例: PROD-001)。",
        }

    # validation: IDは正しいが存在しない
    if not product:
        return {
            "isError": True,
            "errorCategory": "validation",
            "isRetryable": False,
            "content": [{"type": "text", "text": f"Product {product_id} not found"}],
            "userMessage": f"商品 {product_id} は存在しません。商品IDをご確認ください。",
        }

    return {"isError": False, "product": product}


def cancel_order_good(order_id: str, user_role: str = "customer") -> dict:
    """
    OK: permission / business エラーのデモ
    """
    order = MOCK_ORDERS.get(order_id)

    # validation: 注文が存在しない
    if not order:
        return {
            "isError": True,
            "errorCategory": "validation",
            "isRetryable": False,
            "content": [{"type": "text", "text": f"Order {order_id} not found"}],
        }

    # permission: 権限不足 (管理者のみキャンセル可能)
    if order["status"] == "delivered" and user_role != "admin":
        return {
            "isError": True,
            "errorCategory": "permission",
            "isRetryable": False,
            "content": [{"type": "text", "text": "Insufficient permissions: only admin can cancel delivered orders"}],
            "requiredPermission": "order:cancel:delivered",
            "userMessage": "配送済み注文のキャンセルは管理者権限が必要です。サポートにお問い合わせください。",
        }

    # business: ビジネスルール違反 (処理中はキャンセル不可)
    if order["status"] == "processing":
        return {
            "isError": True,
            "errorCategory": "business",
            "isRetryable": False,
            "content": [{"type": "text", "text": f"Order {order_id} is being processed and cannot be cancelled"}],
            "userMessage": "処理中の注文はキャンセルできません。処理完了後に返品申請を行ってください。",
            "policyReference": "cancellation-policy-v2",
        }

    return {"isError": False, "orderId": order_id, "status": "cancelled"}


def search_products_correct(query: str) -> dict:
    """
    OK: 空の検索結果は isError: false で返す (検索自体は成功)
    NG のケース: 空結果を isError: true で返すとモデルが「エラー」と誤解する
    """
    results = [p for p in MOCK_PRODUCTS.values() if query.lower() in p["name"].lower()]
    return {
        "isError": False,
        "results": results,
        "totalCount": len(results),
        "query": query,
        # モデルは totalCount: 0 を見て「検索成功、ただし該当なし」と判断できる
    }


def demo_error_handling(client: anthropic.Anthropic, learn: bool = False) -> None:
    """
    Task 2.2: 構造化エラーレスポンスのデモ
    """
    _print_section("Task 2.2: 構造化エラーレスポンス")

    if learn:
        _print_learn("""
【なぜ構造化エラーが重要か】
均一なエラーメッセージ ("Operation failed") では、
モデルはリトライすべきか、ユーザーに説明すべきか、
上位権限者にエスカレーションすべきか判断できません。

errorCategory の4分類:
  transient  → 一時的な障害 (DB停止・タイムアウト) → リトライ可能
  validation → 入力値の問題 (不正なID・必須項目欠如) → 入力修正を促す
  permission → 権限・認証の問題 (APIキー無効) → エスカレーション
  business   → ビジネスルール違反 (返金上限超過) → ユーザーへの説明
""")

    # NG vs OK の比較
    print("\n[NG vs OK] エラーレスポンスの比較")
    print()

    test_cases = [
        ("存在しないID", "PROD-999", False),
        ("不正なIDフォーマット", "999", False),
        ("一時的なDB障害", "PROD-001", True),
    ]

    for label, product_id, simulate_transient in test_cases:
        bad_result = get_product_bad(product_id if not simulate_transient else "PROD-001")
        good_result = get_product_good(product_id, simulate_transient=simulate_transient)

        print(f"  ケース: {label}")
        print(f"    NG: {json.dumps(bad_result, ensure_ascii=False)}")
        print(f"    OK: {json.dumps(good_result, ensure_ascii=False)}")
        print()

    if learn:
        _print_learn("""
【NG の問題点】
NG の "Operation failed" だけでは:
- transient か business か不明 → リトライ判断ができない
- userMessage がない → ユーザーへの説明ができない
- errorCategory がない → モデルが回復戦略を選択できない

【OK の構造】
- errorCategory: "transient" → リトライすべきと判断
- errorCategory: "validation" → 入力修正を促す
- isRetryable: false → リトライしても無駄と判断
- userMessage: ユーザーへの具体的な案内文
""")

    # permission / business エラーのデモ
    print("\n[permission / business エラー]")
    permission_cases = [
        ("配送済み注文のキャンセル (一般ユーザー)", "ORD-001", "customer"),
        ("配送済み注文のキャンセル (管理者)", "ORD-001", "admin"),
        ("処理中注文のキャンセル", "ORD-002", "customer"),
    ]

    for label, order_id, user_role in permission_cases:
        result = cancel_order_good(order_id, user_role=user_role)
        category = result.get("errorCategory", "success")
        print(f"  {label}: [{category}] {result.get('userMessage', result.get('status', ''))}")

    # 空結果 vs アクセス失敗のデモ
    print("\n[空結果 vs アクセス失敗]")
    queries = ["ノート", "存在しない商品XYZ"]
    for q in queries:
        result = search_products_correct(q)
        print(f"  '{q}' → isError={result['isError']}, totalCount={result['totalCount']} (検索自体は成功)")

    if learn:
        _print_learn("""
【空結果 vs アクセス失敗の区別】
検索結果が0件 → isError: false, totalCount: 0
  「検索は成功した。ただし該当する商品がない」とモデルが判断できる

DB接続エラー → isError: true, errorCategory: "transient"
  「検索自体が失敗した。リトライが必要」とモデルが判断できる

この区別が曖昧だと、モデルは「0件 = エラー」と誤解して
不要なリトライやエスカレーションを行います。
""")

    # Claude を使ったエラー回復のデモ
    _demo_error_recovery_with_claude(client, learn)


def _demo_error_recovery_with_claude(client: anthropic.Anthropic, learn: bool = False) -> None:
    """構造化エラーを受け取ったモデルが適切な回復アクションを選択するデモ"""
    print("\n[Claude による回復アクション選択デモ]")

    tool_def = {
        "name": "get_product",
        "description": (
            "商品IDで商品情報を取得します。"
            "商品IDは 'PROD-' で始まる形式です (例: PROD-001)。"
            "errorCategory: 'validation' の場合はリトライしても意味がありません。"
            "errorCategory: 'transient' の場合は数秒後にリトライしてください。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {"product_id": {"type": "string"}},
            "required": ["product_id"],
        },
    }

    messages: list[dict[str, Any]] = [
        {"role": "user", "content": "商品ID '999' の情報を教えてください"}
    ]

    max_iterations = 4
    for i in range(max_iterations):
        response = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=512,
            tools=[tool_def],
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            final_text = next(
                (b.text for b in response.content if hasattr(b, "text")), ""
            )
            print(f"  モデルの最終回答: {final_text[:200]}")
            if learn:
                _print_learn("""
【ポイント】
errorCategory: "validation" を見たモデルは:
- リトライしない (isRetryable: false)
- userMessage をユーザーに伝える
- 正しいIDフォーマットを案内する

errorCategory: "transient" を見たモデルは:
- retryAfterSeconds 後にリトライする
- または別の手段を探す
""")
            break

        messages.append({"role": "assistant", "content": response.content})

        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                result = get_product_good(block.input.get("product_id", ""))
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(result, ensure_ascii=False),
                })
                print(f"  Iter {i+1}: get_product('{block.input.get('product_id')}') → errorCategory: {result.get('errorCategory', 'none')}")

        messages.append({"role": "user", "content": tool_results})


# ────────────────────────────────────────────────
# Task 2.3: ツール配布と tool_choice
# ────────────────────────────────────────────────

# NG: 全ツールを1エージェントに渡す (18個)
ALL_TOOLS_18 = [
    {"name": "search_web", "description": "Web検索"},
    {"name": "fetch_url", "description": "URL取得"},
    {"name": "parse_html", "description": "HTML解析"},
    {"name": "extract_links", "description": "リンク抽出"},
    {"name": "search_db", "description": "DB検索"},
    {"name": "query_sql", "description": "SQL実行"},
    {"name": "update_record", "description": "レコード更新"},
    {"name": "delete_record", "description": "レコード削除"},
    {"name": "send_email", "description": "メール送信"},
    {"name": "send_sms", "description": "SMS送信"},
    {"name": "create_ticket", "description": "チケット作成"},
    {"name": "update_ticket", "description": "チケット更新"},
    {"name": "analyze_sentiment", "description": "感情分析"},
    {"name": "classify_text", "description": "テキスト分類"},
    {"name": "translate_text", "description": "翻訳"},
    {"name": "summarize", "description": "要約"},
    {"name": "generate_report", "description": "レポート生成"},
    {"name": "export_csv", "description": "CSV出力"},
]

# OK: 役割ごとにスコープを絞ったツールセット (各 4-5個)
RESEARCH_AGENT_TOOLS = [
    {
        "name": "search_web",
        "description": "Webを検索して最新情報を取得します。検索クエリを入力し、URLと概要の一覧を返します。",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
    {
        "name": "fetch_url",
        "description": "指定したURLのページ全文を取得します。search_webで見つけたURLを詳細に読む場合に使用します。",
        "input_schema": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
    },
    {
        "name": "search_db",
        "description": "社内データベースを全文検索します。製品情報・顧客データ・過去の事例を検索できます。",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
]

SYNTHESIS_AGENT_TOOLS = [
    {
        "name": "summarize",
        "description": "複数の情報源から得たテキストを統合・要約します。出力は構造化Markdownです。",
        "input_schema": {
            "type": "object",
            "properties": {
                "texts": {"type": "array", "items": {"type": "string"}},
                "max_length": {"type": "integer"},
            },
            "required": ["texts"],
        },
    },
    {
        "name": "verify_fact",
        "description": (
            "提示された事実が情報源と一致するか検証します。"
            "入力: 検証する主張 + 情報源テキスト。"
            "出力: supported/contradicted/not_mentioned + 根拠文。"
            "Web検索が必要な複雑な検証はコーディネーターに依頼してください。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "claim": {"type": "string"},
                "source_text": {"type": "string"},
            },
            "required": ["claim", "source_text"],
        },
    },
    {
        "name": "generate_report",
        "description": "要約と検証済みデータからレポートを生成します。出力はMarkdown形式です。",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "sections": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["title", "sections"],
        },
    },
]

# 汎用ツールを制約付き代替ツールに置き換える例
GENERIC_FETCH_TOOL = {
    "name": "fetch_url",
    "description": "指定されたURLのコンテンツを取得します",
    "input_schema": {
        "type": "object",
        "properties": {"url": {"type": "string"}},
        "required": ["url"],
    },
}

CONSTRAINED_LOAD_TOOL = {
    "name": "load_document",
    "description": (
        "承認済みドキュメントリポジトリからドキュメントを読み込みます。"
        "入力: docs.internal.example.com または s3://approved-docs/ で始まるURL。"
        "外部WebページのURLは受け付けません (ValidationError を返します)。"
        "任意のWebページ取得が必要な場合は research agent に依頼してください。"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "承認済みURL (docs.internal.example.com/* または s3://approved-docs/*)",
            }
        },
        "required": ["url"],
    },
}


def demo_tool_distribution(client: anthropic.Anthropic, learn: bool = False) -> None:
    """
    Task 2.3: ツール配布と tool_choice のデモ
    """
    _print_section("Task 2.3: ツールのスコープ制限と tool_choice")

    if learn:
        _print_learn("""
【ツールが多すぎると選択精度が下がる理由】
18個のツールを1エージェントに渡すと:
- モデルは18択の中から選択する必要がある
- 似た名前・機能のツールが多いと、どれを使えばよいか迷う
- 決定複雑性が高まり、誤選択率が上がる

解決策: 役割ごとにツールを分割 (各エージェント 4-5個)
- research_agent: search_web, fetch_url, search_db
- synthesis_agent: summarize, verify_fact, generate_report
- notify_agent: send_email, send_sms, create_ticket
""")

    print("\n[NG vs OK] ツール数の比較")
    print(f"  NG: 全ツールを1エージェントに渡す → {len(ALL_TOOLS_18)}個")
    print(f"  OK: 役割ごとに分割:")
    print(f"      research_agent: {len(RESEARCH_AGENT_TOOLS)}個 ({', '.join(t['name'] for t in RESEARCH_AGENT_TOOLS)})")
    print(f"      synthesis_agent: {len(SYNTHESIS_AGENT_TOOLS)}個 ({', '.join(t['name'] for t in SYNTHESIS_AGENT_TOOLS)})")

    if learn:
        _print_learn("""
【専門外ツールの誤用を防ぐ】
synthesis_agent に web_search ツールを渡すと:
- 「情報が足りない」と判断したとき web_search を直接呼んでしまう
- coordinator の管理外で情報収集が発生 → 可観測性が低下

解決策:
- synthesis_agent には summarize/verify_fact/generate_report のみ渡す
- Web検索が必要な場合は coordinator 経由で research_agent に委譲する
- 頻繁に必要な横断的ニーズ (verify_fact) は scoped cross-role tool として提供

【汎用ツールを制約付き代替に置き換える】
fetch_url → load_document (承認済みURLのみ受け付ける)
これにより synthesis_agent が外部APIを叩くリスクを排除できる
""")

    print("\n[汎用 vs 制約付きツール]")
    print(f"  NG: fetch_url - {GENERIC_FETCH_TOOL['description']}")
    print(f"  OK: load_document - {CONSTRAINED_LOAD_TOOL['description'][:100]}...")

    # tool_choice のデモ
    print("\n[tool_choice のデモ]")
    _demo_tool_choice(client, learn)


def _demo_tool_choice(client: anthropic.Anthropic, learn: bool = False) -> None:
    """tool_choice の3パターンをデモ"""

    # tool_choice: "any" のデモ
    print("\n  [tool_choice: 'any'] ツール実行を強制 (会話テキストは返さない)")

    extract_tool = {
        "name": "extract_metadata",
        "description": (
            "テキストからメタデータ (タイトル・著者・日付・カテゴリ) を抽出します。"
            "このツールは常に最初に呼び出されます。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": ["string", "null"]},
                "author": {"type": ["string", "null"]},
                "date": {"type": ["string", "null"]},
                "category": {"type": "string", "enum": ["tech", "business", "science", "other"]},
            },
            "required": ["title", "author", "date", "category"],
        },
    }

    enrich_tool = {
        "name": "enrich_document",
        "description": "抽出されたメタデータを使ってドキュメントを充実させます。extract_metadata の後に呼び出します。",
        "input_schema": {
            "type": "object",
            "properties": {
                "metadata": {"type": "object"},
                "summary": {"type": "string"},
            },
            "required": ["metadata", "summary"],
        },
    }

    test_doc = "Claude 3.5 Sonnet リリースノート (2024-06, Anthropic)"

    # Step 1: forced tool_choice で extract_metadata を強制
    print(f"    入力: '{test_doc}'")
    response_step1 = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=256,
        tools=[extract_tool, enrich_tool],
        tool_choice={"type": "tool", "name": "extract_metadata"},  # ← 強制
        messages=[{"role": "user", "content": f"このドキュメントを処理してください: {test_doc}"}],
    )
    tool_block = next((b for b in response_step1.content if b.type == "tool_use"), None)
    if tool_block:
        print(f"    Step1 (forced): {tool_block.name}({json.dumps(tool_block.input, ensure_ascii=False)})")

        if learn:
            _print_learn("""
【tool_choice: forced の使い所】
パイプラインの最初のステップを強制実行することで、
後続ステップが依存するデータ (メタデータ) の取得を保証できます。

tool_choice: {"type": "tool", "name": "extract_metadata"} を指定すると:
- モデルは必ず extract_metadata を最初に呼び出す
- 「まず考えてからツールを選ぶ」という動作を排除できる

tool_choice: "any" は:
- いずれかのツールを必ず呼び出す (会話テキストを返さない)
- structured output の強制に使用

tool_choice: "auto" は:
- モデルがツールを使うかどうかを判断する
- 通常の agentic loop で使用
""")

    print("\n  [tool_choice: 'any'] 確認クエリでも必ずツールを呼ぶ")
    confirm_tool = {
        "name": "confirm_action",
        "description": "ユーザーの承認を記録します。ユーザーが「はい」「OK」「了解」などと回答した場合に呼び出します。",
        "input_schema": {
            "type": "object",
            "properties": {"confirmed": {"type": "boolean"}, "user_message": {"type": "string"}},
            "required": ["confirmed", "user_message"],
        },
    }

    response_any = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=128,
        tools=[confirm_tool],
        tool_choice={"type": "any"},  # ← ツール実行を強制
        messages=[{"role": "user", "content": "はい、削除してください"}],
    )
    tool_block_any = next((b for b in response_any.content if b.type == "tool_use"), None)
    if tool_block_any:
        print(f"    [any] {tool_block_any.name}({json.dumps(tool_block_any.input, ensure_ascii=False)})")


# ────────────────────────────────────────────────
# Task 2.4: MCP 設定 (API 不要: 設定例と解説)
# ────────────────────────────────────────────────

MCP_CONFIG_EXAMPLES = {
    "project_level": {
        "description": "プロジェクトレベル (.mcp.json) - チーム共有ツール",
        "config": {
            "mcpServers": {
                "github": {
                    "command": "npx",
                    "args": ["-y", "@modelcontextprotocol/server-github"],
                    "env": {
                        "GITHUB_TOKEN": "${GITHUB_TOKEN}"
                    }
                },
                "internal-db": {
                    "command": "python",
                    "args": ["-m", "mcp_server.database"],
                    "env": {
                        "DB_CONNECTION_STRING": "${INTERNAL_DB_URL}",
                        "DB_READ_ONLY": "true"
                    }
                }
            }
        }
    },
    "user_level": {
        "description": "ユーザーレベル (~/.claude.json) - 個人実験用",
        "config": {
            "mcpServers": {
                "my-experiment": {
                    "command": "python",
                    "args": ["-m", "my_experimental_server"],
                    "env": {
                        "API_KEY": "${MY_PERSONAL_API_KEY}"
                    }
                }
            }
        }
    },
    "weak_description": {
        "description": "NG: description が弱い MCP ツール (Grep に負ける)",
        "tool": {
            "name": "search_issues",
            "description": "issueを検索します",
        }
    },
    "strong_description": {
        "description": "OK: description が強い MCP ツール (Grep より優れていると伝える)",
        "tool": {
            "name": "search_issues",
            "description": (
                "Jira の issue を全文検索します。Grep と異なり、タイトル・本文・コメント・"
                "添付ファイルを横断検索でき、ステータス・担当者・優先度でフィルタリングできます。"
                "入力: 検索クエリ文字列、オプション: status/assignee/priority フィルター。"
                "出力: issue ID、タイトル、ステータス、担当者、優先度、本文の最初の200字。"
                "ローカルファイル検索には使用しないでください (Grep を使ってください)。"
            ),
        }
    },
}

MCP_RESOURCES_EXAMPLE = {
    "description": "MCP Resources: コンテンツカタログをエージェントに事前提供",
    "resources": [
        {
            "uri": "docs://quarterly-report-2024-q4",
            "name": "2024年Q4 四半期報告書",
            "description": "売上・利益・KPI サマリー (34ページ)",
            "mimeType": "text/markdown",
        },
        {
            "uri": "docs://api-specification-v3",
            "name": "API仕様書 v3.0",
            "description": "全エンドポイントの仕様、認証方式、エラーコード一覧",
            "mimeType": "application/json",
        },
        {
            "uri": "docs://team-runbook",
            "name": "チーム運用ルールブック",
            "description": "インシデント対応手順・デプロイ手順・エスカレーション先",
            "mimeType": "text/markdown",
        },
    ]
}


def demo_mcp_config(learn: bool = False) -> None:
    """
    Task 2.4: MCP 設定のデモ (API キー不要)
    """
    _print_section("Task 2.4: MCP サーバー設定")

    if learn:
        _print_learn("""
【プロジェクト vs ユーザースコープ】
.mcp.json (プロジェクトルート):
  - チーム全員が同じ MCP サーバーを使用する
  - リポジトリにコミットして共有する
  - GitHub, Jira, 社内DB などのチーム共有ツール

~/.claude.json (ホームディレクトリ):
  - 個人設定 (コミットしない)
  - 個人の実験的サーバー・未検証ツール
  - 本番環境に影響しない範囲で使用する
""")

    for key in ["project_level", "user_level"]:
        example = MCP_CONFIG_EXAMPLES[key]
        print(f"\n[{example['description']}]")
        print(json.dumps(example["config"], ensure_ascii=False, indent=2))

    if learn:
        _print_learn("""
【credential の安全な管理】
- ${GITHUB_TOKEN} → 環境変数から展開 (シークレットを直書きしない)
- .mcp.json 自体はリポジトリにコミット可能
- 実際の credential は .env ファイルで管理し .gitignore に追加する

【なぜ env var 展開が重要か】
.mcp.json にトークンを直書きすると:
- git history に残る
- リポジトリを公開すると漏洩する
- チームメンバー間でシークレットを共有してしまう

${VAR_NAME} 形式にすれば:
- 各自のローカル環境で .env に設定するだけ
- .mcp.json はシークレットなしでコミット可能
""")

    print("\n[MCP ツール description の強化 (組み込みツールに負けないために)]")
    for key in ["weak_description", "strong_description"]:
        example = MCP_CONFIG_EXAMPLES[key]
        tool = example["tool"]
        print(f"\n  {example['description']}:")
        print(f"    name: {tool['name']}")
        print(f"    description: {tool['description'][:120]}{'...' if len(tool['description']) > 120 else ''}")

    if learn:
        _print_learn("""
【MCP ツール description を強化する理由】
Claude Code には Grep, Glob, Read, Bash などの組み込みツールがあります。
MCP ツールの description が弱いと、モデルは MCP ツールより組み込みツールを優先します。

強化のポイント:
1. Grep/組み込みツールと「何が違うか」を明示する
2. 入力・出力の仕様を具体的に書く
3. 「この場合は使わない」という境界を明示する
""")

    print("\n[MCP Resources: コンテンツカタログ]")
    print(f"  {MCP_RESOURCES_EXAMPLE['description']}")
    for resource in MCP_RESOURCES_EXAMPLE["resources"]:
        print(f"    - {resource['name']} ({resource['description']})")

    if learn:
        _print_learn("""
【MCP Resources でツールコールを削減する】
Resources がない場合:
1. エージェントが list_available_documents() を呼ぶ
2. 各ドキュメントの概要を取得するため get_document_summary() を繰り返す
→ 不要な API コール・コスト・レイテンシ

Resources がある場合:
- 接続時にリソース一覧を受け取る
- エージェントは「何が利用可能か」を事前に知っている
- 必要なリソースだけ読み込めばよい

【コミュニティ vs カスタム MCP サーバー】
標準的な統合 (Jira, GitHub, Slack):
  → 公式・コミュニティの MCP サーバーを使う (メンテナンス継続・セキュリティ更新)

チーム固有のワークフロー (社内システム):
  → カスタム MCP サーバーを実装する
""")


# ────────────────────────────────────────────────
# Task 2.5: 組み込みツール選択 (API 不要: 解説デモ)
# ────────────────────────────────────────────────

BUILTIN_TOOLS_SCENARIOS = [
    {
        "question": "コードベース全体で process_refund 関数が呼ばれている箇所を探したい",
        "correct": "Grep",
        "wrong": "Glob",
        "reason": "ファイルの「中身」を検索するのは Grep。Glob はファイル名パターンのみ。",
        "example": 'Grep("process_refund", path="./src")',
    },
    {
        "question": "テストファイル (**/*.test.tsx) を全て列挙したい",
        "correct": "Glob",
        "wrong": "Grep",
        "reason": "ファイル名パターンで検索するのは Glob。中身は関係ない。",
        "example": 'Glob("**/*.test.tsx")',
    },
    {
        "question": "utils.py の一部分だけを修正したい (一意なテキストが存在する)",
        "correct": "Edit",
        "wrong": "Write",
        "reason": "一部修正は Edit が効率的。Write はファイル全体を置き換える。",
        "example": 'Edit(path="utils.py", old_str="return None", new_str="return {}")',
    },
    {
        "question": "Edit が一意なテキストを見つけられない (同じ行が複数箇所にある)",
        "correct": "Read + Write",
        "wrong": "Edit",
        "reason": "Edit が失敗した場合は Read で全体を読んでから Write でフォールバック。",
        "example": 'content = Read("utils.py"); Write("utils.py", content.replace(...))',
    },
    {
        "question": "コードベースを理解したい (全ファイルを読むのは避けたい)",
        "correct": "Grep → Read (必要ファイルのみ)",
        "wrong": "Read (全ファイル一括)",
        "reason": "Grep でエントリーポイントを特定し、import を辿って必要なファイルだけ Read する。",
        "example": 'entry_points = Grep("if __name__", path="./"); Read(entry_points[0])',
    },
    {
        "question": "ビルド・テスト・パッケージインストールを実行したい",
        "correct": "Bash",
        "wrong": "Read/Write",
        "reason": "コマンド実行は Bash。ファイル読み書きとは別の操作。",
        "example": 'Bash("pip install -r requirements.txt && python -m pytest")',
    },
]


def demo_builtin_tools(learn: bool = False) -> None:
    """
    Task 2.5: 組み込みツール選択のデモ (API キー不要)
    """
    _print_section("Task 2.5: 組み込みツール (Grep/Glob/Read/Write/Edit) の使い分け")

    if learn:
        _print_learn("""
【各ツールの役割】
Grep  → ファイルの「中身」を検索 (コンテンツ検索)
Glob  → ファイルの「名前・パス」をパターンマッチ (ファイル発見)
Read  → ファイル全体を読み込む
Write → ファイル全体を書き込む (新規作成・全体置換)
Edit  → ファイルの一部を一意なテキストで特定して修正
Bash  → コマンド実行 (ビルド・テスト・インストール)
""")

    print("\n[シナリオ別 正解ツール選択]")
    for i, scenario in enumerate(BUILTIN_TOOLS_SCENARIOS, 1):
        print(f"\n  シナリオ {i}: {scenario['question']}")
        print(f"    ✅ 正解: {scenario['correct']}")
        print(f"    ❌ 不正解: {scenario['wrong']}")
        print(f"    💡 理由: {scenario['reason']}")
        print(f"    📝 例: {scenario['example']}")

    if learn:
        _print_learn("""
【試験ポイント: Grep vs Glob の使い分け】
  Grep: ファイルの「内容」を検索 → 関数名・エラーメッセージ・import文
  Glob: ファイルの「名前・パス」をパターンマッチ → 拡張子・ディレクトリ構造

「どのファイルに関数 X が定義されているか」→ Grep
「テストファイルは何個あるか」              → Glob

【試験ポイント: Edit 失敗時のフォールバック】
Edit は old_str がファイル内に一意に存在しないと失敗する。
同じ行が複数箇所にある場合:
  → Read でファイル全体を読んで内容を確認
  → より広いコンテキスト (前後の行も含む) で再度 Edit を試みる
  → それでも失敗なら Read + Write でフォールバック

【試験ポイント: インクリメンタルなコードベース理解】
全ファイルを最初に読む → コンテキスト消費が膨大 (NG)
Grep でエントリーポイント特定 → 必要ファイルだけ Read (OK)

Step 1: Grep("if __name__", path="./", glob="**/*.py")
Step 2: Read(entry_point_file)
Step 3: import を辿って必要なファイルだけ Read
""")


# ────────────────────────────────────────────────
# アンチパターンデモ (API キー不要)
# ────────────────────────────────────────────────

ANTIPATTERNS = [
    {
        "title": "NG: 曖昧な description でのツール選択 (Task 2.1)",
        "problem": "analyze_content と analyze_document が同じような description を持つ",
        "consequence": "モデルは毎回ランダムにどちらかを選択する → 再現性がない",
        "fix": "ツール名と description で目的・入力・出力・境界をはっきり分離する",
        "code_ng": """
tools = [
    {"name": "analyze_content",  "description": "コンテンツを分析する"},
    {"name": "analyze_document", "description": "ドキュメントを分析する"},  # ← ほぼ同じ
]""",
        "code_ok": """
tools = [
    {"name": "extract_web_results",  # ← 名前だけで Web 専用と分かる
     "description": "WebページURLを取得・解析。PDF/Wordには使わない"},
    {"name": "analyze_document",
     "description": "PDF/Word/Excelファイルを解析。WebページURLには使わない"},
]""",
    },
    {
        "title": "NG: 均一なエラーレスポンス (Task 2.2)",
        "problem": '"Operation failed" だけではモデルが回復戦略を選択できない',
        "consequence": "transient エラーをリトライしない、または business エラーを無限リトライする",
        "fix": "errorCategory (transient/validation/permission/business) と isRetryable を必ず含める",
        "code_ng": """
return {
    "isError": True,
    "content": [{"type": "text", "text": "Operation failed"}]
    # transient か business か不明 → モデルが判断できない
}""",
        "code_ok": """
return {
    "isError": True,
    "errorCategory": "transient",   # ← リトライすべきと判断できる
    "isRetryable": True,
    "retryAfterSeconds": 5,
    "content": [{"type": "text", "text": "DB temporarily unavailable"}],
}""",
    },
    {
        "title": "NG: ツールが多すぎる (Task 2.3)",
        "problem": "18個のツールを1エージェントに渡す",
        "consequence": "ツール選択の精度が低下、似た名前のツールを混同する",
        "fix": "役割ごとにスコープを絞る (各エージェント 4-5個)",
        "code_ng": """
# 18個を1エージェントに渡す
agent = Agent(tools=all_18_tools)""",
        "code_ok": """
# 役割ごとに分割
research_agent = Agent(tools=research_tools[:4])   # 4個
synthesis_agent = Agent(tools=synthesis_tools[:3]) # 3個""",
    },
    {
        "title": "NG: credential を .mcp.json に直書き (Task 2.4)",
        "problem": "APIキーやDBパスワードを設定ファイルに直書きする",
        "consequence": "git history に残る、リポジトリ公開時に漏洩する",
        "fix": "${ENV_VAR} 形式で環境変数から展開する",
        "code_ng": """
{
  "mcpServers": {
    "db": {
      "env": {"API_KEY": "sk-abc123"}  // ← 直書き NG
    }
  }
}""",
        "code_ok": """
{
  "mcpServers": {
    "db": {
      "env": {"API_KEY": "${DB_API_KEY}"}  // ← 環境変数展開 OK
    }
  }
}""",
    },
    {
        "title": "NG: コードベース全ファイルを一括 Read (Task 2.5)",
        "problem": "最初に全ファイルを Read する",
        "consequence": "コンテキストウィンドウを無駄遣い、必要な情報が埋もれる",
        "fix": "Grep でエントリーポイントを特定してから、必要なファイルだけ Read する",
        "code_ng": """
# 全ファイルを最初に読む (NG)
for file in Glob("**/*.py"):
    content = Read(file)  # 不要なファイルも全部読む""",
        "code_ok": """
# Step 1: エントリーポイントを Grep で特定
entry_points = Grep("if __name__ == '__main__'", path="./")
# Step 2: 必要なファイルだけ Read して import を辿る
main = Read(entry_points[0])""",
    },
]


def demo_antipatterns(learn: bool = False) -> None:
    """
    アンチパターンのデモ (API キー不要)
    """
    _print_section("アンチパターン一覧 (API キー不要)")
    print("各アンチパターンと正しいアプローチを確認します\n")

    for i, ap in enumerate(ANTIPATTERNS, 1):
        print(f"{'─'*60}")
        print(f"[{i}] {ap['title']}")
        print(f"    問題: {ap['problem']}")
        print(f"    影響: {ap['consequence']}")
        print(f"    修正: {ap['fix']}")
        print(f"\n    NG コード:{ap['code_ng']}")
        print(f"\n    OK コード:{ap['code_ok']}")
        print()

    if learn:
        _print_learn("""
【試験での頻出パターン】
1. description が曖昧 → ツール名変更・description 強化で解決
2. "Operation failed" のみ → errorCategory + isRetryable 追加
3. ツールが多すぎる → 役割ごとに 4-5個に絞る
4. credential 直書き → ${ENV_VAR} で展開
5. 全ファイル一括 Read → Grep でエントリーポイント特定 → 必要ファイルだけ Read
""")


# ────────────────────────────────────────────────
# ユーティリティ関数
# ────────────────────────────────────────────────

def _print_section(title: str) -> None:
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


def _print_learn(text: str) -> None:
    print("\n  📚 [LEARN]")
    for line in text.strip().split("\n"):
        print(f"  {line}")
    print()


def _print_tools_comparison(label_a: str, tools_a: list, label_b: str, tools_b: list) -> None:
    print(f"\n  [{label_a}]")
    for t in tools_a:
        print(f"    name: {t['name']}")
        print(f"    description: {t['description']}")
        print()
    print(f"  [{label_b}]")
    for t in tools_b:
        print(f"    name: {t['name']}")
        desc = t['description'][:100] + ('...' if len(t['description']) > 100 else '')
        print(f"    description: {desc}")
        print()


def _print_tools_list(label: str, tools: list) -> None:
    print(f"\n  [{label}]")
    for t in tools:
        desc = t['description'][:80] + ('...' if len(t['description']) > 80 else '')
        print(f"    {t['name']}: {desc}")


# ────────────────────────────────────────────────
# メインエントリーポイント
# ────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Lab 05: Tool Design & MCP Integration",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
実行例:
  # 全デモを順番に実行
  python main.py --mode all

  # Task 2.1: ツール description の設計
  python main.py --mode description

  # Task 2.2: 構造化エラーレスポンス
  python main.py --mode error-handling

  # Task 2.3: ツール配布と tool_choice
  python main.py --mode tool-distribution

  # Task 2.4: MCP 設定 (API キー不要)
  python main.py --mode mcp-config

  # Task 2.5: 組み込みツール選択 (API キー不要)
  python main.py --mode builtin-tools

  # アンチパターンデモ (API キー不要)
  python main.py --mode antipatterns

  # --learn フラグ: 各ステップの WHY を解説しながら実行
  python main.py --mode all --learn
        """,
    )
    parser.add_argument(
        "--mode",
        choices=["all", "description", "error-handling", "tool-distribution", "mcp-config", "builtin-tools", "antipatterns"],
        default="all",
        help="実行するデモ (デフォルト: all)",
    )
    parser.add_argument(
        "--learn",
        action="store_true",
        help="各ステップの WHY を解説しながら実行する",
    )
    args = parser.parse_args()

    # API 不要モード
    api_free_modes = {"mcp-config", "builtin-tools", "antipatterns"}
    needs_api = args.mode not in api_free_modes and args.mode != "all"

    if args.mode == "all" or needs_api:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            print("注意: ANTHROPIC_API_KEY が設定されていません。")
            print("API が必要なデモはスキップされます。\n")
            print("API キー不要のデモのみ実行:")
            print("  python main.py --mode mcp-config")
            print("  python main.py --mode builtin-tools")
            print("  python main.py --mode antipatterns")
            client = None
        else:
            client = anthropic.Anthropic(api_key=api_key)
    else:
        client = None

    modes_to_run = []
    if args.mode == "all":
        modes_to_run = ["description", "error-handling", "tool-distribution", "mcp-config", "builtin-tools", "antipatterns"]
    else:
        modes_to_run = [args.mode]

    for mode in modes_to_run:
        if mode in ("description", "error-handling", "tool-distribution") and client is None:
            print(f"\n[{mode}] SKIP: ANTHROPIC_API_KEY が必要です")
            continue

        if mode == "description":
            demo_description_quality(client, learn=args.learn)
        elif mode == "error-handling":
            demo_error_handling(client, learn=args.learn)
        elif mode == "tool-distribution":
            demo_tool_distribution(client, learn=args.learn)
        elif mode == "mcp-config":
            demo_mcp_config(learn=args.learn)
        elif mode == "builtin-tools":
            demo_builtin_tools(learn=args.learn)
        elif mode == "antipatterns":
            demo_antipatterns(learn=args.learn)

    print(f"\n{'='*60}")
    print("  完了!")
    print(f"{'='*60}")
    print("\n試験との対応:")
    print("  Task 2.1 (description 設計)    → --mode description")
    print("  Task 2.2 (構造化エラー)         → --mode error-handling")
    print("  Task 2.3 (ツール配布/choice)    → --mode tool-distribution")
    print("  Task 2.4 (MCP 設定)             → --mode mcp-config")
    print("  Task 2.5 (組み込みツール)        → --mode builtin-tools")
    print("  全アンチパターン                 → --mode antipatterns")


if __name__ == "__main__":
    main()
