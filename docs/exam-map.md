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
- **エージェントの分類**: メモリタイプ (in-context / external / in-weights / in-cache) と行動タイプ
- **Orchestrator / Subagent パターン**: 役割分離と委譲
- **Hook / Gate によるガード**: prompt ではなく deterministic に縛る条件の識別
- **並列 vs 直列委譲**: Task を使った並列化と依存関係管理
- **Partial failure の扱い**: timeout, structured error, graceful degradation
- **Human-in-the-Loop (HITL)**: 自動実行・承認必須・人間のみの境界設計
- **プロンプトインジェクション対策**: 外部データの安全な処理

### 設計判断のポイント

```
prompt で縛ってよいもの: 文体、回答形式、トーン
hook/gate で縛るべきもの: 本人確認後でないと実行できない操作、金額閾値超えの refund

メモリタイプの選択:
  短期的なタスク文脈 → in-context (messages 配列)
  長期的な状態管理  → external (DB, ファイル)
  コスト削減        → in-cache (プロンプトキャッシング)
```

## Domain 2: Tool Design & MCP Integration (18%)

### 主要テーマ

- **ツール description の設計**: モデルが迷わない境界定義 (5原則)
- **isError / retryable**: tool execution error の表現
- **MCP ツールの役割分離**: 単一責任、小さなインターフェース
- **MCP プリミティブ**: Tools / Resources / Prompts の役割と使い分け
- **トランスポート**: stdio (ローカル) vs HTTP/SSE (リモート) の選択
- **ツールアノテーション**: readOnlyHint, destructiveHint, idempotentHint
- **env var 展開**: `.mcp.json` での安全な credential 管理
- **tool_choice の使い分け**: auto / any / tool
- **MCPセキュリティ**: ツールポイズニング攻撃への対策

### MCP エラーハンドリング

```json
{
  "isError": true,
  "content": [{ "type": "text", "text": "Order not found: ORD-999" }],
  "retryable": false
}
```

### MCP プリミティブの使い分け

```
Tools    → モデルが能動的に呼び出す関数 (API呼び出し、DB操作)
Resources → アプリが提供するデータ (ファイル内容、DBレコード)
Prompts  → ユーザーが選択するテンプレート (ワークフロー定義)
```

## Domain 3: Claude Code Configuration & Workflows (20%)

### 主要テーマ

- **CLAUDE.md 階層**: global / project / subdir の優先順位
- **`.claude/rules/`**: パスベースの規約適用
- **`.claude/skills/`**: `context: fork` による権限制御
- **plan mode vs direct execution**: 安全な探索と複雑変更の設計
- **CI での `-p` と structured output**: 自動化パイプライン
- **Hooks**: PreToolUse / PostToolUse でのセキュリティポリシー強制
- **カスタムスラッシュコマンド**: 再利用可能なタスク自動化
- **SDK による自動化**: GitHub Actions との統合

### CLAUDE.md 階層優先順位

```
~/.claude/CLAUDE.md              (global – 全プロジェクト共通)
  └── ./CLAUDE.md                (project-level)
       └── ./src/CLAUDE.md       (subdir-level – より具体的)
```

### Hooks のトリガーポイント

```
PreToolUse  → ツール実行前 (権限チェック、危険なコマンドのブロック)
PostToolUse → ツール実行後 (ロギング、監査証跡)
Notification → 通知イベント (Slack通知)
Stop        → セッション終了 (クリーンアップ)
```

## Domain 4: Prompt Engineering & Structured Output (20%)

### 主要テーマ

- **システムプロンプト設計**: 役割・制約・スタイルの構造化
- **JSON schema 設計**: nullable/optional の使い分け
- **`tool_use` + `tool_choice`**: structured output の強制
- **semantic validation**: calculated_total vs stated_total
- **validation-retry ループ**: エラー時の自動再試行
- **few-shot examples**: フォーマット制御
- **Extended Thinking**: 複雑な推論タスクでの精度向上
- **XML タグ**: コンテキスト分離による精度向上
- **Prefill**: JSON出力強制への活用 (API専用)

### Structured Output フロー

```
Input → tool_use 呼び出し → JSON parse → schema validation
     → semantic validation → PASS: 次処理 / FAIL: retry with error context
```

### Extended Thinking の適用基準

```
単純な質問応答       → Extended Thinking 不要
複雑な多段階推論     → Extended Thinking 有効
法的・医療的分析     → Extended Thinking 有効 (慎重な判断が必要)
コストが制約         → Extended Thinking 検討 (トークン消費大)
```

## Domain 5: Context Management & Reliability (15%)

### 主要テーマ

- **lost-in-the-middle 対策**: 重要情報の配置戦略
- **tool 出力トリミング**: context window 節約
- **provenance の保持**: 情報源の追跡
- **escalation 基準**: 自動処理と human review の境界
- **scratchpad パターン**: 中間思考の活用
- **プロンプトキャッシング**: コスト削減・速度改善
- **コンテキストウィンドウ管理**: 長い会話の要約戦略
- **エラーリカバリー**: retryable vs non-retryable の判断

### Context 配置のベストプラクティス

```
[System Prompt]
  重要な制約・ルール (冒頭に配置) ← キャッシュ対象
  大きなドキュメント・マニュアル   ← cache_control: ephemeral

[User Turn]
  case facts / retrieved context (最新ターンに配置)
  ↑ モデルは末尾を最もよく参照するため

[Tool Results]
  トリミング済み出力 (必要最小限に絞る)
```

### プロンプトキャッシングの効果

```
コスト: 入力トークン約90%削減 (キャッシュヒット時)
有効期間: 5分間 (ephemeral)
最小サイズ: 1,024トークン (claude-opus-4-5/Sonnet)
最大ポイント数: 4箇所
```
