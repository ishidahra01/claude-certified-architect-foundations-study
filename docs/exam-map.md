# 試験ドメイン別出題マップ

Claude Certified Architect – Foundations の 5 ドメインと、各 Lab との対応表です。

## ドメイン配点

| # | ドメイン | 配点 | 対応 Lab |
|---|---|---|---|
| 1 | Agentic Architecture & Orchestration | 27% | Lab 01, Lab 04 |
| 2 | Tool Design & MCP Integration | 18% | Lab 01, Lab 02 |
| 3 | Claude Code Configuration & Workflows | 20% | Lab 02 |
| 4 | Prompt Engineering & Structured Output | 20% | Lab 03 |
| 5 | Context Management & Reliability | 15% | Lab 01, Lab 03, Lab 04 |

## Domain 1: Agentic Architecture & Orchestration (27%)

### 主要テーマ

- **Agentic loop の設計**: `stop_reason` による制御フロー
- **Orchestrator / Subagent パターン**: 役割分離と委譲
- **Hook / Gate によるガード**: prompt ではなく deterministic に縛る条件の識別
- **並列 vs 直列委譲**: Task を使った並列化と依存関係管理
- **Partial failure の扱い**: timeout, structured error, graceful degradation

### 設計判断のポイント

```
prompt で縛ってよいもの: 文体、回答形式、トーン
hook/gate で縛るべきもの: 本人確認後でないと実行できない操作、金額閾値超えの refund
```

## Domain 2: Tool Design & MCP Integration (18%)

### 主要テーマ

- **ツール description の設計**: モデルが迷わない境界定義
- **isError / retryable**: tool execution error の表現
- **MCP ツールの役割分離**: 単一責任、小さなインターフェース
- **env var 展開**: `.mcp.json` での安全な credential 管理
- **tool_choice の使い分け**: auto / any / tool

### MCP エラーハンドリング

```json
{
  "isError": true,
  "content": [{ "type": "text", "text": "Order not found: ORD-999" }]
}
```

## Domain 3: Claude Code Configuration & Workflows (20%)

### 主要テーマ

- **CLAUDE.md 階層**: global / project / subdir の優先順位
- **`.claude/rules/`**: パスベースの規約適用
- **`.claude/skills/`**: `context: fork` による権限制御
- **plan mode vs direct execution**: 安全な探索と複雑変更の設計
- **CI での `-p` と structured output**: 自動化パイプライン

### CLAUDE.md 階層優先順位

```
~/.claude/CLAUDE.md              (global – 全プロジェクト共通)
  └── ./CLAUDE.md                (project-level)
       └── ./src/CLAUDE.md       (subdir-level – より具体的)
```

## Domain 4: Prompt Engineering & Structured Output (20%)

### 主要テーマ

- **JSON schema 設計**: nullable/optional の使い分け
- **`tool_use` + `tool_choice`**: structured output の強制
- **semantic validation**: calculated_total vs stated_total
- **validation-retry ループ**: エラー時の自動再試行
- **few-shot examples**: フォーマット制御

### Structured Output フロー

```
Input → tool_use 呼び出し → JSON parse → schema validation
     → semantic validation → PASS: 次処理 / FAIL: retry with error context
```

## Domain 5: Context Management & Reliability (15%)

### 主要テーマ

- **lost-in-the-middle 対策**: 重要情報の配置戦略
- **tool 出力トリミング**: context window 節約
- **provenance の保持**: 情報源の追跡
- **escalation 基準**: 自動処理と human review の境界
- **scratchpad パターン**: 中間思考の活用

### Context 配置のベストプラクティス

```
[System Prompt]
  重要な制約・ルール (冒頭に配置)

[User Turn]
  case facts / retrieved context (最新ターンに配置)
  ↑ モデルは末尾を最もよく参照するため

[Tool Results]
  トリミング済み出力 (必要最小限に絞る)
```
