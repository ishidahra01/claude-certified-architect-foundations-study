# Domain 5: Context Management & Reliability

配点: **15%**

## 1. Lost-in-the-Middle 対策

### 問題

モデルは会話の**先頭と末尾**を最もよく参照します。  
長いコンテキストの**中間部**は参照されにくくなります（lost-in-the-middle）。

### 対策: 重要情報の配置戦略

```python
def build_messages_with_placement_strategy(
    case_facts: str,
    retrieved_context: str,
    user_query: str
) -> list[dict]:
    """
    重要情報を先頭と末尾に配置する
    """
    return [
        {
            "role": "user",
            "content": (
                # ★ 先頭: 最も重要なルール・制約
                "## 処理ルール (最優先)\n"
                "- 返金上限: $500\n"
                "- 認証済み顧客のみ処理可能\n\n"
                # 中間: 参照コンテキスト (重要度が下がっても OK なもの)
                f"## 参照情報\n{retrieved_context}\n\n"
                # ★ 末尾: 現在の case facts とユーザーの質問
                f"## 現在のケース\n{case_facts}\n\n"
                f"## 質問\n{user_query}"
            )
        }
    ]
```

## 2. Tool 出力のトリミング

### Context Window 節約

```python
def trim_tool_output(output: dict, max_items: int = 5) -> dict:
    """
    ツール出力を必要最小限にトリミングする
    """
    trimmed = output.copy()
    
    # リスト結果は上位 N 件に絞る
    if "orders" in trimmed and len(trimmed["orders"]) > max_items:
        trimmed["orders"] = trimmed["orders"][:max_items]
        trimmed["_truncated"] = True
        trimmed["_total_count"] = len(output["orders"])
    
    # 不要な内部フィールドを除去
    fields_to_remove = ["internal_id", "created_by_system", "raw_data"]
    for field in fields_to_remove:
        trimmed.pop(field, None)
    
    return trimmed

# Tool 実装内でトリミングを適用
def lookup_order_history(customer_id: str) -> dict:
    orders = db.get_all_orders(customer_id)
    
    # ★ そのまま返すと context を圧迫する
    # return {"orders": orders}  # NG: 全件返す
    
    # ★ 必要最小限にトリミング
    summarized = [
        {
            "order_id": o["id"],
            "date": o["created_at"][:10],  # 日付のみ
            "status": o["status"],
            "total": o["total_amount"]
        }
        for o in orders[:10]  # 直近10件
    ]
    return {"orders": summarized, "total": len(orders)}
```

## 3. Provenance の保持

### 情報源の追跡

Multi-agent システムでは、各情報がどこから来たかを追跡することが重要です。

```python
from dataclasses import dataclass
from typing import Optional

@dataclass
class SourcedResult:
    """情報源付きの結果"""
    content: str
    source: str           # 情報源 (URL, document ID, agent name)
    confidence: float     # 信頼度 0.0-1.0
    retrieved_at: str     # 取得日時
    agent: str            # どの subagent が取得したか

def web_search_agent(query: str) -> SourcedResult:
    results = search_web(query)
    return SourcedResult(
        content=results["summary"],
        source=results["url"],
        confidence=results["relevance_score"],
        retrieved_at=datetime.now().isoformat(),
        agent="web_search_agent"
    )

def synthesis_agent(query: str, sources: list[SourcedResult]) -> dict:
    # ★ synthesis でも provenance を維持する
    context = "\n\n".join([
        f"[出典: {s.source} / 信頼度: {s.confidence:.0%}]\n{s.content}"
        for s in sources
        if not s.get("error")  # エラー結果は除外
    ])
    
    response = client.messages.create(
        model="claude-opus-4-5",
        messages=[{
            "role": "user",
            "content": f"以下の情報を統合して回答してください:\n\n{context}\n\n質問: {query}"
        }]
    )
    
    return {
        "answer": response.content[0].text,
        "sources_used": [s.source for s in sources if not s.get("error")],
        "sources_failed": [s.agent for s in sources if s.get("error")]
    }
```

## 4. Escalation の設計

### 自動処理 vs Human Review の境界

```python
class EscalationPolicy:
    """エスカレーション判断のルール集"""
    
    # Deterministic なルール (prompt ではなく code で担保)
    HARD_LIMITS = {
        "max_refund_auto": 500.0,      # この金額を超えたら必ず human review
        "max_retry_count": 3,           # リトライ上限
        "min_confidence": 0.7,          # 信頼度閾値
    }
    
    @classmethod
    def evaluate(cls, context: dict) -> tuple[str, str]:
        """
        Returns: (action, reason)
        action: "auto_process" | "human_review" | "reject"
        """
        # Hard limit チェック
        if context.get("amount", 0) > cls.HARD_LIMITS["max_refund_auto"]:
            return "human_review", f"Amount exceeds auto limit: ${context['amount']}"
        
        if context.get("retry_count", 0) >= cls.HARD_LIMITS["max_retry_count"]:
            return "human_review", "Max retries exceeded"
        
        if context.get("confidence", 1.0) < cls.HARD_LIMITS["min_confidence"]:
            return "human_review", f"Low confidence: {context['confidence']:.0%}"
        
        # Soft check (一定条件を超えたら human review 推奨)
        if context.get("is_vip_customer") and context.get("amount", 0) > 100:
            return "human_review", "VIP customer with significant amount"
        
        return "auto_process", "Within normal parameters"
```

## 5. Scratchpad パターン

### 中間思考の活用

```python
system_prompt = """
あなたは顧客サポートエージェントです。

回答する前に <thinking> タグ内で以下を確認してください:
1. 顧客の問題を正確に把握できているか
2. 必要な情報が揃っているか
3. 次に取るべきアクションは何か
4. エスカレーションが必要か

その後、顧客への回答を <response> タグ内に記述してください。
"""

# モデルの出力例:
# <thinking>
# - 顧客は ORD-12345 の返金を要求している
# - 注文情報を確認する必要がある
# - 金額が不明なため escalation 判断は保留
# </thinking>
# <response>
# ご注文 ORD-12345 について確認いたします。
# </response>
```

## 6. 信頼性向上のためのパターン集

### Partial Failure の graceful degradation

```python
async def research_with_fallback(query: str) -> dict:
    """
    一部の subagent が失敗しても graceful に処理を継続
    """
    tasks = [
        web_search_agent(query),
        doc_analysis_agent(query),
        knowledge_base_agent(query),
    ]
    
    results = await asyncio.gather(*tasks, return_exceptions=True)
    
    successful = []
    failed = []
    
    for i, result in enumerate(results):
        agent_name = ["web_search", "doc_analysis", "knowledge_base"][i]
        if isinstance(result, Exception):
            failed.append({
                "agent": agent_name,
                "error": str(result),
                "error_type": type(result).__name__
            })
        else:
            successful.append(result)
    
    # 結果が 1 件でもあれば処理継続
    if successful:
        return {
            "results": successful,
            "partial_failure": len(failed) > 0,
            "failed_agents": failed
        }
    
    # 全失敗の場合は human review にエスカレーション
    raise AllAgentsFailedError(f"All agents failed: {failed}")
```

## 試験で問われやすいパターン

1. **lost-in-the-middle の回避** – 重要情報をどこに配置するか
2. **ツール出力のトリミング** – context 圧迫を防ぐ設計
3. **provenance の保持** – multi-agent での情報源追跡
4. **escalation の deterministic な実装** – prompt vs code での制御
5. **partial failure の処理** – 一部失敗時に graceful degradation するか
