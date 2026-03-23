# Lab 03: Structured Extraction Pipeline

**テーマ**: Schema-first extraction + validation-retry + human review routing  
**カバードメイン**: Domain 4 (Prompt Engineering & Structured Output), Domain 5 (Context & Reliability)

## 学習目標

1. `tool_use` + `tool_choice` で structured output を強制する
2. nullable/optional の使い分けを理解する
3. `other + detail` パターンを実装する
4. semantic validation (`calculated_total` vs `stated_total`) を実装する
5. エラーコンテキスト付き validation-retry ループを実装する
6. human review routing の判断ロジックを実装する

## 実装するもの

```
Structured Extraction Pipeline
  ├── InvoiceSchema              JSON schema 定義 (nullable/optional)
  ├── extract_invoice()          tool_use + tool_choice: specific
  ├── validate_invoice()         schema + semantic validation
  ├── extract_with_retry()       エラーコンテキスト付き retry ループ
  └── route_result()             human review routing
```

## 実行方法

```bash
cd labs/03-structured-extraction
pip install -r requirements.txt
export ANTHROPIC_API_KEY="your-api-key"

# サンプル請求書で実行
python main.py

# 意図的な不整合を含む請求書で実行
python main.py --scenario discrepancy

# nullable フィールドを含む請求書で実行
python main.py --scenario missing_fields
```

## 設計のポイント

### 1. tool_choice: specific で JSON 出力を強制

```python
response = client.messages.create(
    tools=[extract_invoice_tool],
    tool_choice={"type": "tool", "name": "extract_invoice"},  # 必ず呼び出す
    messages=messages
)
```

### 2. nullable vs optional の使い分け

```python
# nullable: フィールドは必ず存在するが null の可能性あり
"invoice_date": {"type": ["string", "null"]}

# optional (required に含めない): フィールド自体が省略可能
"discount_amount": {"type": "number"}
# → required リストに含めない
```

### 3. semantic validation

JSON スキーマ検証だけでは不十分。計算整合性も確認する。

```python
# 行項目の積算確認
for item in line_items:
    expected = item.qty * item.unit_price
    assert abs(item.amount - expected) < 0.01

# 合計の確認
calculated = sum(item.amount for item in line_items)
discrepancy = abs(stated_total - calculated)
```

## 試験との対応

| 実装内容 | 試験ドメイン |
|---|---|
| tool_use + tool_choice | Domain 4 |
| nullable/optional 設計 | Domain 4 |
| semantic validation | Domain 4 |
| validation-retry ループ | Domain 4 |
| human review routing | Domain 5 |
