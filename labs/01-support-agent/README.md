# Lab 01: Customer Support Agent

**テーマ**: Agentic loop + Tool calling + Escalation gate  
**カバードメイン**: Domain 1 (Agentic Architecture), Domain 2 (Tool Design), Domain 5 (Context & Reliability)

## 学習目標

1. `stop_reason` による agentic loop の制御を理解する
2. 単一責任のツール設計と `isError` パターンを実装する
3. Hook / Gate による deterministic な escalation を実装する
4. Prompt vs Code での制約の使い分けを体験する

## 実装するもの

```
Customer Support Agent
  ├── get_customer(customer_id)         顧客情報取得 (read-only)
  ├── lookup_order(order_id)            注文情報取得 (read-only)
  ├── process_refund(order_id, amount)  返金処理 + 閾値 Gate
  └── escalate_to_human(reason, ctx)   人間へのエスカレーション
```

## 実行方法

```bash
cd labs/01-support-agent
pip install -r requirements.txt
export ANTHROPIC_API_KEY="your-api-key"

# 通常の返金シナリオ
python main.py --scenario normal

# 閾値超え → escalation シナリオ
python main.py --scenario escalation

# 認証失敗シナリオ
python main.py --scenario auth_fail
```

## 設計のポイント

### 1. Hook / Gate による返金閾値チェック

```python
# NG: prompt だけで制御
# "返金は $500 以下のみ承認してください"

# OK: code で deterministic に制御
REFUND_THRESHOLD = 500.0

def process_refund(order_id: str, amount: float) -> dict:
    if amount > REFUND_THRESHOLD:
        return {
            "isError": True,
            "error": "ESCALATION_REQUIRED",
            "requires_human": True,
        }
    return execute_refund(order_id, amount)
```

### 2. stop_reason によるループ制御

```python
while True:
    response = client.messages.create(...)
    
    if response.stop_reason == "end_turn":
        break  # 完了
    elif response.stop_reason == "tool_use":
        tool_results = execute_tools(response)
        messages.extend([...])  # 結果を追加して継続
    else:
        break  # max_tokens 等
```

### 3. isError のパターン

- `not found` → `isError: true, retryable: false` (別アクションに誘導)
- `network error` → `isError: true, retryable: true` (リトライ可能)
- `threshold exceeded` → `isError: true, requires_human: true` (エスカレーション)

## 試験との対応

| 実装内容 | 試験ドメイン |
|---|---|
| agentic loop + stop_reason | Domain 1 |
| tool 設計と description | Domain 2 |
| isError パターン | Domain 2 |
| Hook / Gate による閾値制御 | Domain 1, 2 |
| escalation の deterministic 実装 | Domain 5 |
