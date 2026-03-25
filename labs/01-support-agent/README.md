# Lab 01: Customer Support Agent

**テーマ**: Agentic loop + Tool calling + Escalation gate + Hooks + Anti-patterns  
**カバードメイン**: Domain 1 (Agentic Architecture), Domain 2 (Tool Design), Domain 5 (Context & Reliability)

## 学習目標

1. Claude Agent SDK の `ClaudeSDKClient` を使ったエージェント実行を理解する
2. `@tool` + `create_sdk_mcp_server()` によるカスタムツール登録を理解する
3. 単一責任のツール設計と `isError` パターンを実装する
4. **プログラム的前提条件ゲート** による順序強制を実装する
5. **Agent SDK Hooks** (`PreToolUse` / `PostToolUse`) の正しい使い方を理解する
6. **構造化ハンドオフサマリー** を実装する
7. Prompt vs Code での制約の使い分けを体験する
8. **アンチパターン** を実際に確認し、なぜ失敗するかを理解する

## 実装するもの

```text
Customer Support Agent
  ├── get_customer(customer_id)         顧客情報取得 (read-only)
  │     └── 成功時: session_state["customer_verified"] = True にセット
  ├── lookup_order(order_id)            注文情報取得 (read-only)
  ├── process_refund(order_id, amount)  返金処理 + 閾値 Gate
  │     ├── 前提条件ゲート: customer_verified でないとブロック
  │     └── 高額返金は PreToolUse hook で実行前にブロック
  └── escalate_to_human(reason, ctx)    人間へのエスカレーション
        └── 構造化ハンドオフサマリーを出力

Agent SDK hooks (Python callbacks):
  ├── PreToolUse
  │     └── 高額返金を deny して escalate_to_human に誘導
  └── PostToolUse
        ├── 監査ログ出力
        └── updatedMCPToolOutput でレスポンスを正規化
```

## 実行方法

```bash
cd labs/01-support-agent
pip install -r requirements.txt
export ANTHROPIC_API_KEY="your-api-key"

# 通常の返金シナリオ (閾値以内)
python main.py --mode normal

# 閾値超え → escalation シナリオ
python main.py --mode escalation

# 存在しない顧客・注文
python main.py --mode auth_fail

# 返金不可の注文
python main.py --mode not_refundable

# 学習ノート付きで実行
python main.py --mode normal --learn

# API キー不要のアンチパターンデモ
python main.py --mode antipatterns
```

## 設計のポイント

### 1. プログラム的前提条件ゲート

```python
session_state = {"customer_verified": False, "verified_customer_id": None}

if not session_state["customer_verified"]:
    return {
        "isError": True,
        "prerequisite_missing": "customer_verification",
        "error": "process_refund requires get_customer first",
    }
```

**なぜ重要か**: 返金のような副作用を持つ操作は、prompt のみで順序保証してはいけません。`session_state` を用いたコード側の gate で deterministic に制御します。

### 2. Agent SDK のカスタムツール登録

```python
@tool("get_customer", "顧客IDで顧客情報を取得", {"customer_id": str})
async def get_customer_tool(args):
    payload = handle_get_customer(args["customer_id"])
    return {
        "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
        "is_error": payload["isError"],
    }

server = create_sdk_mcp_server(
    name="support",
    tools=[get_customer_tool, lookup_order_tool, process_refund_tool, escalate_to_human_tool],
)
```

**ポイント**:
- SDK が理解するエラー表現はトップレベルの `is_error`
- ラボで学ぶ業務 JSON は本文の `isError`
- この 2 層を分けると、SDK と学習目標の両方を崩さずに実装できる

### 3. PreToolUse / PostToolUse hooks

```python
options = ClaudeAgentOptions(
    mcp_servers={"support": server},
    allowed_tools=[
        "mcp__support__get_customer",
        "mcp__support__lookup_order",
        "mcp__support__process_refund",
        "mcp__support__escalate_to_human",
    ],
    hooks={
        "PreToolUse": [
            HookMatcher(
                matcher="mcp__support__process_refund",
                hooks=[pre_tool_use_refund_check],
            )
        ],
        "PostToolUse": [
            HookMatcher(
                matcher="mcp__support__lookup_order|mcp__support__process_refund",
                hooks=[post_tool_use_audit_and_normalize],
            )
        ],
    },
)
```

- **PreToolUse**: 実行前に deny / allow を返せるので、ポリシー強制に向く
- **PostToolUse**: 実行後の監査・正規化・追加コンテキスト注入に向く
- 本ラボでは `updatedMCPToolOutput` を使って日時や status code を正規化する

### 4. 旧 Client SDK との対応関係

| 旧実装 | Agent SDK 版 |
|---|---|
| `client.messages.create()` | `ClaudeSDKClient.query()` |
| 手動の `while` ループ | SDK 内部のエージェントループ |
| `stop_reason` を自前分岐 | `ResultMessage.stop_reason` で結果確認 |
| ツール定義 dict | `@tool` |
| 自前ディスパッチ | SDK の MCP ツール実行 |

### 5. アンチパターン デモ

`--mode antipatterns` で確認できること:

| アンチパターン | 問題 | 正しいアプローチ |
|---|---|---|
| テキストで終了判断 | 出力表現が変わると壊れる | `stop_reason` / SDK の完了判定を使う |
| 低い反復上限 | 複雑なタスクが途中終了する | `max_turns` は安全ネットとして使う |
| prompt のみで順序制御 | 非決定的で破れる | コードの gate で順序保証する |

## 試験との対応

| 実装内容 | 試験の観点 |
|---|---|
| Agent SDK によるエージェント実行 | Domain 1 |
| カスタムツール + MCP 登録 | Domain 1, 2 |
| `isError` パターン | Domain 1 |
| プログラム的前提条件ゲート | Domain 1 |
| 構造化ハンドオフサマリー | Domain 1 |
| PreToolUse / PostToolUse hooks | Domain 1, 2 |
| Prompt vs Code の使い分け | Domain 1, 5 |
| アンチパターンの理解 | Domain 1 |

## 参考

- Claude Agent SDK overview: https://platform.claude.com/docs/en/agent-sdk/overview
- Claude Agent SDK Python reference: https://platform.claude.com/docs/en/agent-sdk/python
- Hooks: https://platform.claude.com/docs/en/agent-sdk/hooks
