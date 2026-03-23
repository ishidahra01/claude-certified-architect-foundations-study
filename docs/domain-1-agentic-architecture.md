# Domain 1: Agentic Architecture & Orchestration

配点: **27%**（最重要ドメイン）

## 1. Agentic Loop の基本設計

### stop_reason による制御

Claude API の `stop_reason` は agentic loop の心臓部です。

| stop_reason | 意味 | ループの次アクション |
|---|---|---|
| `end_turn` | モデルが処理完了と判断 | ループ終了 |
| `tool_use` | ツール呼び出しリクエスト | ツール実行 → 結果を返す |
| `max_tokens` | 出力上限到達 | エラー処理 or リトライ |
| `stop_sequence` | 停止シーケンス検出 | 条件分岐 |

```python
while True:
    response = client.messages.create(...)
    if response.stop_reason == "end_turn":
        break
    elif response.stop_reason == "tool_use":
        tool_results = execute_tools(response.content)
        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})
```

## 2. Orchestrator / Subagent パターン

### 役割分離

```
Orchestrator (Claude)
  ├── 全体計画の立案
  ├── サブエージェントへの委譲判断
  └── 結果の統合・最終判断

Subagent (Claude)
  ├── 単一タスクの実行
  ├── ツール呼び出し
  └── 結果の返却 (provenance 付き)
```

### 重要原則: 文脈の明示渡し

サブエージェントは **親の文脈を自動継承しない**。  
必要な情報は orchestrator が明示的に渡す。

```python
# NG: 暗黙の文脈継承に依存
subagent_prompt = "この注文を処理して"

# OK: 必要な文脈を明示渡し
subagent_prompt = f"""
顧客ID: {customer_id}
注文ID: {order_id}
処理内容: 返金申請
制約: 閾値 ${REFUND_THRESHOLD} を超える場合はエスカレーション
"""
```

## 3. Hook / Gate による deterministic なガード

### prompt vs hook の使い分け

| 制御方法 | 適した用途 | 例 |
|---|---|---|
| **Prompt** | 文体・形式・トーン・ベストプラクティス | "丁寧語で回答して" |
| **Hook / Gate** | 順序保証・権限・金額閾値 | "認証後でないと refund 不可" |
| **Tool 内ロジック** | ビジネスルール・データ検証 | "在庫ゼロなら注文不可" |

```python
# Hook パターン: ツール内で deterministic にブロック
def process_refund(order_id: str, amount: float) -> dict:
    # ★ Gate: 閾値チェックは prompt ではなく code で担保
    if amount > REFUND_THRESHOLD:
        return {
            "isError": True,
            "error": "ESCALATION_REQUIRED",
            "message": f"Refund amount ${amount} exceeds threshold. Human review required.",
            "requires_human": True
        }
    # 実際の処理
    return execute_refund(order_id, amount)
```

## 4. 並列委譲 (Parallel Dispatch)

### Task を使った並列化

```python
import asyncio

async def research_pipeline(query: str):
    # 並列で複数 subagent に委譲
    tasks = [
        web_search_agent(query),
        doc_analysis_agent(query),
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    
    # Partial failure の処理
    valid_results = []
    for r in results:
        if isinstance(r, Exception):
            valid_results.append({"error": str(r), "source": "unknown"})
        else:
            valid_results.append(r)
    
    return synthesis_agent(query, valid_results)
```

## 5. Escalation の設計

### 自動処理と human review の境界

```python
ESCALATION_CRITERIA = {
    "refund_amount": 500.0,       # 金額閾値
    "confidence_score": 0.7,      # 信頼度閾値
    "consecutive_failures": 3,    # 連続失敗回数
    "sensitive_operation": True,  # 機密操作フラグ
}

def should_escalate(context: dict) -> bool:
    return (
        context.get("amount", 0) > ESCALATION_CRITERIA["refund_amount"]
        or context.get("confidence", 1.0) < ESCALATION_CRITERIA["confidence_score"]
        or context.get("failures", 0) >= ESCALATION_CRITERIA["consecutive_failures"]
    )
```

## 試験で問われやすいパターン

1. **「prompt でのルール指定」vs「hook での強制」** どちらが適切か
2. **subagent への文脈渡し方** – 明示 vs 暗黙継承
3. **partial failure 時の処理** – エラーを伝播させるか graceful degradation か
4. **escalation 基準の設計** – どの条件を deterministic に判断するか
