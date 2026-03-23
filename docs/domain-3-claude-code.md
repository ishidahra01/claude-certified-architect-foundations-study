# Domain 3: Claude Code Configuration & Workflows

配点: **20%**

## 1. CLAUDE.md 階層と優先順位

### 3 層の階層構造

```
~/.claude/CLAUDE.md              ← global (全プロジェクト共通ルール)
  └── {project}/CLAUDE.md        ← project-level (プロジェクト固有ルール)
       └── {project}/src/CLAUDE.md  ← subdir-level (ディレクトリ固有ルール)
```

**優先順位**: subdir > project > global  
より具体的なスコープのルールが優先されます。

### CLAUDE.md の記述例

```markdown
# Project: Customer Support Agent

## 役割
このプロジェクトは顧客サポートエージェントです。
Anthropic SDK for Python を使用します。

## コーディング規約
- Python 3.11+
- 型ヒントを必ず付ける
- 関数は 30 行以内に収める

## 禁止事項
- 本番 DB への直接書き込み (test/mock を使うこと)
- API キーのハードコード

## テスト
- `pytest tests/` で全テスト実行
- カバレッジ 80% 以上を維持
```

## 2. `.claude/rules/` のパスベース規約

パスに基づいて自動適用されるルールファイルです。

### ディレクトリ構成

```
.claude/
  rules/
    python.md          ← *.py ファイルに適用
    tests.md           ← tests/**/* に適用
    api.md             ← src/api/**/* に適用
```

### ルールファイルの例

```markdown
<!-- .claude/rules/python.md -->
# Python コーディングルール

## 対象: **/*.py

- PEP 8 に従う
- docstring は Google スタイル
- 例外は具体的な型でキャッチする (Exception は使わない)
- f-string を優先する (% や .format() より)
```

```markdown
<!-- .claude/rules/tests.md -->
# テスト記述ルール

## 対象: tests/**/*

- テスト関数名は `test_` で始める
- Arrange-Act-Assert パターンに従う
- モックは unittest.mock を使用
- テストは独立して実行できるようにする
```

## 3. `.claude/skills/` と `context: fork`

### Skills とは

再利用可能なタスク定義です。`SKILL.md` ファイルで定義します。

```markdown
<!-- .claude/skills/add-feature/SKILL.md -->
# Skill: Add Feature

## 説明
新機能を追加するための標準フロー

## context: fork
このスキルは独立したコンテキストで実行します。
親の変更履歴を引き継がないことで、クリーンな実行を保証します。

## 手順
1. 関連するテストを先に書く
2. 最小実装を行う
3. テストが通ることを確認
4. リファクタリング
5. CLAUDE.md の規約に沿っているか確認
```

### `context: fork` の意味

```
通常の実行: 親の会話コンテキスト全体を引き継ぐ
context: fork: 新しいコンテキストで開始 (親から独立)

用途:
  - 大きなリファクタリング (親の変更で汚染されない)
  - 独立したサブタスク
  - 権限を制限したい操作
```

## 4. plan mode vs direct execution

### 使い分けの基準

| モード | 特徴 | 適した場面 |
|---|---|---|
| **plan mode** | 変更前に計画を表示・承認 | 大規模変更、リファクタリング、不可逆操作 |
| **direct execution** | 即時実行 | 小さな変更、テスト実行、情報取得 |

```bash
# plan mode で実行 (変更を適用する前に確認)
claude --plan "認証モジュールをリファクタリングして"

# direct execution
claude "README の typo を修正して"
```

### plan mode を使うべき場面

- データベーススキーマの変更
- 複数ファイルにまたがるリファクタリング
- 設定ファイルの変更
- 削除・移動操作

## 5. CI での `-p` フラグと structured output

### Non-interactive モード (`-p`)

```bash
# CI パイプラインでの使用例
claude -p "この PR のコードをレビューして、JSON 形式で問題点を返して" \
  --output-format json \
  < pr_diff.txt
```

### CI 統合の例

```yaml
# .github/workflows/code-review.yml
- name: Claude Code Review
  run: |
    claude -p "以下のコード変更をレビューして。
    出力形式: {\"issues\": [{\"file\": \"\", \"line\": 0, \"severity\": \"\", \"message\": \"\"}]}
    " < ${{ github.event.pull_request.diff_url }}
```

### False Positive を減らす工夫

```markdown
<!-- CLAUDE.md に追記 -->
## CI レビューの注意事項
- テストファイル内の TODO は指摘しない
- 自動生成ファイル (*.generated.py) はスキップ
- 重大度は critical/high/medium/low で分類
```

## 6. project scope vs user scope

| スコープ | 設定場所 | 適した設定 |
|---|---|---|
| **project scope** | `.claude/` (repo 内) | プロジェクト固有のルール、MCP サーバー設定 |
| **user scope** | `~/.claude/` (home) | 個人の作業スタイル、global な MCP 設定 |

## 試験で問われやすいパターン

1. **CLAUDE.md の階層競合** – どの階層のルールが優先されるか
2. **plan mode vs direct** – 「大規模リファクタリング前に計画確認」はどちらか
3. **`context: fork` の必要性** – なぜ独立コンテキストが必要か
4. **CI での non-interactive 実行** – `-p` フラグと structured output の組み合わせ
