# Domain 4: Prompt Engineering & Structured Output

配点: **20%**

## 1. システムプロンプトの設計原則

### 効果的なシステムプロンプトの構造

システムプロンプトはモデルの動作の基盤となります。構造化された記述が重要です。

```python
system_prompt = """
# 役割 (Role)
あなたは {company_name} のカスタマーサポートエージェントです。
顧客の注文・返金・アカウントに関する問題を解決します。

# 能力と制約 (Capabilities & Constraints)
## できること
- 注文状況の確認
- $500 以下の返金処理
- サポートチケットの作成

## できないこと
- $500 を超える返金 (人間にエスカレーション)
- アカウントの削除
- 個人情報の変更 (本人確認が必要)

# 応答スタイル (Response Style)
- 丁寧で親切なトーンを維持する
- 簡潔に要点を伝える
- 専門用語は避け、平易な言葉を使う

# 重要なルール (Critical Rules)
1. 顧客の情報を確認してから処理を行う
2. 不確かな場合は確認を取る
3. エラーは正直に伝える
"""
```

### システムプロンプト vs ユーザープロンプト

| 種別 | 適した内容 | 変更頻度 |
|---|---|---|
| **システムプロンプト** | 役割、制約、応答スタイル、ビジネスルール | 低 (固定) |
| **ユーザープロンプト** | タスク固有の指示、入力データ、コンテキスト | 高 (動的) |

## 2. JSON schema 設計: nullable vs optional

### nullable と optional の使い分け

```python
# JSON Schema での表現
schema = {
    "type": "object",
    "properties": {
        # required + nullable: 必ず存在するが null の可能性あり
        "invoice_date": {
            "type": ["string", "null"],
            "description": "請求日 (読み取れない場合は null)"
        },
        # optional (required に含めない): フィールド自体が存在しない可能性あり
        "discount_amount": {
            "type": "number",
            "description": "割引額 (割引がない場合は省略)"
        },
        # other + detail パターン: 未知カテゴリの表現
        "payment_method": {
            "type": "string",
            "enum": ["credit_card", "bank_transfer", "cash", "other"],
            "description": "支払い方法"
        },
        "payment_method_detail": {
            "type": ["string", "null"],
            "description": "payment_method が 'other' の場合の詳細"
        }
    },
    "required": ["invoice_date", "total_amount", "payment_method"]
}
```

## 3. structured output の実装パターン

> 試験対策では **Claude Agent SDK が第一選択**、`tool_use` / `tool_choice` はその下位の Claude API レイヤーとして理解する。

### Agent SDK での第一選択: custom tool / `output_format`

```python
options = ClaudeAgentOptions(
    output_format={"type": "json_schema", "schema": schema},
)

async for message in query(
    prompt="請求書テキストを分析して structured output を返してください",
    options=options,
):
    ...
```

- **出力フォーマットだけ保証したい**なら `output_format`
- **構造化データを tool call として扱いたい**なら Agent SDK の custom tool を使う

### Claude API レイヤー: `tool_use` + `tool_choice`

### `tool_choice` で JSON 出力を強制する

```python
import anthropic

client = anthropic.Anthropic()

extract_invoice_tool = {
    "name": "extract_invoice",
    "description": "請求書から構造化データを抽出します",
    "input_schema": {
        "type": "object",
        "properties": {
            "invoice_number": {"type": "string"},
            "invoice_date": {"type": ["string", "null"]},
            "vendor_name": {"type": "string"},
            "line_items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "description": {"type": "string"},
                        "quantity": {"type": "number"},
                        "unit_price": {"type": "number"},
                        "amount": {"type": "number"}
                    },
                    "required": ["description", "quantity", "unit_price", "amount"]
                }
            },
            "subtotal": {"type": "number"},
            "tax": {"type": ["number", "null"]},
            "stated_total": {"type": "number"},
            "calculated_total": {"type": "number"}
        },
        "required": ["invoice_number", "vendor_name", "line_items", "stated_total", "calculated_total"]
    }
}

response = client.messages.create(
    model="claude-opus-4-5",
    max_tokens=1024,
    tools=[extract_invoice_tool],
    # ★ tool_choice で特定ツールの呼び出しを強制
    tool_choice={"type": "tool", "name": "extract_invoice"},
    messages=[{
        "role": "user",
        "content": f"以下の請求書からデータを抽出してください:\n\n{invoice_text}"
    }]
)

# ツール呼び出し結果を取得
tool_use_block = next(b for b in response.content if b.type == "tool_use")
extracted_data = tool_use_block.input
```

## 4. Semantic Validation

### calculated_total vs stated_total

単なる JSON スキーマ検証では不十分です。**セマンティックな整合性チェック**が必要です。

```python
def validate_invoice(data: dict) -> tuple[bool, list[str]]:
    errors = []
    
    # 1. スキーマ検証 (構造・型チェック)
    # (jsonschema ライブラリ等で実施)
    
    # 2. セマンティック検証: 計算値と記載値の一致確認
    calculated = sum(item["amount"] for item in data["line_items"])
    
    # 行アイテムの小計確認
    for item in data["line_items"]:
        expected = item["quantity"] * item["unit_price"]
        if abs(item["amount"] - expected) > 0.01:
            errors.append(
                f"Line item amount mismatch: {item['description']} "
                f"(expected {expected:.2f}, got {item['amount']:.2f})"
            )
    
    # 合計の確認
    if abs(data["calculated_total"] - calculated) > 0.01:
        errors.append(
            f"calculated_total mismatch: expected {calculated:.2f}, "
            f"got {data['calculated_total']:.2f}"
        )
    
    # stated_total と calculated_total の乖離チェック
    discrepancy = abs(data["stated_total"] - data["calculated_total"])
    if discrepancy > 1.0:  # 1円以上の誤差
        errors.append(
            f"Total discrepancy: stated={data['stated_total']:.2f}, "
            f"calculated={data['calculated_total']:.2f}"
        )
    
    return len(errors) == 0, errors
```

## 5. Validation-Retry ループ

```python
MAX_RETRIES = 3

def extract_with_retry(invoice_text: str) -> dict:
    messages = [{
        "role": "user",
        "content": f"以下の請求書からデータを抽出してください:\n\n{invoice_text}"
    }]
    
    for attempt in range(MAX_RETRIES):
        response = client.messages.create(
            model="claude-opus-4-5",
            max_tokens=1024,
            tools=[extract_invoice_tool],
            tool_choice={"type": "tool", "name": "extract_invoice"},
            messages=messages
        )
        
        tool_use_block = next(b for b in response.content if b.type == "tool_use")
        data = tool_use_block.input
        
        is_valid, errors = validate_invoice(data)
        
        if is_valid:
            return data
        
        # ★ エラーコンテキストを含めてリトライ
        messages.append({"role": "assistant", "content": response.content})
        messages.append({
            "role": "user",
            "content": (
                f"抽出結果に以下の問題があります:\n"
                + "\n".join(f"- {e}" for e in errors)
                + "\n\n修正して再度抽出してください。"
            )
        })
    
    raise ValueError(f"Extraction failed after {MAX_RETRIES} attempts: {errors}")
```

## 6. Human Review Routing

```python
def route_extraction_result(data: dict, errors: list[str]) -> str:
    """
    検証結果に基づいて処理ルートを決定する
    
    Returns: "auto_process" | "human_review" | "reject"
    """
    # 総額乖離が大きい場合は human review
    discrepancy = abs(data.get("stated_total", 0) - data.get("calculated_total", 0))
    if discrepancy > 100:
        return "human_review"
    
    # 複数エラーがある場合は human review
    if len(errors) > 1:
        return "human_review"
    
    # 請求日不明の場合は human review
    if data.get("invoice_date") is None:
        return "human_review"
    
    # 小額乖離は自動処理
    if discrepancy <= 1.0 and len(errors) == 0:
        return "auto_process"
    
    return "human_review"
```

## 7. Few-Shot でフォーマット制御

```python
messages = [
    {
        "role": "user",
        "content": "請求書1のテキスト..."
    },
    {
        "role": "assistant",
        "content": [
            {
                "type": "tool_use",
                "id": "toolu_example",
                "name": "extract_invoice",
                "input": {
                    "invoice_number": "INV-001",
                    "vendor_name": "Example Corp",
                    # ... 期待する出力例
                }
            }
        ]
    },
    {
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": "toolu_example", "content": "OK"},
            {"type": "text", "text": "次の請求書を処理してください:\n\n{invoice_text}"}
        ]
    }
]
```

## 8. Extended Thinking (拡張思考)

### Extended Thinking とは

モデルが回答前に内部で推論する「思考ステップ」を有効にする機能です。  
複雑な問題や多段階の推論が必要なタスクで精度が向上します。

```python
import anthropic

client = anthropic.Anthropic()

response = client.messages.create(
    model="claude-opus-4-5",
    max_tokens=16000,
    thinking={
        "type": "enabled",
        "budget_tokens": 10000  # 思考に使えるトークン数の上限
    },
    messages=[{
        "role": "user",
        "content": """
        以下の複雑な契約条項を分析して、リスクを評価してください:
        {contract_text}
        """
    }]
)

# 思考ブロックと回答ブロックを分離して処理
for block in response.content:
    if block.type == "thinking":
        # 思考プロセス (デバッグ・監査用)
        print(f"Thinking: {block.thinking}")
    elif block.type == "text":
        # 最終的な回答
        print(f"Answer: {block.text}")
```

### Extended Thinking の活用場面

| 場面 | 効果 |
|---|---|
| 複雑なコードのデバッグ | 多段階の原因分析が正確になる |
| 法的・医療的文書の分析 | 慎重な推論が必要な判断が改善 |
| 数学的・論理的問題 | ステップバイステップの解法 |
| 複数条件の意思決定 | 条件の整理と優先付けが明確 |

### Extended Thinking の注意点

```python
# ★ Extended Thinking 有効時は streaming が必須
with client.messages.stream(
    model="claude-opus-4-5",
    max_tokens=16000,
    thinking={"type": "enabled", "budget_tokens": 5000},
    messages=[{"role": "user", "content": "複雑な分析タスク..."}]
) as stream:
    for event in stream:
        if hasattr(event, "type"):
            if event.type == "content_block_start":
                if event.content_block.type == "thinking":
                    print("Thinking started...")
```

## 9. プロンプトエンジニアリングの高度なテクニック

### XML タグによる構造化

```python
prompt = """
以下の情報を使って顧客への回答を作成してください。

<customer_info>
名前: {customer_name}
顧客ID: {customer_id}
VIPステータス: {is_vip}
</customer_info>

<order_info>
注文ID: {order_id}
金額: ${amount}
ステータス: {status}
</order_info>

<task>
顧客から返金申請がありました。
適切な回答を作成してください。
</task>
"""
```

### Prefill (アシスタントの応答先読み)

モデルの応答の始まりを指定することで、出力フォーマットを制御できます。

```python
response = client.messages.create(
    model="claude-opus-4-5",
    max_tokens=1024,
    messages=[
        {
            "role": "user",
            "content": "注文 ORD-12345 の状況を JSON で教えてください"
        },
        {
            # ★ assistant の応答先読みで JSON を強制
            "role": "assistant",
            "content": "```json\n{"
        }
    ]
)
# レスポンスは { から始まり、JSON 形式になる
```

> **注意**: Prefill は claude.ai では使用できません。API 専用の機能です。

### Chain of Thought (CoT) プロンプティング

```python
cot_prompt = """
顧客からの返金申請を処理してください。

# 手順
以下のステップで考えてください:

<thinking>
1. 注文情報を確認する (注文ID, 金額, 購入日)
2. 返金ポリシーを確認する (30日以内か？)
3. 金額が閾値以下か確認する ($500 以下か？)
4. エスカレーションが必要か判断する
5. 最終的なアクションを決定する
</thinking>

考えた後、顧客への回答と処理アクションを提供してください。
"""
```

### 役割付与 (Role Assignment)

```python
# 専門知識を引き出す役割設定
specialist_system = """
あなたは15年のキャリアを持つシニアセキュリティエンジニアです。
SOC 2 Type II, ISO 27001 の認証経験があり、
クラウドセキュリティアーキテクチャの設計を専門としています。

セキュリティの観点から、潜在的なリスクを見落とさず、
具体的かつ実行可能な推奨事項を提供することに長けています。
"""
```

## 試験で問われやすいパターン

1. **nullable vs optional の設計** – どちらを使うべきか
2. **tool_choice の使い分け** – auto / any / specific tool
3. **semantic validation の必要性** – スキーマ検証だけでは不十分な理由
4. **retry 時のエラーコンテキスト** – 単純リトライ vs エラー情報付きリトライ
5. **human review の判断基準** – どの条件でエスカレーションするか
6. **Extended Thinking の適用場面** – 複雑な推論が必要なタスクとは
7. **Prefill の用途** – JSON 出力強制への活用（API専用）
8. **XML タグの効果** – コンテキスト分離による精度向上
