#!/usr/bin/env bash
# .claude/hooks/post_tool_use_audit.sh
#
# PostToolUse フック: 全ツール実行の監査ログ記録
#
# Claude Code Agent SDK のフックの仕組み:
#   - stdin にイベントデータが JSON 形式で渡される
#   - PostToolUse は exit code によるブロック不可（ツールは既に実行済み）
#   - exit 0  → 正常完了
#
# settings.json での登録例:
#   "PostToolUse": [
#     { "matcher": "", "hooks": [{ "type": "command", "command": ".claude/hooks/post_tool_use_audit.sh" }] }
#   ]
#
# 参照: https://platform.claude.com/docs/en/agent-sdk/hooks

set -euo pipefail

# stdin からイベントデータを読み込む
input=$(cat)

TOOL_NAME=$(echo "$input" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('tool_name','unknown'))")
TIMESTAMP=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
LOG_FILE="${CLAUDE_PROJECT_DIR:-.}/.claude/hooks/audit.log"

# ログディレクトリが存在しない場合は作成する
mkdir -p "$(dirname "$LOG_FILE")"

# 監査ログに記録（漏れなく全ツール呼び出しを記録できる）
echo "[$TIMESTAMP] tool_used=$TOOL_NAME" >> "$LOG_FILE"

exit 0
