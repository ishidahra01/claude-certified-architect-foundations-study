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
    
    result = run_agent_sdk_query(
        prompt=f"以下の情報を統合して回答してください:\n\n{context}\n\n質問: {query}"
    )

    return {
        "answer": result,
        "sources_used": [s.source for s in sources if not s.get("error")],
        "sources_failed": [s.agent for s in sources if s.get("error")]
    }
```

> 実装では Claude Agent SDK の `query()` / `ClaudeSDKClient` を使うことが多いが、重要なのは **provenance を保ったまま synthesis する設計** であり、下位 API を直接叩くことではない。

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

## 7. プロンプトキャッシング (Prompt Caching)

### キャッシングとは

長いシステムプロンプトや頻繁に再利用されるコンテキストをキャッシュすることで、  
**コストの削減**と**レスポンス速度の向上**を実現できます。

### キャッシュの基本設定

```python
import anthropic

client = anthropic.Anthropic()

# 長いシステムプロンプトをキャッシュ
response = client.messages.create(
    model="claude-opus-4-5",
    max_tokens=1024,
    system=[
        {
            "type": "text",
            "text": "あなたは顧客サポートエージェントです。以下のマニュアルを参照してください。",
        },
        {
            "type": "text",
            "text": large_manual_text,  # 大きなドキュメント (数千〜数万トークン)
            # ★ cache_control でキャッシュを有効化
            "cache_control": {"type": "ephemeral"}
        }
    ],
    messages=[{
        "role": "user",
        "content": "返金ポリシーを教えてください"
    }]
)
```

### キャッシュの効果

| 指標 | キャッシュなし | キャッシュあり (ヒット時) |
|---|---|---|
| **コスト** | 通常料金 | 入力トークンが約90%削減 |
| **レイテンシ** | 通常 | 大幅に短縮 |
| **有効期間** | - | 5分間 (ephemeral) |

### キャッシュが効果的な場面

```python
# パターン1: 大きなシステムプロンプト
# → 製品マニュアル、FAQ、ガイドラインをキャッシュ

# パターン2: Few-shot Examples
# → 多くの例示をキャッシュして再利用

# パターン3: RAG での検索結果
# → 同じドキュメントを参照する複数のクエリでキャッシュ

# パターン4: マルチターン会話の長いコンテキスト
# → 長い会話履歴をキャッシュして次のターンに再利用

def build_cached_messages(system_doc: str, conversation: list[dict]) -> dict:
    """
    キャッシュを活用したメッセージ構築
    """
    return {
        "system": [
            {
                "type": "text",
                "text": system_doc,
                "cache_control": {"type": "ephemeral"}  # ドキュメントをキャッシュ
            }
        ],
        "messages": conversation
    }
```

### キャッシュの制限事項

```
最小キャッシュサイズ: 
  - Claude Opus, Sonnet: 1,024 トークン以上
  - Claude Haiku: 2,048 トークン以上

有効期間: 5分間 (最後のアクセスから)

対応モデル: Claude 3.5 Haiku 以降のすべてのモデル
  (claude-3-5-haiku, claude-3-5-sonnet, claude-opus-4-5, claude-sonnet-4-5 など)

キャッシュポイントの数: 最大4箇所
```

## 8. コンテキストウィンドウの管理

### モデル別コンテキストウィンドウ

| モデル | コンテキストウィンドウ | 最大出力 |
|---|---|---|
| claude-opus-4-5 | 200K トークン | 32K トークン |
| claude-sonnet-4-5 | 200K トークン | 64K トークン |
| claude-haiku-4-5 | 200K トークン | 8K トークン |

### 長い会話の管理戦略

```python
class ConversationManager:
    """長い会話を管理するクラス"""
    
    MAX_TOKENS = 150000  # コンテキストウィンドウの上限より低めに設定
    
    def __init__(self):
        self.messages = []
        self.token_count = 0
    
    def add_message(self, role: str, content: str):
        """メッセージを追加し、必要に応じて古いメッセージを削除"""
        # 概算トークン数 (実際はトークナイザーを使用)
        estimated_tokens = len(content) // 4
        
        self.messages.append({"role": role, "content": content})
        self.token_count += estimated_tokens
        
        # コンテキスト上限に近い場合、古いメッセージを要約
        if self.token_count > self.MAX_TOKENS * 0.8:
            self._summarize_old_messages()
    
    def _summarize_old_messages(self):
        """古いメッセージを要約して context を節約"""
        if len(self.messages) < 6:
            return
        
        # 最初の数件を要約
        old_messages = self.messages[:4]
        summary_prompt = f"""
        以下の会話履歴を簡潔に要約してください:
        {json.dumps(old_messages, ensure_ascii=False)}
        """
        
        summary_response = client.messages.create(
            model="claude-haiku-4-5",  # 要約には安価なモデルを使用
            max_tokens=500,
            messages=[{"role": "user", "content": summary_prompt}]
        )
        
        summary_text = summary_response.content[0].text
        
        # 要約で置き換え
        self.messages = [
            {"role": "user", "content": f"[会話要約]\n{summary_text}"},
            {"role": "assistant", "content": "了解しました。続きを進めます。"}
        ] + self.messages[4:]
        
        # トークン数を再計算
        self.token_count = sum(len(m["content"]) // 4 for m in self.messages)
```

## 9. エラーリカバリーパターン

### 構造化エラーハンドリング

```python
from enum import Enum
from dataclasses import dataclass

class ErrorType(Enum):
    TIMEOUT = "timeout"
    RATE_LIMIT = "rate_limit"
    INVALID_INPUT = "invalid_input"
    TOOL_FAILURE = "tool_failure"
    CONTEXT_OVERFLOW = "context_overflow"

@dataclass
class AgentError:
    error_type: ErrorType
    message: str
    retryable: bool
    retry_after: float | None = None  # レート制限の場合の待機秒数

def handle_api_error(error: Exception) -> AgentError:
    """API エラーを構造化エラーに変換"""
    error_str = str(error)
    
    if "rate_limit" in error_str.lower():
        return AgentError(
            error_type=ErrorType.RATE_LIMIT,
            message="Rate limit exceeded",
            retryable=True,
            retry_after=60.0
        )
    elif "timeout" in error_str.lower():
        return AgentError(
            error_type=ErrorType.TIMEOUT,
            message="Request timed out",
            retryable=True,
            retry_after=None
        )
    elif "context_length" in error_str.lower():
        return AgentError(
            error_type=ErrorType.CONTEXT_OVERFLOW,
            message="Context window exceeded",
            retryable=False  # コンテキストを削減する必要がある
        )
    else:
        return AgentError(
            error_type=ErrorType.TOOL_FAILURE,
            message=error_str,
            retryable=False
        )

async def resilient_agent_call(
    messages: list[dict],
    max_retries: int = 3
) -> dict:
    """エラーリカバリー付きエージェント呼び出し"""
    for attempt in range(max_retries):
        try:
            response = await client.messages.create_async(
                model="claude-opus-4-5",
                max_tokens=4096,
                messages=messages
            )
            return {"success": True, "response": response}
            
        except Exception as e:
            agent_error = handle_api_error(e)
            
            if not agent_error.retryable:
                return {
                    "success": False,
                    "error": agent_error,
                    "escalate": True
                }
            
            if attempt < max_retries - 1:
                wait_time = agent_error.retry_after or (2 ** attempt)
                await asyncio.sleep(wait_time)
    
    return {
        "success": False,
        "error": AgentError(
            error_type=ErrorType.TOOL_FAILURE,
            message=f"Failed after {max_retries} attempts",
            retryable=False
        ),
        "escalate": True
    }
```

## 試験で問われやすいパターン

1. **lost-in-the-middle の回避** – 重要情報をどこに配置するか
2. **ツール出力のトリミング** – context 圧迫を防ぐ設計
3. **provenance の保持** – multi-agent での情報源追跡
4. **escalation の deterministic な実装** – prompt vs code での制御
5. **partial failure の処理** – 一部失敗時に graceful degradation するか
6. **プロンプトキャッシングの活用** – コスト削減・速度改善の場面
7. **コンテキストウィンドウ管理** – 長い会話での要約戦略
8. **エラーリカバリー** – retryable vs non-retryable の判断
