# Lab 02: Claude Code Team Workflow

**テーマ**: CLAUDE.md 階層 / rules / skills / MCP / plan mode  
**カバードメイン**: Domain 3 (Claude Code Configuration & Workflows)

## 学習目標

1. CLAUDE.md の 3 層階層（global / project / subdir）を理解する
2. `.claude/rules/` のパスベース規約を実装する
3. `.claude/skills/` の `context: fork` を使ったスキル定義を理解する
4. `.mcp.json` の env var 展開で安全に credential を管理する
5. plan mode と direct execution の使い分けを理解する

## このディレクトリの構成

```
02-claude-code-team-workflow/
  CLAUDE.md                     ← project-level のルール
  .mcp.json                     ← MCP サーバー設定 (env var 展開)
  .claude/
    rules/
      python.md                 ← Python ファイルへの規約
      tests.md                  ← テストファイルへの規約
      api.md                    ← API 実装への規約
    skills/
      add-feature/
        SKILL.md                ← 新機能追加スキル (context: fork)
      refactor/
        SKILL.md                ← リファクタリングスキル (plan mode)
  src/
    CLAUDE.md                   ← subdir-level のルール (src/ 配下に適用)
  README.md
```

## CLAUDE.md 階層の理解

### 優先順位

```
~/.claude/CLAUDE.md              (global)
  └── ./CLAUDE.md                (project-level) ← このファイル
       └── ./src/CLAUDE.md       (subdir-level)  ← src/ 配下で優先
```

より具体的なスコープのルールが上書きします。

## plan mode と direct execution の使い分け

| 操作 | 推奨モード | 理由 |
|---|---|---|
| typo 修正 | direct | 小さく安全な変更 |
| 大規模リファクタリング | plan | 不可逆な変更を事前確認 |
| テスト実行 | direct | 副作用なし |
| DB スキーマ変更 | plan | 不可逆な変更 |
| ドキュメント更新 | direct | 安全な変更 |
| 依存関係の追加 | plan | 影響範囲を事前確認 |

## MCP 設定の確認ポイント

`.mcp.json` では:
- credential は `${ENV_VAR}` 形式で参照する
- `.mcp.json` はリポジトリにコミット可能
- 実際の credential は `.env` で管理し `.gitignore` に追加
