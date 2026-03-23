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

### ループ設計のベストプラクティス

- **無限ループ防止**: 最大ステップ数（例: 10〜20回）を設定する
- **tool_use ブロックの完全な返却**: `response.content` をそのまま assistant メッセージに追加する（部分的な追加は API エラーの原因）
- **tool_result の並列返却**: 複数ツールが並列呼び出された場合、全ての結果を1つの user メッセージにまとめて返す

```python
# 複数ツールの並列呼び出し結果をまとめて返す
tool_use_blocks = [b for b in response.content if b.type == "tool_use"]
tool_results = []
for block in tool_use_blocks:
    result = execute_tool(block.name, block.input)
    tool_results.append({
        "type": "tool_result",
        "tool_use_id": block.id,
        "content": result
    })
# ★ 全結果を1つの user メッセージにまとめる
messages.append({"role": "user", "content": tool_results})
```

## 2. エージェントの分類と設計パターン

### エージェントの4種類

Anthropic のドキュメントでは、エージェントが活用するメモリと行動の種類が定義されています。

#### メモリタイプ

| メモリ種別 | 説明 | 実装例 |
|---|---|---|
| **In-context** | 現在の会話コンテキスト内の情報 | messages 配列 |
| **External** | データベース・ファイルシステムへの永続化 | PostgreSQL, Redis, ファイル |
| **In-weights** | モデルの学習済みパラメータ (Fine-tuning) | Fine-tuned model |
| **In-cache** | KV キャッシュによる再利用 | Prompt caching |

#### 行動タイプ

| 行動種別 | 説明 | 例 |
|---|---|---|
| **Storage R/W** | データの読み書き | DB操作, ファイルI/O |
| **Process execution** | プログラム・コマンドの実行 | Terminal, Test runner |
| **UI interaction** | GUIの操作 | Web browser, Desktop app |
| **Service calls** | 外部APIの呼び出し | REST API, Web scraping |
| **Cross-agent** | 他エージェントの生成・呼び出し | Subagent dispatch |

### エージェントアーキテクチャの選択基準

```
シンプルな質問応答          → 単一エージェント (ツールなし)
ツール連携が必要            → 単一エージェント + ツール
複雑なワークフロー          → Orchestrator + Subagent
独立した並列タスク          → 並列 Subagent (Parallel Dispatch)
特化型エキスパートが必要    → 専門 Subagent (Router Pattern)
```

## 3. Orchestrator / Subagent パターン

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

### Subagent の実装パターン

```python
import anthropic

client = anthropic.Anthropic()

def run_subagent(task: str, context: dict, tools: list) -> dict:
    """
    独立したサブエージェントを実行する
    """
    system = """あなたは専門的なサブエージェントです。
    与えられたタスクのみを実行し、結果を構造化して返してください。"""
    
    user_message = f"""
    ## タスク
    {task}
    
    ## コンテキスト
    {context}
    
    ## 制約
    - スコープ外の操作は行わない
    - エラーは詳細に報告する
    - 結果には情報源を含める
    """
    
    response = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=4096,
        system=system,
        tools=tools,
        messages=[{"role": "user", "content": user_message}]
    )
    
    return {
        "result": response.content,
        "stop_reason": response.stop_reason,
        "agent": "subagent",
        "task": task
    }
```

## 4. Hook / Gate による deterministic なガード

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

### Human-in-the-Loop (HITL) の設計

エージェントが自律的に行動する範囲が広いほど、人間の監視が重要になります。

```python
class AgentAction:
    """エージェントのアクション分類"""
    
    # 自動実行可能 (reversible かつ低リスク)
    AUTO_ALLOWED = [
        "read_data",
        "search_web",
        "generate_report",
    ]
    
    # 承認後実行 (irreversible または中リスク)
    REQUIRES_APPROVAL = [
        "send_email",
        "update_customer_data",
        "create_ticket",
    ]
    
    # 人間のみ実行 (高リスク)
    HUMAN_ONLY = [
        "delete_account",
        "process_large_refund",
        "modify_permissions",
    ]

def check_action_permission(action: str, context: dict) -> tuple[str, str]:
    """
    アクションの実行権限を確認
    Returns: ("allow" | "request_approval" | "deny", reason)
    """
    if action in AgentAction.HUMAN_ONLY:
        return "deny", f"Action '{action}' requires human execution"
    
    if action in AgentAction.REQUIRES_APPROVAL:
        return "request_approval", f"Action '{action}' requires human approval"
    
    return "allow", "Action is within automated scope"
```

## 5. 並列委譲 (Parallel Dispatch)

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

### 直列 vs 並列の選択基準

```
直列 (Sequential) が適切な場合:
  - タスク B がタスク A の結果に依存する
  - 状態変更が順序に依存する
  - デバッグ・監査のため実行順序を明確にしたい

並列 (Parallel) が適切な場合:
  - 各タスクが独立している
  - 処理時間の短縮が重要
  - リソースが十分に利用可能
```

## 6. Escalation の設計

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

## 7. 長時間タスクの設計 (Long-running Agents)

### チェックポイントと中断可能な設計

長時間実行するエージェントは、途中で中断・再開できるよう設計することが重要です。

```python
import json
from datetime import datetime

class CheckpointedAgent:
    """チェックポイント付きエージェント"""
    
    def __init__(self, task_id: str):
        self.task_id = task_id
        self.checkpoint_file = f"/tmp/agent_{task_id}_checkpoint.json"
    
    def save_checkpoint(self, state: dict):
        """現在の状態を保存"""
        checkpoint = {
            "task_id": self.task_id,
            "timestamp": datetime.now().isoformat(),
            "state": state
        }
        with open(self.checkpoint_file, "w") as f:
            json.dump(checkpoint, f)
    
    def load_checkpoint(self) -> dict | None:
        """保存された状態を復元"""
        try:
            with open(self.checkpoint_file) as f:
                return json.load(f)
        except FileNotFoundError:
            return None
    
    async def run_with_checkpoints(self, steps: list[callable]):
        """ステップごとにチェックポイントを保存しながら実行"""
        checkpoint = self.load_checkpoint()
        start_step = checkpoint["state"].get("completed_steps", 0) if checkpoint else 0
        
        results = checkpoint["state"].get("results", []) if checkpoint else []
        
        for i, step in enumerate(steps[start_step:], start=start_step):
            try:
                result = await step()
                results.append(result)
                
                # ステップ完了後にチェックポイント保存
                self.save_checkpoint({
                    "completed_steps": i + 1,
                    "results": results
                })
            except Exception as e:
                self.save_checkpoint({
                    "completed_steps": i,
                    "results": results,
                    "last_error": str(e)
                })
                raise
        
        return results
```

### タイムアウトと再試行の設計

```python
import asyncio
from typing import TypeVar, Callable, Awaitable

T = TypeVar("T")

async def with_timeout_and_retry(
    coro_factory: Callable[[], Awaitable[T]],
    timeout_seconds: float = 30.0,
    max_retries: int = 3,
    backoff_base: float = 2.0
) -> T:
    """タイムアウトと指数バックオフリトライ"""
    last_error = None
    
    for attempt in range(max_retries):
        try:
            return await asyncio.wait_for(
                coro_factory(),
                timeout=timeout_seconds
            )
        except asyncio.TimeoutError:
            last_error = TimeoutError(f"Timed out after {timeout_seconds}s")
        except Exception as e:
            last_error = e
        
        if attempt < max_retries - 1:
            wait = backoff_base ** attempt
            await asyncio.sleep(wait)
    
    raise last_error
```

## 8. エージェントの安全性とセキュリティ

### プロンプトインジェクション対策

エージェントは外部データを処理するため、プロンプトインジェクション攻撃に注意が必要です。

```python
def sanitize_tool_output(output: str) -> str:
    """
    ツール出力からプロンプトインジェクションを防ぐ
    外部データは必ずサニタイズしてから context に追加する
    """
    # XML/HTMLタグのエスケープ (誤って命令として解釈されないよう)
    sanitized = output.replace("<", "&lt;").replace(">", "&gt;")
    
    # 疑わしいパターンの検出 (ログ記録)
    suspicious_patterns = [
        "ignore previous instructions",
        "disregard your system prompt",
        "你好", # 言語切り替え試行 (例示)
    ]
    for pattern in suspicious_patterns:
        if pattern.lower() in output.lower():
            log_security_event(f"Suspicious pattern in tool output: {pattern}")
    
    return sanitized

def build_tool_result_message(tool_id: str, raw_output: str) -> dict:
    """ツール結果をセーフに構造化する"""
    return {
        "type": "tool_result",
        "tool_use_id": tool_id,
        "content": [
            {
                "type": "text",
                # ★ 外部データは明示的にデータとしてマーク
                "text": f"<tool_output>\n{sanitize_tool_output(raw_output)}\n</tool_output>"
            }
        ]
    }
```

### 最小権限の原則 (Principle of Least Privilege)

```python
# エージェントに与えるツールは必要最小限に絞る
def get_tools_for_role(role: str) -> list[dict]:
    """役割に応じた最小ツールセット"""
    TOOL_SETS = {
        "read_only_agent": [
            lookup_order_tool,
            get_customer_tool,
            search_knowledge_base_tool,
        ],
        "support_agent": [
            lookup_order_tool,
            get_customer_tool,
            process_small_refund_tool,   # 小額のみ
            create_ticket_tool,
        ],
        "senior_agent": [
            lookup_order_tool,
            get_customer_tool,
            process_refund_tool,         # 金額制限なし (Gate で担保)
            escalate_to_human_tool,
            update_customer_tool,
        ],
    }
    return TOOL_SETS.get(role, TOOL_SETS["read_only_agent"])
```

## 試験で問われやすいパターン

1. **「prompt でのルール指定」vs「hook での強制」** どちらが適切か
2. **subagent への文脈渡し方** – 明示 vs 暗黙継承
3. **partial failure 時の処理** – エラーを伝播させるか graceful degradation か
4. **escalation 基準の設計** – どの条件を deterministic に判断するか
5. **メモリタイプの選択** – in-context / external / in-cache の使い分け
6. **直列 vs 並列委譲** – タスクの依存関係に基づく設計判断
7. **HITL の設計** – どのアクションに人間の承認が必要か
8. **プロンプトインジェクション対策** – 外部データの安全な処理
