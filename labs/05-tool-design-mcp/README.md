# Lab 05: Tool Design & MCP Integration

**テーマ**: Tool description 設計 / 構造化エラー / ツール配布 / MCP 設定 / 組み込みツール  
**カバードメイン**: Domain 2 (Tool Design & MCP Integration)

## 学習目標

1. **Task 2.1**: ツール description の設計 — 曖昧・重複 description によるミスルーティングを理解する
2. **Task 2.2**: 構造化エラーレスポンス — `errorCategory` / `isRetryable` / ローカル回復パターンを実装する
3. **Task 2.3**: ツールのスコープ制限と `tool_choice` — 多すぎるツール問題と tool_choice 3パターンを理解する
4. **Task 2.4**: MCP 設定 — プロジェクト vs ユーザースコープ・env var 展開・MCP Resources を理解する
5. **Task 2.5**: 組み込みツール選択 — Grep/Glob/Read/Write/Edit の正しい使い分けを習得する

## 実装するもの

```
Task 2.1: description 設計デモ
  ├── AMBIGUOUS_TOOLS    (analyze_content vs analyze_document: ほぼ同じ description)
  ├── CLEAR_TOOLS        (extract_web_results vs analyze_document: 境界が明確)
  └── SPLIT_TOOLS        (汎用ツール → extract_data_points / summarize_content / verify_claim に分割)

Task 2.2: 構造化エラーレスポンス
  ├── get_product_bad()   (NG: "Operation failed" のみ)
  ├── get_product_good()  (OK: errorCategory + isRetryable + userMessage)
  ├── cancel_order_good() (permission / business エラーのデモ)
  └── search_products_correct() (空結果 vs アクセス失敗の区別)

Task 2.3: ツール配布と tool_choice
  ├── ALL_TOOLS_18         (NG: 18個を1エージェントに渡す)
  ├── RESEARCH_AGENT_TOOLS (OK: 3個に絞る)
  ├── SYNTHESIS_AGENT_TOOLS (OK: 3個に絞る)
  └── _demo_tool_choice()  (auto / any / forced の3パターン)

Task 2.4: MCP 設定 (API キー不要)
  ├── project_level config  (.mcp.json: チーム共有ツール)
  ├── user_level config     (~/.claude.json: 個人実験用)
  ├── weak vs strong description (MCP ツールが Grep に負けないために)
  └── MCP Resources example (コンテンツカタログ)

Task 2.5: 組み込みツール選択 (API キー不要)
  └── BUILTIN_TOOLS_SCENARIOS (6シナリオで正解ツールを確認)
```

## 実行方法

```bash
cd labs/05-tool-design-mcp
pip install -r requirements.txt
export ANTHROPIC_API_KEY="your-api-key"   # API が必要なデモのみ

# ────── 全デモ一括実行 ──────
python main.py --mode all

# ────── Task 別デモ ──────

# Task 2.1: ツール description の設計
python main.py --mode description

# Task 2.2: 構造化エラーレスポンス
python main.py --mode error-handling

# Task 2.3: ツール配布と tool_choice
python main.py --mode tool-distribution

# ────── API キー不要 ──────

# Task 2.4: MCP 設定
python main.py --mode mcp-config

# Task 2.5: 組み込みツール選択
python main.py --mode builtin-tools

# アンチパターン一覧 (全 Task)
python main.py --mode antipatterns

# ────── 学習モード ──────

# --learn フラグ: 各ステップの WHY を解説しながら実行
python main.py --mode all --learn
python main.py --mode antipatterns --learn
```

## 設計のポイント

### 1. ツール description の質 (Task 2.1)

LLM はツール選択を **description だけ** を根拠に行います。

```python
# NG: 目的も入力形式も不明 → モデルが毎回ランダムに選択する
{"name": "analyze_content",  "description": "コンテンツを分析する"},
{"name": "analyze_document", "description": "ドキュメントを分析する"},  # ← ほぼ同じ

# OK: 名前と description で目的・入力・出力・境界をすべて分離
{"name": "extract_web_results",
 "description": "WebページURLを取得・解析。PDF/Wordには使わない (analyze_document を使用)"},
{"name": "analyze_document",
 "description": "PDF/Word/Excel を解析。WebページURLには使わない (extract_web_results を使用)"},
```

**なぜ重要か**: 重複する description があると、モデルは同じクエリに対して毎回異なるツールを選択する。  
ツール名の変更だけで選択精度が大幅に向上する。

### 2. 構造化エラーレスポンス (Task 2.2)

```python
# NG: 均一なエラー → モデルが回復戦略を選択できない
return {"isError": True, "content": [{"type": "text", "text": "Operation failed"}]}

# OK: errorCategory で回復戦略を示す
return {
    "isError": True,
    "errorCategory": "transient",  # transient/validation/permission/business
    "isRetryable": True,
    "retryAfterSeconds": 5,
    "content": [{"type": "text", "text": "DB temporarily unavailable"}],
}
```

| errorCategory | isRetryable | 例 | モデルの対応 |
|---|---|---|---|
| `transient` | `true` | タイムアウト, DB 停止 | リトライ |
| `validation` | `false` | 不正なIDフォーマット | 入力修正を促す |
| `permission` | `false` | 権限不足 | エスカレーション |
| `business` | `false` | ポリシー違反 | ユーザーへの説明 |

### 3. ツールのスコープ制限と tool_choice (Task 2.3)

```python
# NG: 18個を1エージェントに渡す → 選択精度が低下
agent = Agent(tools=all_18_tools)

# OK: 役割ごとに絞る (4-5個)
research_agent  = Agent(tools=["search_web", "fetch_url", "search_db"])
synthesis_agent = Agent(tools=["summarize", "verify_fact", "generate_report"])

# tool_choice: 3パターン
tool_choice={"type": "auto"}                             # モデルが判断 (通常の agentic loop)
tool_choice={"type": "any"}                              # ツール実行を強制 (会話テキスト禁止)
tool_choice={"type": "tool", "name": "extract_metadata"} # 特定ツールを最初に強制
```

### 4. MCP 設定 (Task 2.4)

```json
// .mcp.json (プロジェクト): チーム共有ツール → リポジトリにコミット
{
  "mcpServers": {
    "github": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-github"],
      "env": {"GITHUB_TOKEN": "${GITHUB_TOKEN}"}  // ← 環境変数展開
    }
  }
}
```

```json
// ~/.claude.json (ユーザー): 個人実験用 → コミットしない
{
  "mcpServers": {
    "my-experiment": {
      "command": "python",
      "args": ["-m", "my_experimental_server"],
      "env": {"API_KEY": "${MY_PERSONAL_API_KEY}"}
    }
  }
}
```

### 5. 組み込みツールの使い分け (Task 2.5)

```
質問 → 正解ツール

「process_refund はどのファイルで呼ばれている?」 → Grep (中身を検索)
「テストファイルを全て列挙」                       → Glob (名前パターン)
「utils.py の一部だけ修正 (一意なテキストあり)」   → Edit
「Edit が失敗 (同じ行が複数箇所)」                 → Read + Write
「コードベース全体を理解したい」                   → Grep でエントリーポイント → Read
「ビルド・テスト実行」                             → Bash
```

## 試験との対応

| 実装内容 | 試験 Task Statement |
|---|---|
| description の曖昧さによるミスルーティング | Task 2.1 |
| ツール名変更・description 強化 | Task 2.1 |
| 汎用ツールの目的別分割 | Task 2.1 |
| システムプロンプトのキーワード感度 | Task 2.1 |
| isError + errorCategory + isRetryable | Task 2.2 |
| 均一エラー vs 構造化エラー | Task 2.2 |
| 空結果 vs アクセス失敗の区別 | Task 2.2 |
| サブエージェントのローカルリトライ | Task 2.2 |
| ツール数と選択精度 (18 vs 4-5) | Task 2.3 |
| 専門外ツールの誤用防止 | Task 2.3 |
| tool_choice: auto / any / forced | Task 2.3 |
| .mcp.json vs ~/.claude.json のスコープ | Task 2.4 |
| env var 展開による安全な credential 管理 | Task 2.4 |
| MCP ツール description の強化 | Task 2.4 |
| MCP Resources によるツールコール削減 | Task 2.4 |
| Grep vs Glob の使い分け | Task 2.5 |
| Edit 失敗時の Read + Write フォールバック | Task 2.5 |
| インクリメンタルなコードベース理解 | Task 2.5 |
