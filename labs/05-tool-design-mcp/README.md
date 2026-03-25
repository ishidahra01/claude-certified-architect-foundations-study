# Lab 05: Tool Design & MCP Integration

**テーマ**: Tool description 設計 / 構造化エラー / ツール配布 / MCP 設定 / 組み込みツール  
**カバードメイン**: Domain 2 (Tool Design & MCP Integration)

## 学習目標

1. **Task 2.1**: `@tool` の description 設計 — 曖昧・重複 description によるミスルーティングを理解する
2. **Task 2.2**: 構造化エラーレスポンス — `errorCategory` / `isRetryable` / ローカル回復パターンを実装する
3. **Task 2.3**: ツールのスコープ制限 — `create_sdk_mcp_server()` と `allowed_tools` / `AgentDefinition` で誤用を防ぐ
4. **Task 2.4**: MCP 設定 — プロジェクト vs ユーザースコープ・env var 展開・MCP Resources を理解する
5. **Task 2.5**: 組み込みツール選択 — Grep/Glob/Read/Write/Edit の正しい使い分けを習得する

## 実装するもの

```text
Task 2.1: description 設計
  ├── NG: ambiguous dict-based examples
  ├── OK: @tool extract_web_results / analyze_document
  └── OK: @tool で目的別に分割した specialized tools

Task 2.2: 構造化エラー
  ├── @tool get_product / cancel_order / search_products
  └── commerce MCP server

Task 2.3: ツール配布
  ├── research MCP server
  ├── synthesis MCP server
  ├── allowed_tools の生成
  └── AgentDefinition での mcpServers 分離

Task 2.4 / 2.5:
  └── .mcp.json / MCP Resources / built-in tools の使い分け
```

## 実行方法

```bash
cd labs/05-tool-design-mcp
pip install -r requirements.txt

# 全デモ一括実行
python main.py --mode all

# Task 別デモ
python main.py --mode description
python main.py --mode error-handling
python main.py --mode tool-distribution
python main.py --mode mcp-config
python main.py --mode builtin-tools
python main.py --mode antipatterns

# 学習モード
python main.py --mode all --learn
```

> この Lab は **Agent SDK の設計パターンをローカルに確認する教材** です。  
> 実行自体に Claude API キーは不要です。

## 設計のポイント

### 1. `@tool` を使っても、評価されるのは description の質

```python
@tool(
    "extract_web_results",
    "URLで指定されたWebページのコンテンツを取得・解析します...",
    {"url": str},
)
async def extract_web_results_tool(args):
    ...
```

- Agent SDK でもモデルが判断材料にするのは **ツール名・description・入力境界**
- decorator 自体より、**何に使うか / 何に使わないか** を明確に書くことが重要

### 2. MCP integration は `create_sdk_mcp_server()` で構成する

```python
content_server = create_sdk_mcp_server(
    name="content",
    tools=[extract_web_results_tool, analyze_document_tool],
)
```

- Agent SDK の custom tool 群をそのまま MCP server として束ねられる
- `allowed_tools` は `mcp__{server}__{tool}` 形式で絞り込む

### 3. ツール配布は server 単位・agent 単位で絞る

```python
options = ClaudeAgentOptions(
    mcp_servers={"research": research_server, "synthesis": synthesis_server},
    agents={
        "research-agent": AgentDefinition(..., mcpServers=["research"]),
        "synthesis-agent": AgentDefinition(..., mcpServers=["synthesis"]),
    },
)
```

- Claude API の `tool_choice` は下位レイヤーの概念
- Agent SDK では **server grouping / `allowed_tools` / `AgentDefinition`** で選択精度と安全性を上げる

### 4. 構造化エラーは tool handler の戻り値で表現する

```python
return {
    "isError": True,
    "errorCategory": "validation",
    "isRetryable": False,
    "content": [{"type": "text", "text": "Product not found"}],
}
```

- 例外だけに頼らず、モデルが次アクションを選べる payload を返す
- `validation` / `permission` / `business` / `transient` を区別する

## 試験との対応

| 実装内容 | 試験 Task Statement |
|---|---|
| `@tool` description の曖昧さによるミスルーティング | Task 2.1 |
| 汎用ツールの目的別分割 | Task 2.1 |
| `isError` + `errorCategory` + `isRetryable` | Task 2.2 |
| 検索0件 vs 本当の障害の区別 | Task 2.2 |
| `create_sdk_mcp_server()` による MCP integration | Task 2.3, 2.4 |
| `allowed_tools` / `AgentDefinition.mcpServers` によるスコープ制限 | Task 2.3 |
| `.mcp.json` / `~/.claude.json` のスコープ差 | Task 2.4 |
| MCP Resources による事前コンテキスト提供 | Task 2.4 |
| Grep vs Glob など built-in tools の使い分け | Task 2.5 |

## 参考

- Claude Agent SDK overview: https://platform.claude.com/docs/en/agent-sdk/overview
- Claude Agent SDK Python reference: https://platform.claude.com/docs/en/agent-sdk/python
