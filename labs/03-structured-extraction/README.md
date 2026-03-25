# Lab 03: Structured Extraction Pipeline

**テーマ**: Schema-first extraction + validation-retry + human review routing  
**カバードメイン**: Domain 4 (Prompt Engineering & Structured Output), Domain 5 (Context & Reliability)

## 学習目標

1. Claude Agent SDK で structured extraction を実装する
2. custom MCP tool を 1 つに絞って構造化出力を強制する
3. nullable / optional の使い分けを理解する
4. `other + detail` パターンを実装する
5. semantic validation (`calculated_total` vs `stated_total`) を実装する
6. エラーコンテキスト付き validation-retry ループを実装する
7. human review routing の判断ロジックを実装する

## 実装するもの

```text
Structured Extraction Pipeline
  ├── InvoiceSchema              JSON schema 定義 (nullable / optional)
  ├── @tool extract_invoice()    Claude Agent SDK の custom tool
  ├── extract_with_retry()       validation エラーを含めた再抽出ループ
  ├── validate_invoice()         semantic validation
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

### 1. Agent SDK で structured extraction を強制する

```python
@tool("extract_invoice", "請求書テキストから構造化データを抽出", INVOICE_SCHEMA)
async def extract_invoice_tool(args):
    return {"content": [{"type": "text", "text": "Structured invoice extraction received for validation."}]}

options = ClaudeAgentOptions(
    system_prompt=SYSTEM_PROMPT,
    mcp_servers={"invoice": create_sdk_mcp_server(name="invoice", tools=[extract_invoice_tool])},
    allowed_tools=["mcp__invoice__extract_invoice"],
    max_turns=4,
)
```

- 旧実装の `tool_choice={"type": "tool", "name": "extract_invoice"}` の代わりに、Agent SDK では **単一の custom tool** を exposed して structured extraction を誘導する
- 抽出された structured data は `ToolUseBlock.input` から受け取る

### 2. nullable vs optional の使い分け

```python
# nullable: フィールドは存在するが null の可能性あり
"invoice_date": {"type": ["string", "null"]}

# optional: フィールド自体が省略可能
"discount_amount": {"type": "number"}
# → required に含めない
```

### 3. semantic validation

```python
for item in line_items:
    expected = item["quantity"] * item["unit_price"]
    assert abs(item["amount"] - expected) < 0.01

expected_calculated = sum(item["amount"] for item in line_items) + (tax_amount or 0)
assert abs(data["calculated_total"] - expected_calculated) < 0.01
```

JSON schema だけでは、金額の整合性までは担保できません。抽出後に semantic validation を入れる必要があります。

### 4. エラーコンテキスト付き retry

```python
prompt = build_retry_prompt(
    invoice_text=invoice_text,
    attempt=attempt,
    previous_data=last_data,
    previous_validation=last_validation,
)
```

- 単純リトライではなく、**前回の抽出結果と validation error** を次回 prompt に含める
- これにより、モデルに「何を直すべきか」を具体的に伝えられる

### 5. human review routing

```python
if len(validation.errors) > 1:
    return "human_review"
if abs(data["stated_total"] - data["calculated_total"]) > 100:
    return "human_review"
if data.get("invoice_date") is None:
    return "human_review"
```

## 試験との対応

| 実装内容 | 試験ドメイン |
|---|---|
| Agent SDK による structured extraction | Domain 4 |
| custom tool + schema design | Domain 4 |
| nullable / optional 設計 | Domain 4 |
| semantic validation | Domain 4 |
| validation-retry ループ | Domain 4 |
| human review routing | Domain 5 |

## 参考

- Claude Agent SDK overview: https://platform.claude.com/docs/en/agent-sdk/overview
- Claude Agent SDK Python reference: https://platform.claude.com/docs/en/agent-sdk/python
