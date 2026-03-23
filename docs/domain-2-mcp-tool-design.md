# Domain 2: Tool Design & MCP Integration

配点: **18%**

## 1. ツール description の設計原則

### モデルが迷わないインターフェースを作る

ツール設計で最も重要なのは **description の質** です。  
"いいツールを作る" より "モデルが迷わないツールインターフェースを作る" が重要です。

```python
# NG: 曖昧な description
{
    "name": "get_info",
    "description": "情報を取得する",
    "input_schema": { ... }
}

# OK: 境界と用途が明確
{
    "name": "lookup_order",
    "description": (
        "注文IDで注文情報を取得します。"
        "顧客の注文状況確認・返金申請の前に必ず呼び出してください。"
        "注文IDは 'ORD-' で始まる文字列です。"
        "注文が存在しない場合は isError: true を返します。"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "order_id": {
                "type": "string",
                "description": "注文ID (例: ORD-12345)",
                "pattern": "^ORD-[0-9]+"
            }
        },
        "required": ["order_id"]
    }
}
```

## 2. エラーハンドリング: isError と retryable

### MCP 仕様に準拠したエラー表現

MCP 仕様では、tool execution error は `isError: true` で返す形が明示されています。

```python
def lookup_order(order_id: str) -> dict:
    order = db.find_order(order_id)
    
    if not order:
        # ★ tool execution error は isError: true で返す
        return {
            "isError": True,
            "content": [{"type": "text", "text": f"Order not found: {order_id}"}],
            "retryable": False  # リトライしても意味がない
        }
    
    if db.is_temporarily_unavailable():
        return {
            "isError": True,
            "content": [{"type": "text", "text": "Database temporarily unavailable"}],
            "retryable": True  # リトライで解決する可能性あり
        }
    
    return {"isError": False, "order": order.to_dict()}
```

### isError のフロー

```
Tool 実行
  ├── 成功: isError=false, 結果を返す
  ├── 論理エラー (not found 等): isError=true, retryable=false → モデルが別アクションを選択
  └── 一時的エラー (network 等): isError=true, retryable=true → リトライ可能
```

## 3. ツールの役割分離 (Single Responsibility)

```python
# NG: 何でもできる神ツール
tools = [
    {"name": "handle_customer", "description": "顧客に関する全操作を処理"}
]

# OK: 単一責任の小さなツール群
tools = [
    {"name": "get_customer",      "description": "顧客情報の取得のみ"},
    {"name": "lookup_order",      "description": "注文情報の取得のみ"},
    {"name": "process_refund",    "description": "返金処理のみ (閾値チェック含む)"},
    {"name": "escalate_to_human", "description": "人間へのエスカレーションのみ"},
]
```

## 4. MCP サーバーの設定

### `.mcp.json` での安全な credential 管理

```json
{
  "mcpServers": {
    "customer-db": {
      "command": "python",
      "args": ["-m", "mcp_server.customer"],
      "env": {
        "DB_CONNECTION_STRING": "${CUSTOMER_DB_URL}",
        "API_KEY": "${CUSTOMER_API_KEY}"
      }
    },
    "order-service": {
      "command": "node",
      "args": ["./mcp-servers/order-service/index.js"],
      "env": {
        "ORDER_SERVICE_URL": "${ORDER_SERVICE_URL}"
      }
    }
  }
}
```

**ポイント**: 
- credential は環境変数経由 (`${VAR_NAME}`) で展開
- `.mcp.json` 自体はリポジトリにコミット可能（credential を直書きしない）
- `.env` ファイルは `.gitignore` に追加

## 5. tool_choice の使い分け

| tool_choice | 動作 | 適した場面 |
|---|---|---|
| `"auto"` | モデルが判断 | 通常の agentic loop |
| `"any"` | いずれかのツールを必ず呼び出す | ツール呼び出しを強制したい場合 |
| `{"type": "tool", "name": "X"}` | 特定ツールを必ず呼び出す | structured output の強制 |

```python
# structured output の強制 (Domain 4 と連携)
response = client.messages.create(
    model="claude-opus-4-5",
    tools=[extract_invoice_schema],
    tool_choice={"type": "tool", "name": "extract_invoice"},  # 必ず呼び出す
    messages=messages
)
```

## 6. MCP ツールのベストプラクティス

### description 設計チェックリスト

- [ ] ツールの目的が 1 文で明確か
- [ ] いつ使うべきか / 使うべきでないかが明確か
- [ ] 入力パラメータの形式・制約が明記されているか
- [ ] エラー時の戻り値が説明されているか
- [ ] 他のツールとの呼び出し順序の依存関係が記述されているか

## 試験で問われやすいパターン

1. **description の不備でモデルが誤ったツールを選択** – 適切な description の設計
2. **isError vs 例外スロー** – tool execution error の正しい表現方法
3. **神ツール vs 単一責任ツール** – ツール粒度の判断
4. **credential の安全な管理** – env var 展開のパターン
