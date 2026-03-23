# Domain 4: Prompt Engineering & Structured Output

配点: **20%**

## 1. JSON schema 設計: nullable vs optional

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

## 2. `tool_use` + `tool_choice` による structured output

### tool_choice で JSON 出力を強制する

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

## 3. Semantic Validation

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

## 4. Validation-Retry ループ

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

## 5. Human Review Routing

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

## 6. Few-Shot でフォーマット制御

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

## 試験で問われやすいパターン

1. **nullable vs optional の設計** – どちらを使うべきか
2. **tool_choice の使い分け** – auto / any / specific tool
3. **semantic validation の必要性** – スキーマ検証だけでは不十分な理由
4. **retry 時のエラーコンテキスト** – 単純リトライ vs エラー情報付きリトライ
5. **human review の判断基準** – どの条件でエスカレーションするか
