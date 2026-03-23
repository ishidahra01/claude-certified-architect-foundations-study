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

### CLAUDE.md の効果的な記述内容

| カテゴリ | 記述すべき内容 | 例 |
|---|---|---|
| **役割・目的** | プロジェクトの概要、使用技術 | "Python 3.11+, FastAPI, PostgreSQL" |
| **コーディング規約** | スタイルガイド、命名規則 | "PEP 8, Google docstring" |
| **ワークフロー** | 開発手順、テスト方法 | "`pytest tests/` でテスト実行" |
| **禁止事項** | やってはいけない操作 | "本番DBへの直接書き込み禁止" |
| **MCP 情報** | 使用する MCP サーバー | "customer-db MCP サーバーを使用" |

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

## 7. Claude Code の Hooks 機能

### Hooks とは

Claude Code の実行ライフサイクルの特定ポイントにカスタムスクリプトを挿入できます。  
セキュリティポリシーの強制、ロギング、通知などに活用できます。

### Hook のトリガーポイント

| Hook | タイミング | ユースケース |
|---|---|---|
| `PreToolUse` | ツール実行前 | 権限チェック、危険なコマンドのブロック |
| `PostToolUse` | ツール実行後 | 結果のロギング、監査証跡 |
| `Notification` | 通知イベント | Slack通知、アラート |
| `Stop` | セッション終了 | クリーンアップ、レポート生成 |

### Hook の設定例 (`settings.json`)

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          {
            "type": "command",
            "command": "python /path/to/security_check.py"
          }
        ]
      }
    ],
    "PostToolUse": [
      {
        "matcher": ".*",
        "hooks": [
          {
            "type": "command",
            "command": "python /path/to/audit_logger.py"
          }
        ]
      }
    ]
  }
}
```

### PreToolUse Hook の実装例

```python
#!/usr/bin/env python3
# security_check.py - Bash コマンドの実行前チェック

import json
import sys

def check_bash_command(tool_input: dict) -> tuple[bool, str]:
    """危険なコマンドをブロックする"""
    command = tool_input.get("command", "")
    
    # 危険なパターンのリスト
    dangerous_patterns = [
        "rm -rf /",
        "dd if=",
        "mkfs",
        "> /dev/",
        ":(){ :|:& };:",  # Fork bomb
    ]
    
    for pattern in dangerous_patterns:
        if pattern in command:
            return False, f"Dangerous command blocked: '{pattern}'"
    
    # 本番環境への直接アクセスをブロック
    if "prod-db" in command and "--force" in command:
        return False, "Direct production DB access with --force is not allowed"
    
    return True, ""

# stdin から hook input を読み込む
hook_input = json.loads(sys.stdin.read())
tool_input = hook_input.get("tool_input", {})

allowed, reason = check_bash_command(tool_input)

if not allowed:
    # exit code 2 でブロック (Claude Code に伝える)
    print(json.dumps({"decision": "block", "reason": reason}))
    sys.exit(2)
else:
    # exit code 0 で許可
    sys.exit(0)
```

## 8. Claude Code SDK による自動化

### SDK の活用

Claude Code は SDK を通じてプログラム的に制御できます。  
CI/CD パイプライン、バッチ処理、テスト自動化に活用できます。

```python
import subprocess
import json

def run_claude_code_task(prompt: str, working_dir: str) -> dict:
    """
    Claude Code をプログラムから実行する
    """
    result = subprocess.run(
        ["claude", "-p", prompt, "--output-format", "json"],
        cwd=working_dir,
        capture_output=True,
        text=True,
        timeout=300  # 5分タイムアウト
    )
    
    if result.returncode != 0:
        return {
            "success": False,
            "error": result.stderr,
            "exit_code": result.returncode
        }
    
    try:
        output = json.loads(result.stdout)
        return {"success": True, "output": output}
    except json.JSONDecodeError:
        return {"success": True, "output": result.stdout}

# 使用例: PR のコードレビュー自動化
review_result = run_claude_code_task(
    prompt="""
    このリポジトリの変更をレビューして。
    以下の JSON 形式で返してください:
    {
      "summary": "変更の概要",
      "issues": [{"severity": "high|medium|low", "file": "", "message": ""}],
      "approved": true/false
    }
    """,
    working_dir="/path/to/project"
)
```

### GitHub Actions との統合

```yaml
# .github/workflows/claude-review.yml
name: Claude Code Review

on:
  pull_request:
    types: [opened, synchronize]

jobs:
  review:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      
      - name: Install Claude Code
        run: npm install -g @anthropic-ai/claude-code
      
      - name: Run Claude Review
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
        run: |
          claude -p "この PR の変更をレビューして JSON で返して" \
            --output-format json \
            > review_result.json
      
      - name: Post Review Comment
        uses: actions/github-script@v7
        with:
          script: |
            const result = require('./review_result.json');
            await github.rest.issues.createComment({
              issue_number: context.issue.number,
              owner: context.repo.owner,
              repo: context.repo.repo,
              body: `## Claude Code Review\n${JSON.stringify(result, null, 2)}`
            });
```

## 9. カスタムスラッシュコマンド

### スラッシュコマンドの定義

```markdown
<!-- .claude/commands/deploy-check.md -->
# /deploy-check

## 説明
デプロイ前の安全チェックを実行します

## 手順
1. すべてのテストが通ることを確認 (`pytest tests/`)
2. 静的解析でエラーがないことを確認 (`flake8 src/`)
3. 依存関係の脆弱性チェック (`pip-audit`)
4. デプロイ設定ファイルの確認
5. 確認できたら "Deploy check passed" と報告
```

```bash
# 使用方法
/deploy-check
```

### スラッシュコマンドのベストプラクティス

```markdown
<!-- .claude/commands/add-test.md -->
# /add-test $FILE

## 説明
指定ファイルのユニットテストを追加します

## パラメータ
- $FILE: テストを追加するファイルのパス

## 実行内容
1. $FILE の内容を分析して公開インターフェースを特定
2. 各パブリック関数・クラスのテストケースを作成
3. エッジケース・エラーケースを含める
4. tests/ ディレクトリに保存 (命名: test_{basename}.py)
5. テストが通ることを確認
```

## 試験で問われやすいパターン

1. **CLAUDE.md の階層競合** – どの階層のルールが優先されるか
2. **plan mode vs direct** – 「大規模リファクタリング前に計画確認」はどちらか
3. **`context: fork` の必要性** – なぜ独立コンテキストが必要か
4. **CI での non-interactive 実行** – `-p` フラグと structured output の組み合わせ
5. **Hooks の活用** – PreToolUse でセキュリティポリシーを強制する場面
6. **project scope vs user scope** – チーム共有設定 vs 個人設定の使い分け
