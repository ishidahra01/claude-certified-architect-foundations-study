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

### description 設計の5原則

1. **目的を1文で明確化**: 何をするツールか即座に分かる
2. **いつ使うべきか明示**: 呼び出し条件を明確にする
3. **パラメータ形式を具体的に**: 型・フォーマット・例を記載する
4. **エラー戻り値を説明**: どんなエラーが返るかを記述する
5. **依存関係を記述**: 他ツールとの順序依存があれば明記する

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

## 4. MCP アーキテクチャ

### MCPの3つのプリミティブ

Model Context Protocol (MCP) は、AI モデルと外部システムを接続するための標準プロトコルです。  
MCP サーバーは以下の3種類のプリミティブを提供します。

| プリミティブ | 制御者 | 説明 | ユースケース |
|---|---|---|---|
| **Tools** | モデル | モデルが呼び出す実行可能な関数 | API呼び出し, DB操作, 計算 |
| **Resources** | アプリケーション | モデルに提供する読み取り専用データ | ファイル内容, DB レコード |
| **Prompts** | ユーザー | ユーザーが選択できるテンプレート | ワークフロー定義, インストラクション |

### トランスポートメカニズム

MCP は2つのトランスポートをサポートします。

#### stdio (標準入出力)
ローカルプロセスとの通信に適しています。

```json
// .mcp.json: stdio トランスポートの設定
{
  "mcpServers": {
    "local-db-server": {
      "command": "python",
      "args": ["-m", "mcp_server.database"],
      "env": {
        "DB_URL": "${DATABASE_URL}"
      }
    }
  }
}
```

#### HTTP with SSE (Server-Sent Events)
リモートサービスとの通信に適しています。

```json
// .mcp.json: HTTP/SSE トランスポートの設定
{
  "mcpServers": {
    "remote-api-server": {
      "url": "https://api.example.com/mcp",
      "headers": {
        "Authorization": "Bearer ${API_TOKEN}"
      }
    }
  }
}
```

#### トランスポートの選択基準

```
stdio:
  - 同一マシン上のプロセス
  - セキュリティ境界内での通信
  - 低レイテンシが必要

HTTP/SSE:
  - リモートサービスへの接続
  - マイクロサービスアーキテクチャ
  - 複数クライアントからのアクセス
```

## 5. MCP サーバーの設定

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

## 6. tool_choice の使い分け

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

## 7. ツールアノテーション (Tool Annotations)

MCP 仕様では、ツールに動作特性を示すアノテーションを付与できます。  
これにより、クライアントがリスク管理のための適切なUXを提供できます。

```python
# MCP ツール定義でのアノテーション
tool_definition = {
    "name": "delete_order",
    "description": "指定された注文を削除します",
    "inputSchema": {
        "type": "object",
        "properties": {
            "order_id": {"type": "string"}
        },
        "required": ["order_id"]
    },
    "annotations": {
        "readOnlyHint": False,       # データを変更する
        "destructiveHint": True,     # 破壊的操作 (元に戻せない)
        "idempotentHint": False,     # 冪等ではない
        "openWorldHint": False       # 外部サービスを呼ばない
    }
}

# 読み取り専用ツールの例
read_tool = {
    "name": "get_order_status",
    "description": "注文ステータスを取得します",
    "inputSchema": { ... },
    "annotations": {
        "readOnlyHint": True,        # データを変更しない
        "destructiveHint": False,
        "idempotentHint": True,      # 何度呼んでも同じ結果
        "openWorldHint": False
    }
}
```

### アノテーションの種類

| アノテーション | 意味 | デフォルト |
|---|---|---|
| `readOnlyHint` | データを変更しない | false |
| `destructiveHint` | 破壊的操作（削除など） | true |
| `idempotentHint` | 同じ入力で常に同じ結果 | false |
| `openWorldHint` | 外部インターネットにアクセスする | true |

> **注意**: アノテーションはヒントであり、セキュリティ上の保証ではありません。  
> 実際のアクセス制御は server 側で実装する必要があります。

## 8. MCP セキュリティの考慮事項

### ツールポイズニング攻撃への対策

外部 MCP サーバーを使用する場合、悪意のある description によってモデルが操作される可能性があります。

```python
# セキュリティチェックリスト
class MCPSecurityValidator:
    """MCP サーバー接続前のセキュリティ検証"""
    
    TRUSTED_SERVERS = [
        "internal-crm-server",
        "approved-search-server"
    ]
    
    def validate_server(self, server_name: str, server_config: dict) -> bool:
        """MCP サーバーの信頼性を確認"""
        # 1. ホワイトリスト確認
        if server_name not in self.TRUSTED_SERVERS:
            raise SecurityError(f"Untrusted MCP server: {server_name}")
        
        # 2. URL の検証 (HTTP サーバーの場合)
        if "url" in server_config:
            url = server_config["url"]
            if not url.startswith("https://"):
                raise SecurityError("MCP server must use HTTPS")
        
        # 3. コマンドの検証 (stdio サーバーの場合)
        if "command" in server_config:
            allowed_commands = ["python", "node", "go"]
            cmd = server_config["command"]
            if cmd not in allowed_commands:
                raise SecurityError(f"Disallowed command: {cmd}")
        
        return True
```

## 9. MCP ツールのベストプラクティス

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
5. **MCP プリミティブの使い分け** – Tools / Resources / Prompts の役割
6. **トランスポートの選択** – stdio vs HTTP/SSE の適切な選択
7. **ツールアノテーション** – readOnlyHint, destructiveHint の意味と活用
