"""
Lab 05: Tool Design & MCP Integration (Claude Agent SDK 版)

学習目標:
- Task 2.1: ツール description の設計
- Task 2.2: 構造化エラーレスポンス
- Task 2.3: ツールのスコープ制限と allowed_tools
- Task 2.4: MCP 設定 (プロジェクト vs ユーザースコープ, Resources)
- Task 2.5: 組み込みツール選択
"""

from __future__ import annotations

import argparse
import json
from typing import Any

from claude_agent_sdk import AgentDefinition, ClaudeAgentOptions, create_sdk_mcp_server, tool
from dotenv import load_dotenv

load_dotenv()

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

AMBIGUOUS_TOOLS = [
    {
        "name": "analyze_content",
        "description": "コンテンツを分析して結果を返します",
        "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
    },
    {
        "name": "analyze_document",
        "description": "ドキュメントを分析して結果を返します",
        "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
    },
]


def get_product_response(product_id: str, simulate_transient: bool = False) -> dict[str, Any]:
    if simulate_transient:
        return {
            "isError": True,
            "errorCategory": "transient",
            "isRetryable": True,
            "retryAfterSeconds": 3,
            "content": [{"type": "text", "text": "Database temporarily unavailable. Please retry."}],
        }

    if not product_id.startswith("PROD-"):
        return {
            "isError": True,
            "errorCategory": "validation",
            "isRetryable": False,
            "content": [{"type": "text", "text": f"Invalid product ID format: {product_id}"}],
            "userMessage": "商品IDは 'PROD-' で始まる形式です (例: PROD-001)。",
        }

    product = MOCK_PRODUCTS.get(product_id)
    if not product:
        return {
            "isError": True,
            "errorCategory": "validation",
            "isRetryable": False,
            "content": [{"type": "text", "text": f"Product {product_id} not found"}],
            "userMessage": f"商品 {product_id} は存在しません。商品IDをご確認ください。",
        }

    return {"isError": False, "product": product}



def cancel_order_response(order_id: str, user_role: str = "customer") -> dict[str, Any]:
    order = MOCK_ORDERS.get(order_id)
    if not order:
        return {
            "isError": True,
            "errorCategory": "validation",
            "isRetryable": False,
            "content": [{"type": "text", "text": f"Order {order_id} not found"}],
        }

    if order["status"] == "delivered" and user_role != "admin":
        return {
            "isError": True,
            "errorCategory": "permission",
            "isRetryable": False,
            "content": [{"type": "text", "text": "Insufficient permissions: only admin can cancel delivered orders"}],
            "requiredPermission": "order:cancel:delivered",
            "userMessage": "配送済み注文のキャンセルは管理者権限が必要です。サポートにお問い合わせください。",
        }

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



def search_products_response(query_text: str) -> dict[str, Any]:
    results = [p for p in MOCK_PRODUCTS.values() if query_text.lower() in p["name"].lower()]
    return {
        "isError": False,
        "results": results,
        "totalCount": len(results),
        "query": query_text,
    }


@tool(
    "extract_web_results",
    (
        "URLで指定されたWebページのコンテンツを取得・解析します。"
        "入力: URL文字列 (https:// または http:// で始まる)。"
        "出力: ページタイトル、本文テキスト (最大2000字)、メタデータ。"
        "PDFやWordファイルには使用しないでください (analyze_document を使ってください)。"
    ),
    {"url": str},
)
async def extract_web_results_tool(args: dict[str, Any]) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": f"Fetched web page: {args['url']}"}]}


@tool(
    "analyze_document",
    (
        "ローカルまたはS3上のドキュメントファイル (PDF/Word/Excel) を解析します。"
        "入力: ファイルパスまたはS3 URI。"
        "出力: 構造化テキスト、ページ数、メタデータ。"
        "WebページのURLには使用しないでください (extract_web_results を使ってください)。"
    ),
    {"path": str},
)
async def analyze_document_tool(args: dict[str, Any]) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": f"Analyzed document: {args['path']}"}]}


@tool(
    "extract_data_points",
    (
        "ドキュメントから数値・日付・固有名詞などの構造化データポイントを抽出します。"
        "要約や事実検証には使用しないでください。"
    ),
    {"document_text": str, "data_types": list[str]},
)
async def extract_data_points_tool(args: dict[str, Any]) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": "Extracted structured data points."}]}


@tool(
    "summarize_content",
    "ドキュメントの主要な論点・結論を200字以内で要約します。数値データ抽出や事実検証には使用しないでください。",
    {
        "type": "object",
        "properties": {
            "document_text": {"type": "string"},
            "max_length": {"type": "integer", "description": "省略時は 200"},
        },
        "required": ["document_text"],
    },
)
async def summarize_content_tool(args: dict[str, Any]) -> dict[str, Any]:
    max_length = max(1, int(args.get("max_length", 200)))
    return {"content": [{"type": "text", "text": "Summarized content."[:max_length]}]}


@tool(
    "verify_claim_against_source",
    "提示された主張がドキュメントの内容と一致するか検証します。supported/contradicted/not_mentioned と根拠文を返します。",
    {"document_text": str, "claim": str},
)
async def verify_claim_against_source_tool(args: dict[str, Any]) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": "Claim verification complete."}]}


@tool(
    "get_product",
    "商品IDで商品情報を取得します。validation / transient を区別した構造化エラーを返します。",
    {"product_id": str},
)
async def get_product_tool(args: dict[str, Any]) -> dict[str, Any]:
    return get_product_response(args["product_id"])


@tool(
    "cancel_order",
    "注文キャンセルを実行します。permission / business error を構造化して返します。",
    {"order_id": str, "user_role": str},
)
async def cancel_order_tool(args: dict[str, Any]) -> dict[str, Any]:
    return cancel_order_response(args["order_id"], args.get("user_role", "customer"))


@tool(
    "search_products",
    "商品名で商品を検索します。検索0件は isError: false の成功結果として返します。",
    {"query": str},
)
async def search_products_tool(args: dict[str, Any]) -> dict[str, Any]:
    return search_products_response(args["query"])


@tool("search_web", "Webを検索して最新情報を取得します。", {"query": str})
async def search_web_tool(args: dict[str, Any]) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": f"Search results for {args['query']}"}]}


@tool("fetch_url", "指定したURLのページ全文を取得します。", {"url": str})
async def fetch_url_tool(args: dict[str, Any]) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": f"Fetched {args['url']}"}]}


@tool("search_db", "社内データベースを全文検索します。", {"query": str})
async def search_db_tool(args: dict[str, Any]) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": f"DB search for {args['query']}"}]}


@tool(
    "summarize",
    "複数の情報源から得たテキストを統合・要約します。",
    {
        "type": "object",
        "properties": {
            "texts": {"type": "array", "items": {"type": "string"}},
            "max_length": {"type": "integer", "description": "省略時は 400"},
        },
        "required": ["texts"],
    },
)
async def summarize_tool(args: dict[str, Any]) -> dict[str, Any]:
    max_length = max(1, int(args.get("max_length", 400)))
    return {"content": [{"type": "text", "text": "Combined summary generated."[:max_length]}]}


@tool("verify_fact", "提示された事実が情報源と一致するか検証します。", {"claim": str, "source_text": str})
async def verify_fact_tool(args: dict[str, Any]) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": "Fact verification complete."}]}


@tool("generate_report", "要約と検証済みデータからレポートを生成します。", {"title": str, "sections": list[str]})
async def generate_report_tool(args: dict[str, Any]) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": f"Generated report: {args['title']}"}]}


@tool(
    "load_document",
    (
        "承認済みドキュメントリポジトリからドキュメントを読み込みます。"
        "docs.internal.example.com または s3://approved-docs/ で始まるURLのみ受け付けます。"
    ),
    {"url": str},
)
async def load_document_tool(args: dict[str, Any]) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": f"Loaded approved document: {args['url']}"}]}


CLEAR_TOOLS = [extract_web_results_tool, analyze_document_tool]
SPLIT_TOOLS = [extract_data_points_tool, summarize_content_tool, verify_claim_against_source_tool]
COMMERCE_TOOLS = [get_product_tool, cancel_order_tool, search_products_tool]
RESEARCH_AGENT_TOOLS = [search_web_tool, fetch_url_tool, search_db_tool]
SYNTHESIS_AGENT_TOOLS = [summarize_tool, verify_fact_tool, generate_report_tool]
CONSTRAINED_SYNTHESIS_TOOLS = [summarize_tool, verify_fact_tool, generate_report_tool, load_document_tool]


MCP_CONFIG_EXAMPLES = {
    "project_level": {
        "description": "プロジェクトレベル (.mcp.json) - チーム共有ツール",
        "config": {
            "mcpServers": {
                "github": {
                    "command": "npx",
                    "args": ["-y", "@modelcontextprotocol/server-github"],
                    "env": {"GITHUB_TOKEN": "${GITHUB_TOKEN}"},
                }
            }
        },
    },
    "user_level": {
        "description": "ユーザーレベル (~/.claude.json) - 個人実験用",
        "config": {
            "mcpServers": {
                "my-experiment": {
                    "command": "python",
                    "args": ["-m", "my_experimental_server"],
                    "env": {"API_KEY": "${MY_PERSONAL_API_KEY}"},
                }
            }
        },
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
    ],
}

BUILTIN_TOOLS_SCENARIOS = [
    {
        "question": "コードベース全体で process_refund 関数が呼ばれている箇所を探したい",
        "correct": "Grep",
        "wrong": "Glob",
        "reason": "ファイルの中身を検索するのは Grep。",
        "example": 'Grep("process_refund", path="./src")',
    },
    {
        "question": "テストファイル (**/*.test.tsx) を全て列挙したい",
        "correct": "Glob",
        "wrong": "Grep",
        "reason": "ファイル名パターンで検索するのは Glob。",
        "example": 'Glob("**/*.test.tsx")',
    },
    {
        "question": "utils.py の一部分だけを修正したい (一意なテキストが存在する)",
        "correct": "Edit",
        "wrong": "Write",
        "reason": "一部修正は Edit が効率的。",
        "example": 'Edit(path="utils.py", old_str="return None", new_str="return {}")',
    },
]

ANTIPATTERNS = [
    {
        "title": "NG: 曖昧な description でのツール選択",
        "problem": "analyze_content と analyze_document が同じような description を持つ",
        "consequence": "モデルが毎回異なるツールを選びやすい",
        "fix": "ツール名と description で目的・入力・境界を分離する",
    },
    {
        "title": "NG: 均一なエラーレスポンス",
        "problem": "Operation failed だけでは回復戦略を選べない",
        "consequence": "不要なリトライや誤エスカレーションを招く",
        "fix": "errorCategory と isRetryable を含める",
    },
    {
        "title": "NG: ツールが多すぎる",
        "problem": "1エージェントに大量のツールを渡す",
        "consequence": "選択精度が下がる",
        "fix": "役割ごとにスコープを分ける",
    },
]



def build_mcp_allowed_tools(server_name: str, tools: list[Any]) -> list[str]:
    """Agent SDK の allowed_tools で使う mcp__{server}__{tool} 形式を作る。"""
    return [f"mcp__{server_name}__{tool.name}" for tool in tools]




def extract_tool_metadata(tool_obj: Any) -> dict[str, Any]:
    return {
        "name": tool_obj.name,
        "description": tool_obj.description,
        "input_schema": tool_obj.input_schema,
    }



def print_tool_group(label: str, tools: list[Any]) -> None:
    print(f"\n  [{label}]")
    for tool_obj in tools:
        snapshot = extract_tool_metadata(tool_obj)
        desc = snapshot["description"][:100] + ("..." if len(snapshot["description"]) > 100 else "")
        print(f"    {snapshot['name']}: {desc}")



def _print_section(title: str) -> None:
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print(f"{'=' * 60}")



def _print_learn(text: str) -> None:
    print("\n  📚 [LEARN]")
    for line in text.strip().split("\n"):
        print(f"  {line}")
    print()



def demo_description_quality(learn: bool = False) -> None:
    _print_section("Task 2.1: ツール description の設計")
    if learn:
        _print_learn(
            "description は Agent SDK でも最重要。@tool を使っても、モデルが判断材料にするのは説明文と境界です。"
        )

    print("\n[NG] 曖昧な description")
    for tool_def in AMBIGUOUS_TOOLS:
        print(f"  - {tool_def['name']}: {tool_def['description']}")

    print_tool_group("OK: 明確な description を持つ SDK tools", CLEAR_TOOLS)
    print_tool_group("OK: 汎用ツールを目的別に分割", SPLIT_TOOLS)

    content_server = create_sdk_mcp_server(name="content", tools=CLEAR_TOOLS + SPLIT_TOOLS)
    print("\n[MCP integration]")
    print(f"  server config: {content_server}")
    print(f"  allowed_tools: {build_mcp_allowed_tools('content', CLEAR_TOOLS + SPLIT_TOOLS)}")



def demo_error_handling(learn: bool = False) -> None:
    _print_section("Task 2.2: 構造化エラーレスポンス")
    if learn:
        _print_learn(
            "Agent SDK の custom tool でも、エラーは例外より structured payload で返した方がモデルが次アクションを選びやすい。"
        )

    commerce_server = create_sdk_mcp_server(name="commerce", tools=COMMERCE_TOOLS)
    print(f"\n[Commerce MCP server] {commerce_server}")
    print(f"  allowed_tools: {build_mcp_allowed_tools('commerce', COMMERCE_TOOLS)}")

    test_cases = [
        ("存在しないID", get_product_response("PROD-999")),
        ("不正なIDフォーマット", get_product_response("999")),
        ("一時的なDB障害", get_product_response("PROD-001", simulate_transient=True)),
    ]
    print("\n[get_product の構造化エラー]")
    for label, result in test_cases:
        print(f"  {label}: {json.dumps(result, ensure_ascii=False)}")

    print("\n[cancel_order の permission / business error]")
    for order_id, user_role in [("ORD-001", "customer"), ("ORD-001", "admin"), ("ORD-002", "customer")]:
        result = cancel_order_response(order_id, user_role=user_role)
        print(f"  {order_id}/{user_role}: {json.dumps(result, ensure_ascii=False)}")

    print("\n[search_products の空結果]")
    for query_text in ["ノート", "存在しない商品XYZ"]:
        print(f"  {query_text}: {json.dumps(search_products_response(query_text), ensure_ascii=False)}")



def demo_tool_distribution(learn: bool = False) -> None:
    _print_section("Task 2.3: ツールのスコープ制限と allowed_tools")
    research_server = create_sdk_mcp_server(name="research", tools=RESEARCH_AGENT_TOOLS)
    synthesis_server = create_sdk_mcp_server(name="synthesis", tools=CONSTRAINED_SYNTHESIS_TOOLS)

    print_tool_group("research tools", RESEARCH_AGENT_TOOLS)
    print_tool_group("synthesis tools", CONSTRAINED_SYNTHESIS_TOOLS)

    research_allowed = build_mcp_allowed_tools("research", RESEARCH_AGENT_TOOLS)
    synthesis_allowed = build_mcp_allowed_tools("synthesis", CONSTRAINED_SYNTHESIS_TOOLS)
    print(f"\nresearch server: {research_server}")
    print(f"research allowed_tools: {research_allowed}")
    print(f"synthesis server: {synthesis_server}")
    print(f"synthesis allowed_tools: {synthesis_allowed}")

    options = ClaudeAgentOptions(
        mcp_servers={"research": research_server, "synthesis": synthesis_server},
        agents={
            "research-agent": AgentDefinition(
                description="Collects evidence only.",
                prompt="Use only research tools to gather evidence.",
                mcpServers=["research"],
            ),
            "synthesis-agent": AgentDefinition(
                description="Synthesizes evidence without broad external access.",
                prompt="Use only synthesis tools and approved documents.",
                mcpServers=["synthesis"],
            ),
        },
    )
    print(f"\n[Agent SDK options example]\n  {options}")

    if learn:
        _print_learn(
            "Claude API の tool_choice は下位レイヤーの概念。Agent SDK では allowed_tools / server grouping / AgentDefinition で誤用を抑える。"
        )



def demo_mcp_config(learn: bool = False) -> None:
    _print_section("Task 2.4: MCP サーバー設定")
    if learn:
        _print_learn(".mcp.json はチーム共有、~/.claude.json は個人実験用。credential は ${ENV_VAR} 展開で管理する。")

    for key in ["project_level", "user_level"]:
        example = MCP_CONFIG_EXAMPLES[key]
        print(f"\n[{example['description']}]")
        print(json.dumps(example["config"], ensure_ascii=False, indent=2))

    print("\n[MCP Resources]")
    print(MCP_RESOURCES_EXAMPLE["description"])
    for resource in MCP_RESOURCES_EXAMPLE["resources"]:
        print(f"  - {resource['name']}: {resource['description']}")



def demo_builtin_tools(learn: bool = False) -> None:
    _print_section("Task 2.5: 組み込みツールの使い分け")
    if learn:
        _print_learn("Grep は内容検索、Glob はファイル発見、Edit は部分修正、Bash はコマンド実行。")
    for scenario in BUILTIN_TOOLS_SCENARIOS:
        print(f"\n  質問: {scenario['question']}")
        print(f"    ✅ 正解: {scenario['correct']}")
        print(f"    ❌ 不正解: {scenario['wrong']}")
        print(f"    💡 理由: {scenario['reason']}")
        print(f"    📝 例: {scenario['example']}")



def demo_antipatterns(learn: bool = False) -> None:
    _print_section("アンチパターン一覧")
    for antipattern in ANTIPATTERNS:
        print(f"\n  {antipattern['title']}")
        print(f"    問題: {antipattern['problem']}")
        print(f"    影響: {antipattern['consequence']}")
        print(f"    修正: {antipattern['fix']}")
    if learn:
        _print_learn("description の曖昧さ、均一エラー、多すぎるツールは試験で頻出の失敗パターン。")



def main() -> None:
    parser = argparse.ArgumentParser(description="Lab 05: Tool Design & MCP Integration (Claude Agent SDK)")
    parser.add_argument(
        "--mode",
        choices=["all", "description", "error-handling", "tool-distribution", "mcp-config", "builtin-tools", "antipatterns"],
        default="all",
    )
    parser.add_argument("--learn", action="store_true")
    args = parser.parse_args()

    modes_to_run = (
        ["description", "error-handling", "tool-distribution", "mcp-config", "builtin-tools", "antipatterns"]
        if args.mode == "all"
        else [args.mode]
    )

    for mode in modes_to_run:
        if mode == "description":
            demo_description_quality(learn=args.learn)
        elif mode == "error-handling":
            demo_error_handling(learn=args.learn)
        elif mode == "tool-distribution":
            demo_tool_distribution(learn=args.learn)
        elif mode == "mcp-config":
            demo_mcp_config(learn=args.learn)
        elif mode == "builtin-tools":
            demo_builtin_tools(learn=args.learn)
        elif mode == "antipatterns":
            demo_antipatterns(learn=args.learn)

    print(f"\n{'=' * 60}")
    print("  完了!")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
