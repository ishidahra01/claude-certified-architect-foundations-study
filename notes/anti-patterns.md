# アンチパターン集

試験で「最も不適切な選択肢」として登場しやすいパターンです。

## AP-01: Prompt だけで業務ルールを強制する

### ❌ アンチパターン

```python
system_prompt = """
返金は $500 以下の場合のみ承認してください。
$500 を超える場合は人間にエスカレーションしてください。
"""
# ↑ prompt は無視される可能性があり、強制力がない
```

### ✅ 正しいパターン

```python
def process_refund(order_id: str, amount: float) -> dict:
    if amount > REFUND_THRESHOLD:  # code で deterministic に制御
        return {"isError": True, "requires_escalation": True}
    return execute_refund(order_id, amount)
```

**なぜ NG か**: prompt はモデルのベストエフォート実行であり、エッジケースで破られる可能性があります。金銭的・法的影響のある制約は code で担保する必要があります。

---

## AP-02: 神ツール (God Tool) の実装

### ❌ アンチパターン

```python
{
    "name": "customer_operations",
    "description": "顧客に関するすべての操作を実行する",
    "input_schema": {
        "action": {"type": "string", "enum": ["get", "update", "delete", "refund", ...]}
    }
}
```

### ✅ 正しいパターン

```python
# 単一責任のツール群に分割
tools = [
    {"name": "get_customer", ...},
    {"name": "update_customer_email", ...},
    {"name": "process_refund", ...},
]
```

**なぜ NG か**: 1 つのツールに多くの action を詰め込むと、モデルが正しい action を選択するための context が不足します。description の質が下がり、誤ったツール使用が増えます。

---

## AP-03: Subagent への暗黙の文脈継承に依存する

### ❌ アンチパターン

```python
# orchestrator が持つ文脈を subagent が勝手に使えると仮定する
subagent_prompt = "この顧客の注文をキャンセルして"
# ↑ 「この顧客」が誰か subagent には伝わらない
```

### ✅ 正しいパターン

```python
subagent_prompt = f"""
顧客ID: {customer_id}
注文ID: {order_id}
理由: {cancellation_reason}
制約: VIP 顧客の場合は確認が必要
"""
```

**なぜ NG か**: Subagent は独立したコンテキストで動作します。orchestrator の会話履歴や変数は自動的に共有されません。

---

## AP-04: Retry 時にエラーコンテキストを渡さない

### ❌ アンチパターン

```python
for attempt in range(MAX_RETRIES):
    result = extract_data(text)
    if is_valid(result):
        return result
    # ↑ 単純リトライ: 同じエラーが繰り返される
```

### ✅ 正しいパターン

```python
for attempt in range(MAX_RETRIES):
    result = extract_data(text, previous_errors=errors)
    is_valid_flag, errors = validate(result)
    if is_valid_flag:
        return result
    # エラー情報を次のリトライに渡す
```

**なぜ NG か**: エラーコンテキストなしのリトライは同じ失敗を繰り返します。モデルがなぜ失敗したかを理解して修正するには、エラー情報を会話に追加する必要があります。

---

## AP-05: ツール出力をそのまま返す (Context 肥大化)

### ❌ アンチパターン

```python
def get_order_history(customer_id: str) -> dict:
    return db.get_all_orders(customer_id)  # 数百件の全件返す
```

### ✅ 正しいパターン

```python
def get_order_history(customer_id: str) -> dict:
    orders = db.get_all_orders(customer_id)
    return {
        "recent_orders": [summarize(o) for o in orders[:10]],
        "total_count": len(orders)
    }
```

**なぜ NG か**: ツール出力がそのまま context に追加されます。大量データを返すと context window を圧迫し、lost-in-the-middle を悪化させます。

---

## AP-06: Provenance なしの synthesis

### ❌ アンチパターン

```python
def synthesize(results: list[str]) -> str:
    combined = "\n".join(results)
    return generate_summary(combined)
    # ↑ どの情報がどこから来たか追跡不能
```

### ✅ 正しいパターン

```python
def synthesize(results: list[SourcedResult]) -> dict:
    return {
        "summary": generate_summary_with_sources(results),
        "sources": [r.source for r in results],
        "confidence": min(r.confidence for r in results)
    }
```

**なぜ NG か**: 合成結果の信頼性を評価できなくなります。誤情報が混入した場合に追跡・修正ができません。

---

## AP-07: CLAUDE.md のみでの制御 (code 不要と誤解)

### ❌ 誤解

```
CLAUDE.md に書けばすべてのルールが強制される
→ code での実装は不要
```

### ✅ 正しい理解

```
CLAUDE.md: Claude Code の動作のガイドライン (ベストエフォート)
Code / Gate: ビジネスルールの deterministic な強制
```

**なぜ NG か**: CLAUDE.md は Claude Code が参照するガイドラインです。アプリケーションのビジネスロジックを CLAUDE.md で代替することはできません。

---

## AP-08: structured output の方式を取り違える

### ❌ アンチパターン

```python
# Agent SDK なのに、下位 API の tool_choice だけを唯一の正解だと思い込む
# あるいは Claude API なのに auto のままで structured output を期待する
```

### ✅ 正しいパターン

```python
# Agent SDK:
options = ClaudeAgentOptions(
    output_format={"type": "json_schema", "schema": schema},
)

# Claude API:
response = client.messages.create(
    tools=[extract_schema],
    tool_choice={"type": "tool", "name": "extract_invoice"},
    messages=[...],
)
```

**なぜ NG か**: structured output には複数の実装レイヤーがあります。Agent SDK なら `output_format` や custom tool、Claude API なら `tool_choice` を使い分ける必要があります。`tool_choice: auto` のままでは、API レイヤーでは自然言語応答に逃げる可能性があります。
