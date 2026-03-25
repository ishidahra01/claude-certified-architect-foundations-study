# Domain 2: Tool Design & MCP Integration

配点: **18%**

## Task 2.1: 効果的なツールインターフェースの設計

### なぜ description が最重要なのか

LLM はツール選択を **description だけ** を根拠に行います。  
コードの実装がどれほど優れていても、description が曖昧なら **モデルは正しいツールを選べません**。

"良いツールを作る" より "モデルが迷わないツールインターフェースを作る" がツール設計の本質です。

### description の必須要素

| 要素 | 理由 | 例 |
|---|---|---|
| **目的を1文で明確化** | 即座に役割を把握できる | "注文IDで注文情報を取得します" |
| **いつ使うべきか** | 呼び出し条件を明確にする | "返金申請の前に必ず呼び出す" |
| **入力形式・例** | 誤ったパラメータ渡しを防ぐ | "注文IDは 'ORD-' で始まる文字列" |
| **エラー戻り値** | モデルがエラー後の行動を選択できる | "注文が存在しない場合は isError: true" |
| **依存関係** | 他ツールとの呼び出し順序を保証する | "lookup_order より先に get_customer を呼ぶ" |

### Agent SDK でのツール定義

Claude Agent SDK では、上記の設計原則を **`@tool` decorator + `create_sdk_mcp_server()`** で実装します。

```python
from claude_agent_sdk import create_sdk_mcp_server, tool

@tool(
    "lookup_order",
    "注文IDで注文情報を取得します。返金申請の前に必ず呼び出してください。",
    {"order_id": str},
)
async def lookup_order_tool(args):
    ...

server = create_sdk_mcp_server(name="orders", tools=[lookup_order_tool])
```

- Agent SDK でも重要なのは decorator 自体ではなく **description の質**
- dict ベースの tool 定義でも `@tool` でも、設計原則は同じ

```python
# NG: 目的も入力形式も不明な description
{
    "name": "get_info",
    "description": "情報を取得する",
    "input_schema": {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"]
    }
}

# OK: 目的・条件・形式・エラー・依存関係がすべて明確
{
    "name": "lookup_order",
    "description": (
        "注文IDで注文情報を取得します。"
        "顧客の注文状況確認・返金申請の前に必ず呼び出してください。"
        "注文IDは 'ORD-' で始まる文字列です (例: ORD-12345)。"
        "注文が存在しない場合は isError: true, errorCategory: 'validation' を返します。"
        "このツールは get_customer の成功後に呼び出してください。"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "order_id": {
                "type": "string",
                "description": "注文ID (例: ORD-12345)",
                "pattern": "^ORD-[0-9]+"
            }
        },
        "required": ["order_id"]
    }
}
```

### 重複・曖昧な description によるミスルーティング

**同じような description が複数のツールに存在すると、モデルは一貫してどちらかを選べません。**

```python
# NG: description がほぼ同一 → モデルが毎回ランダムに選択する
tools = [
    {
        "name": "analyze_content",
        "description": "コンテンツを分析して結果を返します",  # ← 何のコンテンツ?
    },
    {
        "name": "analyze_document",
        "description": "ドキュメントを分析して結果を返します",  # ← 何が違うの?
    },
]

# OK: ツール名と description で目的・入力・出力をはっきり分離
tools = [
    {
        "name": "extract_web_results",
        "description": (
            "URLで指定されたWebページのコンテンツを取得・解析します。"
            "入力: URL文字列 (https:// または http:// で始まる)。"
            "出力: ページタイトル、本文テキスト、メタデータ。"
            "ドキュメントファイル (PDF/Word) には使用しないでください。"
        ),
    },
    {
        "name": "analyze_document",
        "description": (
            "ローカルまたはS3上のドキュメントファイル (PDF/Word/Excel) を解析します。"
            "入力: ファイルパスまたはS3 URI。"
            "出力: 構造化テキスト、ページ数、メタデータ。"
            "WebページのURLには使用しないでください。"
        ),
    },
]
```

**なぜ名前変更が重要か**: `analyze_content` は何でも分析できそうに見える。  
`extract_web_results` はWeb専用だと名前だけで伝わり、モデルが迷わなくなる。

### 汎用ツールを目的別ツールに分割する

```python
# NG: 1つのツールに複数の責務
{
    "name": "analyze_document",
    "description": "ドキュメントを分析してデータ抽出・要約・主張検証などを行います",
    # モデルは「どのモードで使えばいい?」と迷う
}

# OK: 責務ごとに分割し、それぞれの入出力を明確に定義
tools = [
    {
        "name": "extract_data_points",
        "description": (
            "ドキュメントから数値・日付・固有名詞などの構造化データを抽出します。"
            "入力: ドキュメントテキスト。出力: key-value 形式のデータポイントリスト。"
            "要約や主張検証には使用しないでください。"
        ),
    },
    {
        "name": "summarize_content",
        "description": (
            "ドキュメントの主要な論点・結論を200字以内で要約します。"
            "入力: ドキュメントテキスト。出力: 要約テキスト。"
            "数値データ抽出や事実検証には使用しないでください。"
        ),
    },
    {
        "name": "verify_claim_against_source",
        "description": (
            "提示された主張がドキュメントの内容と一致するか検証します。"
            "入力: ドキュメントテキスト + 検証する主張。"
            "出力: supported/contradicted/not_mentioned + 根拠文。"
        ),
    },
]
```

**なぜ分割するか**: 汎用ツールはモデルが「このツールで何をすべきか」を推測しなければならない。  
目的別ツールは description を読むだけで使い方が決まるため、選択精度が飛躍的に上がる。

### システムプロンプトのキーワードがツール選択に影響する

システムプロンプトに含まれるキーワードは、意図せずツール選択をバイアスします。

```python
# 問題のあるシステムプロンプト
system = """
あなたは文書分析の専門家です。
ユーザーからの質問には必ず analyze を使って回答してください。
"""
# ↑ "analyze" というキーワードが analyze_* 系ツールへの過度な偏りを生む

# より安全なシステムプロンプト
system = """
あなたは情報取得の専門家です。
ユーザーの意図に最も合ったツールを選択して回答してください。
- Webページの内容を取得する場合: extract_web_results
- ドキュメントファイルを処理する場合: analyze_document
"""
# ↑ ツール名を明示して曖昧な関連付けを排除する
```

---

## Task 2.2: MCP ツールの構造化エラーレスポンス

### isError フラグパターン

MCP 仕様では、ツール実行エラーは **例外をスローするのではなく** `isError: true` で返す形が定められています。  
これにより、モデルはエラーを「通常のレスポンス」として受け取り、次のアクションを決定できます。

```python
# NG: 例外をスローする → モデルが回復アクションを選択できない
def lookup_order(order_id: str) -> dict:
    order = db.find_order(order_id)
    if not order:
        raise ValueError(f"Order not found: {order_id}")  # ← loop が中断される

# OK: isError: true で構造化して返す → モデルが次の行動を選択できる
def lookup_order(order_id: str) -> dict:
    order = db.find_order(order_id)
    if not order:
        return {
            "isError": True,
            "errorCategory": "validation",
            "isRetryable": False,
            "content": [{"type": "text", "text": f"Order {order_id} not found"}],
            "userMessage": "指定された注文IDが見つかりません。注文IDをご確認ください。"
        }
    return {"isError": False, "order": order.to_dict()}
```

### エラーカテゴリの分類

エラーを4種類に分類することで、モデルが適切な回復戦略を選択できます。

| errorCategory | 意味 | isRetryable | 例 | モデルの対応 |
|---|---|---|---|---|
| `transient` | 一時的な障害 | `true` | タイムアウト, サービス一時停止 | リトライ |
| `validation` | 入力値の問題 | `false` | 不正なIDフォーマット, 必須項目欠如 | 入力修正を促す |
| `permission` | 権限・認証の問題 | `false` | APIキー無効, アクセス権なし | エスカレーション |
| `business` | ビジネスルール違反 | `false` | 返金ポリシー違反, 在庫なし | ユーザーへの説明 |

```python
def process_refund(order_id: str, amount: float) -> dict:
    # transient: 一時的なDB障害 → リトライで解決する可能性あり
    if db.is_temporarily_unavailable():
        return {
            "isError": True,
            "errorCategory": "transient",
            "isRetryable": True,
            "retryAfterSeconds": 5,
            "content": [{"type": "text", "text": "Database temporarily unavailable"}],
        }

    order = db.find_order(order_id)

    # validation: 入力値が不正 → リトライしても意味がない
    if not order:
        return {
            "isError": True,
            "errorCategory": "validation",
            "isRetryable": False,
            "content": [{"type": "text", "text": f"Order {order_id} not found"}],
            "userMessage": "注文IDが見つかりません。正しいIDをご確認ください。",
        }

    # permission: 権限不足 → 上位権限者へのエスカレーションが必要
    if not current_user.can_refund():
        return {
            "isError": True,
            "errorCategory": "permission",
            "isRetryable": False,
            "content": [{"type": "text", "text": "Insufficient permissions for refund"}],
            "requiredPermission": "refund:execute",
        }

    # business: ポリシー違反 → ユーザーへの丁寧な説明が必要
    if amount > REFUND_POLICY_LIMIT:
        return {
            "isError": True,
            "errorCategory": "business",
            "isRetryable": False,
            "content": [{"type": "text", "text": f"Refund exceeds policy limit: {REFUND_POLICY_LIMIT}"}],
            "userMessage": f"自動返金の上限は {REFUND_POLICY_LIMIT:,}円です。上限超過の場合は担当者にお問い合わせください。",
            "policyReference": "refund-policy-v2",
        }

    # 成功
    result = db.execute_refund(order_id, amount)
    return {"isError": False, "refund_id": result.id, "status": "completed"}
```

### なぜ均一なエラーレスポンスが問題なのか

```python
# NG: 均一なエラーメッセージ → モデルが回復戦略を選べない
return {
    "isError": True,
    "content": [{"type": "text", "text": "Operation failed"}]
    # "Operation failed" だけでは transient か business か不明
    # モデルはリトライすべきか、ユーザーに説明すべきか判断できない
}

# OK: 構造化されたメタデータ → モデルが適切な回復戦略を選択できる
return {
    "isError": True,
    "errorCategory": "transient",   # ← リトライすべきエラーと判断できる
    "isRetryable": True,
    "retryAfterSeconds": 5,         # ← 何秒後にリトライするか分かる
    "content": [{"type": "text", "text": "Payment service temporarily unavailable"}],
}
```

### サブエージェントでのローカルエラー回復

**サブエージェントは一時的なエラーをローカルで回復し、回復できない場合のみコーディネーターに伝播する。**

```python
async def web_search_subagent(query: str, max_retries: int = 3) -> dict:
    """サブエージェントがローカルでリトライを処理する例"""
    last_error = None

    for attempt in range(max_retries):
        result = call_search_api(query)

        # transient エラー → ローカルでリトライ (コーディネーターに伝播しない)
        if result.get("isError") and result.get("errorCategory") == "transient":
            last_error = result
            await asyncio.sleep(result.get("retryAfterSeconds", 2) ** attempt)
            continue

        # validation/permission/business → ローカル回復不可 → 即座に伝播
        if result.get("isError"):
            return {
                "isError": True,
                "agent": "web_search",
                "originalError": result,
                "partialResults": None,
                "attempted": f"{attempt + 1} attempts",
            }

        return result  # 成功

    # max retry 超過 → partial result と共に伝播
    return {
        "isError": True,
        "errorCategory": "transient",
        "isRetryable": False,   # コーディネーター側でもリトライ不要と示す
        "agent": "web_search",
        "partialResults": None,
        "attempted": f"{max_retries} attempts exhausted",
        "lastError": last_error,
    }
```

### アクセス失敗 vs 有効な空結果の区別

```python
# NG: 検索結果ゼロを isError: true で返す → モデルが「エラーが起きた」と誤解する
def search_products(query: str) -> dict:
    results = db.search(query)
    if not results:
        return {"isError": True, "content": [{"type": "text", "text": "No results"}]}

# OK: 空結果は成功として返す (検索自体は成功、ただし一致なし)
def search_products(query: str) -> dict:
    results = db.search(query)
    return {
        "isError": False,
        "results": results,          # 空リスト [] でも OK
        "totalCount": len(results),
        "query": query,
        # モデルは totalCount: 0 を見て「検索は成功、ただし該当なし」と判断できる
    }
```

---

## Task 2.3: エージェント間のツール配布と tool_choice 設定

### ツールが多すぎると選択精度が下がる

**18個のツールを1つのエージェントに渡すと、ツール選択の精度が大幅に低下します。**

```python
# NG: 全ツールを1エージェントに渡す (18個)
all_tools = [
    "search_web", "fetch_url", "parse_html", "extract_links",
    "search_db", "query_sql", "update_record", "delete_record",
    "send_email", "send_sms", "create_ticket", "update_ticket",
    "analyze_sentiment", "classify_text", "translate_text", "summarize",
    "generate_report", "export_csv",
]
# モデルは 18個の中から選択 → 決定複雑性が高く、誤選択リスクが高い

# OK: 役割ごとにスコープを絞る (各エージェント 4-5個)
research_agent_tools = ["search_web", "fetch_url", "parse_html", "search_db"]
write_agent_tools    = ["update_record", "delete_record", "create_ticket"]
notify_agent_tools   = ["send_email", "send_sms", "update_ticket"]
report_agent_tools   = ["analyze_sentiment", "summarize", "generate_report", "export_csv"]
```

**なぜ重要か**: モデルの注意力は有限。ツールが増えるほど「似たような名前のツールのどちらを使うか」という判断が増え、誤選択率が上がる。

### 専門外ツールの誤用を防ぐスコープ制限

```python
# NG: synthesis agent に web_search を渡す → 専門外のツールを誤用する
synthesis_agent = Agent(
    role="research synthesis",
    tools=["web_search", "summarize", "generate_report", "verify_fact"],
    # synthesis agent が「情報が足りない」と判断すると web_search を呼ぶ
    # → coordinator の管理外で情報収集が発生 → 可観測性が低下
)

# OK: synthesis agent には synthesis に必要なツールだけ渡す
synthesis_agent = Agent(
    role="research synthesis",
    tools=["summarize", "generate_report", "verify_fact"],
    # web 検索が必要な場合は coordinator 経由で web_search agent に委譲する
)

# 高頻度の横断的ニーズには scoped cross-role tool を提供
synthesis_agent = Agent(
    role="research synthesis",
    tools=[
        "summarize",
        "generate_report",
        "verify_fact",          # ← 頻繁に必要な verify は提供
        # web_search は提供しない (coordinator 経由で委譲)
    ],
)
```

### 汎用ツールを制約付き代替ツールに置き換える

```python
# NG: fetch_url は任意のURLを取得できる → synthesis agent が外部APIを叩ける
synthesis_agent_tools = [
    {
        "name": "fetch_url",
        "description": "指定されたURLのコンテンツを取得します",
        # URLの種類を制限していない → 意図しないAPIコールのリスク
    }
]

# OK: load_document は承認済みドキュメントURLのみ取得できる
synthesis_agent_tools = [
    {
        "name": "load_document",
        "description": (
            "承認済みドキュメントリポジトリからドキュメントを読み込みます。"
            "入力: docs.internal.example.com または s3://approved-docs/ で始まるURL。"
            "外部WebページのURLは受け付けません (ValidationError を返します)。"
        ),
    }
]
```

### tool_choice の設定オプション

| tool_choice | 動作 | 適した場面 |
|---|---|---|
| `"auto"` | モデルが判断 (ツールを使う/使わない) | 通常の agentic loop |
| `"any"` | いずれかのツールを必ず呼び出す | 会話テキストではなくツール実行を強制 |
| `{"type": "tool", "name": "X"}` | 特定ツールを必ず最初に呼び出す | パイプラインの最初のステップを強制 |

```python
# tool_choice: "auto" - 通常の agentic loop
response = client.messages.create(
    model="claude-opus-4-5",
    tools=tools,
    tool_choice={"type": "auto"},   # デフォルト: ツール使用は任意
    messages=messages,
)

# tool_choice: "any" - 会話テキストではなくツール実行を強制
# 例: structured output を必ず返させたい場合
response = client.messages.create(
    model="claude-opus-4-5",
    tools=tools,
    tool_choice={"type": "any"},    # いずれかのツールを必ず呼ぶ
    messages=messages,
)

# tool_choice: forced - 特定ツールを最初に強制実行
# 例: メタデータ抽出 → エンリッチメントの順序を保証
response = client.messages.create(
    model="claude-opus-4-5",
    tools=[extract_metadata_tool, enrich_tool],
    tool_choice={"type": "tool", "name": "extract_metadata"},  # 必ず最初に実行
    messages=messages,
)
# extract_metadata の結果を受け取った後、次のターンで enrich_tool を呼ぶ
```

**なぜ `tool_choice: "any"` が重要か**: モデルは「ツールを呼ぶより会話テキストで答えた方が早い」と判断することがある。  
structured output や必須前処理ステップでは `"any"` または forced で強制する。

---

## Task 2.4: MCP サーバーの Claude Code / エージェントへの統合

### プロジェクトスコープ vs ユーザースコープ

MCP サーバーは **配置場所によってスコープ** が決まります。

| 設定ファイル | スコープ | 用途 |
|---|---|---|
| `.mcp.json` (プロジェクトルート) | プロジェクト全体 | チーム共有ツール (Jira, GitHub, 社内DB) |
| `~/.claude.json` (ホームディレクトリ) | ユーザー個人 | 個人実験用・未検証サーバー |

```json
// .mcp.json (プロジェクトレベル) - リポジトリにコミットする
// チーム全員が同じ MCP サーバーを使用する
{
  "mcpServers": {
    "github": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-github"],
      "env": {
        "GITHUB_TOKEN": "${GITHUB_TOKEN}"  // ← 環境変数展開でシークレットを守る
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
```

```json
// ~/.claude.json (ユーザーレベル) - コミットしない (個人設定)
// 個人の実験的サーバー・未承認ツール
{
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
```

**credential の安全な管理**:
- `${VAR_NAME}` 形式で環境変数を参照する
- `.mcp.json` 自体はリポジトリにコミット可能 (シークレットは書かない)
- 実際の credential は `.env` ファイルで管理し `.gitignore` に追加する

### MCP ツール description の強化 (組み込みツールに負けないために)

Claude Code には Grep, Glob, Read, Bash などの組み込みツールがあります。  
MCP ツールの description が弱いと、モデルは MCP ツールより組み込みツールを優先します。

```python
# NG: 説明が弱い → モデルは "Grep の方が速そう" と判断して MCP ツールを使わない
{
    "name": "search_issues",
    "description": "issueを検索します",
}

# OK: 説明を強化して「なぜ Grep より優れているか」を伝える
{
    "name": "search_issues",
    "description": (
        "Jira の issue を全文検索します。Grep と異なり、タイトル・本文・コメント・"
        "添付ファイルを横断検索でき、ステータス・担当者・優先度でフィルタリングできます。"
        "入力: 検索クエリ文字列、オプション: status/assignee/priority フィルター。"
        "出力: issue ID、タイトル、ステータス、担当者、優先度、本文の最初の200字。"
        "ローカルファイル検索には使用しないでください (Grep を使ってください)。"
    ),
}
```

### コミュニティ MCP サーバー vs カスタム実装

```
標準的な統合 (Jira, GitHub, Slack, Google Drive 等)
  → まず公式・コミュニティの MCP サーバーを探す
  → 既存サーバーで要件を満たせる場合、カスタム実装しない

チーム固有のワークフロー (社内システム, 独自API)
  → カスタム MCP サーバーを実装する
```

**理由**: コミュニティサーバーはメンテナンス・セキュリティアップデートが継続的に行われる。  
カスタム実装は初期開発コストと長期メンテナンスコストが高い。

### MCP Resources でエクスプロラトリーな Tool Call を削減

MCP Resources は **コンテンツカタログをエージェントに事前提供** する仕組みです。  
エージェントが「何が利用可能か」を調べるための tool call を省けます。

```python
# Resources がない場合: エージェントが探索的な tool call を繰り返す
# 1. list_available_documents() を呼ぶ
# 2. 各ドキュメントの概要を取得するため get_document_summary() を繰り返す
# → 不要な API コール・コスト・レイテンシ

# Resources がある場合: エージェントは接続時に利用可能なデータを把握する
# MCP サーバー実装例
class DocumentMCPServer:
    def list_resources(self) -> list[Resource]:
        """接続時に利用可能なリソース一覧を返す"""
        return [
            Resource(
                uri="docs://quarterly-report-2024-q4",
                name="2024年Q4 四半期報告書",
                description="売上・利益・KPI サマリー (34ページ)",
                mimeType="text/markdown",
            ),
            Resource(
                uri="docs://api-specification-v3",
                name="API仕様書 v3.0",
                description="全エンドポイントの仕様、認証方式、エラーコード一覧",
                mimeType="application/json",
            ),
            # ... 他のドキュメント
        ]
    # エージェントはこの一覧を見て「どのリソースを読めばよいか」を判断できる
    # 不要なドキュメントは読まずに済む
```

### トランスポートの選択

| トランスポート | 用途 | 選択基準 |
|---|---|---|
| **stdio** | ローカルプロセス | 同一マシン, 低レイテンシ, セキュリティ境界内 |
| **HTTP/SSE** | リモートサービス | マイクロサービス, 複数クライアント対応 |

```json
// stdio: ローカルプロセスとの通信
{
  "mcpServers": {
    "local-db": {
      "command": "python",
      "args": ["-m", "mcp_server.database"],
      "env": {"DB_URL": "${DATABASE_URL}"}
    }
  }
}
```

```json
// HTTP/SSE: リモートサービスとの通信
{
  "mcpServers": {
    "remote-api": {
      "url": "https://api.internal.example.com/mcp",
      "headers": {
        "Authorization": "Bearer ${API_TOKEN}"
      }
    }
  }
}
```

---

## Task 2.5: 組み込みツール (Grep, Glob, Read, Write, Edit, Bash) の効果的な使い方

### 各ツールの役割と使い分け

| ツール | 用途 | 選択基準 |
|---|---|---|
| **Grep** | ファイル内容の検索 | 関数名・エラーメッセージ・import文などのパターン検索 |
| **Glob** | ファイルパスのパターンマッチ | 拡張子・ディレクトリ構造によるファイル発見 |
| **Read** | ファイル全体の読み込み | ファイル内容の完全取得 |
| **Write** | ファイル全体の書き込み | 新規ファイル作成・全体置換 |
| **Edit** | ターゲット箇所の修正 | ファイルの一部を一意のテキストで特定して変更 |
| **Bash** | コマンド実行 | ビルド・テスト・インストール・複雑な操作 |

### Grep: コンテンツ検索 (ファイルの中身を検索する)

```bash
# 関数の呼び出し箇所を全ファイルから検索
Grep("process_refund", path="./src")

# エラーメッセージを含むファイルを特定
Grep("Order not found", path="./", glob="**/*.py")

# import 文の検索
Grep("from anthropic import", path="./", glob="**/*.py")

# 正規表現でフォーマット検索
Grep("ORD-[0-9]+", path="./logs")
```

**Grep を使う場面**:
- コードベース全体で「どこかで使われているか」を確認するとき
- エラーメッセージやログからソースコードを追跡するとき
- import/export の依存関係を調べるとき

### Glob: ファイルパスのパターンマッチ (ファイルを「名前」で探す)

```bash
# テストファイルを全て発見
Glob("**/*.test.tsx")

# 特定ディレクトリの Python ファイル
Glob("src/**/*.py")

# 設定ファイルを探す
Glob("**/*.json", exclude="**/node_modules/**")

# 複数の拡張子
Glob("**/*.{ts,tsx}")
```

**Glob を使う場面**:
- 特定の拡張子のファイルを網羅的に収集するとき
- ディレクトリ構造を把握するとき
- バッチ処理するファイル群を特定するとき

**Grep vs Glob の使い分け**:
```
「どのファイルに関数 X が定義されているか」  → Grep (ファイルの中身を検索)
「テストファイルは何個あるか」               → Glob (ファイル名パターンで検索)
「このエラーはどのファイルから来るか」        → Grep (エラー文字列で検索)
「src 配下の全 .ts ファイルを取得」          → Glob (拡張子パターン)
```

### Read / Write / Edit: ファイル操作の使い分け

```python
# Read: ファイル全体を読む
content = Read("src/main.py")

# Write: ファイル全体を書く (新規作成・全体置換)
Write("src/config.py", content=new_content)

# Edit: 一意のテキストを特定して修正 (ファイルの一部だけ変更)
Edit(
    path="src/main.py",
    old_str='    return {"isError": False, "order": order}',
    new_str='    return {"isError": False, "order": order.to_dict()}',
)
```

**Edit が失敗する場合の対処法**:

```python
# Edit は old_str がファイル内に一意に存在しないと失敗する
# 例: 同じ行が複数箇所に存在する場合

# NG: Edit が失敗するパターン
Edit(
    path="src/utils.py",
    old_str="    return None",  # ← ファイル内に10箇所存在する → Edit 失敗
    new_str="    return {}",
)

# OK: 失敗時は Read + Write にフォールバック
content = Read("src/utils.py")
new_content = content.replace(
    "def get_order(order_id):\n    return None",  # 関数名を含む一意なコンテキスト
    "def get_order(order_id):\n    return {}",
)
Write("src/utils.py", content=new_content)
```

### コードベース理解の構築戦略: 全ファイルを読まない

```python
# NG: 全ファイルを最初に読み込む → コンテキスト消費が膨大
for file in Glob("**/*.py"):
    content = Read(file)  # 不要なファイルも全部読む

# OK: Grep でエントリーポイントを特定 → 必要なファイルだけ Read する
# Step 1: エントリーポイントを Grep で特定
entry_points = Grep("if __name__ == '__main__'", path="./", glob="**/*.py")

# Step 2: エントリーポイントを Read して import を追う
main_content = Read(entry_points[0])
# main.py が "from core.agent import Agent" を import しているなら

# Step 3: 必要なモジュールだけ追って Read する
agent_content = Read("core/agent.py")
# → 必要最小限のファイルだけ読んでコードベースを理解できる
```

**ラッパーモジュール越しの関数追跡**:
```python
# Step 1: エクスポートされている名前を全て特定 (Grep)
exports = Grep("^def |^class |^__all__", path="core/", glob="**/*.py")

# Step 2: 各名前がコードベース全体でどこで使われているかを検索 (Grep)
for name in exports:
    usages = Grep(name, path="./", glob="**/*.py")
```

---

## 補足: ツールアノテーション (Tool Annotations)

MCP 仕様では、ツールに動作特性を示すアノテーションを付与できます。  
クライアントがリスク管理のための適切なUXを提供できます。

```python
tool_definition = {
    "name": "delete_order",
    "description": "指定された注文を削除します",
    "inputSchema": {"type": "object", "properties": {"order_id": {"type": "string"}}, "required": ["order_id"]},
    "annotations": {
        "readOnlyHint": False,    # データを変更する
        "destructiveHint": True,  # 破壊的操作 (元に戻せない)
        "idempotentHint": False,  # 冪等ではない
        "openWorldHint": False    # 外部サービスを呼ばない
    }
}
```

| アノテーション | 意味 | デフォルト |
|---|---|---|
| `readOnlyHint` | データを変更しない | false |
| `destructiveHint` | 破壊的操作（削除など） | true |
| `idempotentHint` | 同じ入力で常に同じ結果 | false |
| `openWorldHint` | 外部インターネットにアクセスする | true |

> **注意**: アノテーションはヒントであり、セキュリティ上の保証ではありません。  
> 実際のアクセス制御は server 側で実装する必要があります。

---

## 試験で問われやすいパターン (Task 別)

### Task 2.1
1. **重複 description でのミスルーティング** – `analyze_content` vs `analyze_document` の区別
2. **ツール名変更で重複を排除** – `analyze_content` → `extract_web_results` へのリネーム
3. **汎用ツールの分割** – `analyze_document` → `extract_data_points` / `summarize_content` / `verify_claim_against_source`
4. **システムプロンプトのキーワード感度** – "analyze" というキーワードが意図しないツール選択を引き起こす

### Task 2.2
5. **isError vs 例外スロー** – tool execution error は `isError: true` で返す
6. **errorCategory の4分類** – transient / validation / permission / business
7. **均一エラー vs 構造化エラー** – "Operation failed" では回復不可
8. **空結果 vs アクセス失敗** – `totalCount: 0` は成功、`isError: true` はエラー
9. **サブエージェントのローカルリトライ** – transient はローカルで回復、business はコーディネーターへ伝播

### Task 2.3
10. **ツール数と選択精度** – 18個より 4-5個
11. **専門外ツールの誤用** – synthesis agent が web_search を呼ぶ問題
12. **tool_choice: "any"** – 会話テキストでなくツール実行を強制
13. **tool_choice: forced** – 特定ツールを最初に実行するパイプライン強制

### Task 2.4
14. **プロジェクト vs ユーザースコープ** – `.mcp.json` vs `~/.claude.json`
15. **credential の安全管理** – `${ENV_VAR}` で展開、直書き禁止
16. **MCP Resources でツールコール削減** – コンテンツカタログを事前提供
17. **コミュニティ vs カスタム** – 標準統合はコミュニティ、チーム固有はカスタム

### Task 2.5
18. **Grep vs Glob の使い分け** – コンテンツ検索 vs ファイルパスパターン
19. **Edit 失敗時の Read+Write フォールバック** – 一意なテキストが存在しない場合
20. **インクリメンタルなコードベース理解** – Grep でエントリーポイント特定 → 必要ファイルだけ Read
