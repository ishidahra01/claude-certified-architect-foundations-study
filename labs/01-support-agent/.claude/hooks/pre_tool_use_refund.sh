#!/usr/bin/env bash
# .claude/hooks/pre_tool_use_refund.sh
#
# PreToolUse フック: 返金額ポリシー強制
#
# Claude Code Agent SDK のフックの仕組み:
#   - stdin にイベントデータが JSON 形式で渡される
#   - exit 0  → 許可（ツール実行を継続）
#   - exit 2  → ブロック（ツール呼び出しを中止し、エラーをモデルに通知）
#   - その他  → 警告（処理は継続、stderr のメッセージがユーザーに表示）
#
# settings.json での登録例:
#   "PreToolUse": [
#     { "matcher": "process_refund", "hooks": [{ "type": "command", "command": ".claude/hooks/pre_tool_use_refund.sh" }] }
#   ]
#
# 参照: https://platform.claude.com/docs/en/agent-sdk/hooks

set -euo pipefail

REFUND_LIMIT=500

# stdin からイベントデータを読み込む
input=$(cat)

TOOL_NAME=$(echo "$input" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('tool_name',''))")
AMOUNT=$(echo "$input" | python3 -c "
import sys, json
d = json.load(sys.stdin)
print(d.get('tool_input', {}).get('amount', 0))
")

if [ "$TOOL_NAME" = "process_refund" ]; then
    # awk で浮動小数点比較（bc より可搬性が高い）
    if awk -v amount="$AMOUNT" -v limit="$REFUND_LIMIT" 'BEGIN { exit (amount > limit) ? 0 : 1 }'; then
        echo "ポリシー違反: 返金額 \$$AMOUNT が自動承認上限 \$$REFUND_LIMIT を超過しています。" >&2
        exit 2  # ★ Claude にツール実行をブロックさせる
    fi
fi

exit 0  # 許可
