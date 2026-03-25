# Domain 1: Agentic Architecture & Orchestration

配点: **27%**（最重要ドメイン）

---

## Task 1.1: Agentic Loop の設計と実装

### なぜ重要か

Agentic loop は Claude がツールを使って自律的に問題を解くための基本メカニズムです。ループの制御を誤ると、無限ループ・早期終了・会話履歴の破損といった致命的なバグが発生します。試験でも「ループをどこで止めるか」「ツール結果をどう返すか」が頻出です。

### stop_reason ライフサイクル

| stop_reason | 意味 | ループの次アクション |
|---|---|---|
| `end_turn` | モデルが処理完了と判断 | **ループ終了** |
| `tool_use` | ツール呼び出しリクエスト | **ツール実行 → 結果を返す** |
| `max_tokens` | 出力上限到達 | エラー処理 or リトライ |
| `stop_sequence` | 停止シーケンス検出 | 条件分岐 |

### ✅ 正しい Agentic Loop の実装

```python
import anthropic

client = anthropic.Anthropic()

def run_agentic_loop(messages: list, tools: list, max_iterations: int = 20) -> str:
    """
    正しい agentic loop:
    - stop_reason == "tool_use" → ツール実行して続行
    - stop_reason == "end_turn" → 正常終了
    - 安全弁として max_iterations を設定
    """
    for iteration in range(max_iterations):
        response = client.messages.create(
            model="claude-opus-4-5",
            max_tokens=4096,
            tools=tools,
            messages=messages,
        )

        # assistant の応答をそのまま履歴に追加（部分追加はAPIエラーの原因）
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            # モデルが完了と判断 → 正常終了
            return extract_text(response.content)

        if response.stop_reason == "tool_use":
            # 全ての tool_use ブロックを処理して1つの user メッセージにまとめる
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    result = execute_tool(block.name, block.input)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    })
            # ★ 複数ツールの結果は必ず1つの user メッセージにまとめる
            messages.append({"role": "user", "content": tool_results})
            continue

    # 安全弁発動（通常はここに到達しない）
    raise RuntimeError(f"Loop did not terminate within {max_iterations} iterations")
```

### ❌ アンチパターン

```python
# ❌ アンチパターン1: テキスト内容でループ終了を判断する
response_text = get_text_from_response(response)
if "完了しました" in response_text:  # 自然言語シグナルに依存 → 不確実
    break

# ❌ アンチパターン2: イテレーション上限を主要な停止メカニズムにする
for i in range(5):  # stop_reason を無視して回数だけで制御
    response = client.messages.create(...)
    execute_tools_if_any(response)
# → end_turn を処理しないため、完了後も無駄に API を呼び続ける

# ❌ アンチパターン3: assistant コンテンツを部分的にしか返さない
text_only = [b for b in response.content if b.type == "text"]
messages.append({"role": "assistant", "content": text_only})
# → tool_use ブロックが欠落し API エラー

# ✅ 正しくは stop_reason でのみ制御する
if response.stop_reason == "end_turn":
    break
elif response.stop_reason == "tool_use":
    # ツール実行して続行
    ...
```

### ツール結果を会話履歴に追加する仕組み

```
Turn 1: user → "注文 #123 の状態を調べて"
Turn 2: assistant → [text: "調べます", tool_use: {id: "tu_1", name: "lookup_order", input: {order_id: "123"}}]
Turn 3: user → [tool_result: {tool_use_id: "tu_1", content: "配送中、到着予定: 明日"}]
Turn 4: assistant → [text: "注文 #123 は配送中で明日到着予定です"] (stop_reason: "end_turn")
```

---

## Task 1.2: Multi-agent Systems (Coordinator-Subagent)

### なぜ重要か

単一エージェントでは対応困難な複雑タスク（大規模リサーチ、並列専門処理）には Multi-agent が必要です。ただし設計を誤ると、情報の断絶・重複作業・エラー伝播が起きます。**Hub-and-spoke アーキテクチャ**でコーディネーターを中心に据えることが鍵です。

### Hub-and-Spoke アーキテクチャ

```
                  ┌─────────────────┐
                  │  Coordinator    │
                  │  (Hub)          │
                  │  ・タスク分解    │
                  │  ・委譲判断      │
                  │  ・結果統合      │
                  │  ・エラー処理    │
                  └────────┬────────┘
                           │ ALL inter-subagent communication
              ┌────────────┼────────────┐
              ▼            ▼            ▼
        ┌──────────┐ ┌──────────┐ ┌──────────┐
        │Subagent A│ │Subagent B│ │Subagent C│
        │(Web検索) │ │(DB照会)  │ │(分析)    │
        └──────────┘ └──────────┘ └──────────┘
```

**重要原則**: サブエージェント同士は直接通信しない。必ずコーディネーター経由。

### ✅ Coordinator の実装

```python
import anthropic
import json

client = anthropic.Anthropic()

# コーディネーターが使うツール（サブエージェントを呼び出すための Task ツールを含む）
coordinator_tools = [
    {
        "name": "Task",
        "description": "サブエージェントを起動して専門タスクを委譲する",
        "input_schema": {
            "type": "object",
            "properties": {
                "description": {"type": "string", "description": "タスクの説明"},
                "prompt": {"type": "string", "description": "サブエージェントへの完全な指示"},
            },
            "required": ["description", "prompt"],
        },
    },
    {
        "name": "synthesize_results",
        "description": "収集した調査結果を統合して最終回答を生成する",
        "input_schema": {
            "type": "object",
            "properties": {
                "findings": {"type": "array", "items": {"type": "string"}},
                "query": {"type": "string"},
            },
            "required": ["findings", "query"],
        },
    },
]

def run_coordinator(user_query: str) -> str:
    """
    コーディネーターがクエリを分析し、
    必要なサブエージェントを動的に選択して委譲する。
    常に全パイプラインを実行するわけではない。
    """
    system_prompt = """あなたはリサーチコーディネーターです。
    
    役割:
    - ユーザーのクエリを分析し、必要なサブタスクを特定する
    - 適切なサブエージェントに委譲する（常に全員に委譲するわけではない）
    - 全サブエージェント間の通信はあなたを経由する
    - 結果を統合して最終回答を生成する
    
    重要: タスクの範囲が狭すぎると重要情報を見落とす。
    カバレッジを確保しつつ重複を最小化すること。
    """
    
    messages = [{"role": "user", "content": user_query}]
    
    while True:
        response = client.messages.create(
            model="claude-opus-4-5",
            max_tokens=8192,
            system=system_prompt,
            tools=coordinator_tools,
            messages=messages,
        )
        
        messages.append({"role": "assistant", "content": response.content})
        
        if response.stop_reason == "end_turn":
            return extract_text(response.content)
        
        if response.stop_reason == "tool_use":
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    if block.name == "Task":
                        # サブエージェントを実行
                        result = run_subagent(block.input["prompt"])
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": json.dumps(result, ensure_ascii=False),
                        })
                    elif block.name == "synthesize_results":
                        result = synthesize(block.input["findings"], block.input["query"])
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result,
                        })
            messages.append({"role": "user", "content": tool_results})
```

### ❌ アンチパターン vs ✅ ベストプラクティス

```python
# ❌ サブエージェント同士が直接通信する
subagent_a_result = run_subagent_a(query)
subagent_b_result = run_subagent_b(subagent_a_result)  # A → B 直接連携
# → コーディネーターが状況を把握できない、エラー処理が困難

# ✅ 全通信をコーディネーター経由にする
result_a = coordinator_delegates_to(subagent_a, query)
coordinator_evaluates(result_a)  # ギャップを確認
result_b = coordinator_delegates_to(subagent_b, refined_query_based_on_a)
final = coordinator_synthesizes([result_a, result_b])

# ❌ タスク分解が狭すぎる（重要領域を見落とす）
tasks = [
    "2020年のデータのみを調べる",  # 前後の文脈が欠落
]

# ✅ 十分なカバレッジを確保する
tasks = [
    "2019〜2021年のトレンド全体を調べ、2020年に焦点を当てる",
    "2020年の特異な要因（COVID-19等）の影響を分析する",
]
```

### 反復的な精緻化ループ

```python
def coordinator_with_refinement(query: str) -> str:
    """
    コーディネーターが合成結果にギャップを発見したら
    ターゲットを絞った追加調査を委譲する。
    """
    initial_findings = delegate_research(query)
    synthesis = synthesize(initial_findings)
    
    # ギャップチェック
    gaps = identify_gaps(synthesis, query)
    if gaps:
        # ギャップを埋めるための追加調査
        additional = delegate_research(gaps)
        synthesis = synthesize(initial_findings + additional)
    
    return synthesis
```

---

## Task 1.3: Subagent の起動・コンテキスト渡し・スポーン

### なぜ重要か

サブエージェントは**独立したコンテキスト**で動作します。親（コーディネーター）の会話履歴を自動的に継承しません。必要な情報を明示的に渡さないと、サブエージェントは「何も知らない状態」でタスクを実行することになります。

### Task ツールによるサブエージェント起動

```python
# コーディネーターが allowedTools に "Task" を含む必要がある
coordinator_config = {
    "model": "claude-opus-4-5",
    "tools": [
        {"name": "Task", ...},   # ← これがないとサブエージェントを起動できない
        {"name": "web_search", ...},
    ],
}
```

### ✅ コンテキストの明示渡し

```python
# ❌ 暗黙の文脈継承に依存（サブエージェントには何も伝わらない）
bad_prompt = "顧客の問題を調査して"

# ✅ 必要な全コンテキストをプロンプトに含める
def build_subagent_prompt(
    prior_findings: list[dict],
    research_goal: str,
    quality_criteria: str,
    scope: str,
) -> str:
    prior_context = "\n".join([
        f"[{f['source']}] {f['content']}"
        for f in prior_findings
    ])
    
    return f"""## リサーチ目標
{research_goal}

## 品質基準
{quality_criteria}

## 調査スコープ
{scope}

## 前エージェントの調査結果（コンテキスト）
{prior_context}

## 指示
上記のコンテキストを踏まえ、スコープ内の未カバー領域を重点的に調査してください。
手順を指定するのではなく、目標と基準を満たす方法をあなた自身が判断してください。
"""
```

### 並列サブエージェントのスポーン

```python
# ✅ 1回のコーディネーター応答で複数の Task ツールを同時に呼び出す（並列実行）
# コーディネーターのレスポンスに複数の tool_use ブロックが含まれる形になる

coordinator_system = """
複数の独立したリサーチタスクがある場合は、
1回の応答で複数の Task ツールを同時に呼び出して並列実行してください。
"""

# コーディネーターが生成するレスポンスイメージ:
parallel_tool_calls = [
    {
        "type": "tool_use",
        "id": "tu_1",
        "name": "Task",
        "input": {
            "description": "北米市場調査",
            "prompt": build_subagent_prompt(
                prior_findings=[],
                research_goal="北米市場でのEV普及率と主要プレイヤーを調査",
                quality_criteria="2023年以降のデータ、信頼性の高いソース",
                scope="北米（米国・カナダ・メキシコ）のみ",
            ),
        },
    },
    {
        "type": "tool_use",
        "id": "tu_2",
        "name": "Task",
        "input": {
            "description": "欧州市場調査",
            "prompt": build_subagent_prompt(
                prior_findings=[],
                research_goal="欧州市場でのEV普及率と主要プレイヤーを調査",
                quality_criteria="2023年以降のデータ、信頼性の高いソース",
                scope="欧州（EU + 英国）のみ",
            ),
        },
    },
]
# → 両タスクは並列実行される
```

### 構造化データで出典情報を分離する

```python
# ✅ コンテンツとメタデータ（出典）を構造化して分離する
subagent_result = {
    "findings": [
        {
            "content": "2023年の全世界EV販売台数は1,400万台に達した",
            "metadata": {
                "source_url": "https://iea.org/reports/ev-outlook-2024",
                "doc_name": "IEA Global EV Outlook 2024",
                "page_number": 12,
                "retrieved_at": "2024-06-01",
            },
        },
        {
            "content": "中国が全世界販売の60%を占める",
            "metadata": {
                "source_url": "https://iea.org/reports/ev-outlook-2024",
                "doc_name": "IEA Global EV Outlook 2024",
                "page_number": 15,
                "retrieved_at": "2024-06-01",
            },
        },
    ],
    "coverage_gaps": ["南米市場のデータが不足"],
}
```

### AgentDefinition / ClaudeAgentOptions の設定

```python
from claude_agent_sdk import AgentDefinition, ClaudeAgentOptions

options = ClaudeAgentOptions(
    agents={
        "market-research-agent": AgentDefinition(
            description="特定地域の市場データを調査・分析する専門エージェント",
            prompt=(
                "あなたは市場調査の専門家です。"
                "与えられたスコープ内のデータのみを調査し、"
                "全ての発見事項に出典（URL、文書名、ページ番号）を付けてください。"
                "スコープ外のトピックには踏み込まないでください。"
            ),
            tools=["Read", "Grep", "WebSearch"],
            model="sonnet",
        ),
    },
)
```

---

## Task 1.4: 多ステップワークフローの強制とハンドオフパターン

### なぜ重要か

「返金処理の前に本人確認を必ずする」のような要件を**プロンプトだけで実現しようとすると失敗率がゼロにならない**です。金融・医療・法的な操作では決定論的な強制が必須です。また、エスカレーション時には人間エージェントが会話履歴なしでも対応できる構造化ハンドオフが必要です。

### プログラム的強制 vs プロンプトベースガイダンス

| アプローチ | 失敗率 | 適した用途 |
|---|---|---|
| **プロンプト指示** | 非ゼロ（確率的） | トーン・スタイル・ベストプラクティス |
| **プログラム的ゲート** | ゼロ（決定論的） | 本人確認・金額閾値・権限チェック |

### ✅ プログラム的な前提条件ゲート

```python
from dataclasses import dataclass
from typing import Optional

@dataclass
class CustomerVerification:
    customer_id: str
    verified: bool
    verification_method: str

# グローバルな検証状態（実際はセッション/DBで管理）
_verified_customers: dict[str, CustomerVerification] = {}

def get_customer(customer_id: str) -> dict:
    """顧客情報を取得し、検証済みとしてマークする"""
    customer = fetch_from_db(customer_id)
    _verified_customers[customer_id] = CustomerVerification(
        customer_id=customer_id,
        verified=True,
        verification_method="database_lookup",
    )
    return customer

def process_refund(customer_id: str, order_id: str, amount: float) -> dict:
    """
    ★ プログラム的ゲート:
    get_customer が先に呼ばれていない限り、process_refund はブロックされる。
    プロンプト指示ではなく、コードで保証する。
    """
    # 前提条件チェック（決定論的）
    if customer_id not in _verified_customers:
        return {
            "isError": True,
            "error": "PREREQUISITE_NOT_MET",
            "message": (
                f"顧客 {customer_id} の本人確認が完了していません。"
                "先に get_customer を呼び出してください。"
            ),
            "required_action": "call_get_customer_first",
        }
    
    verification = _verified_customers[customer_id]
    if not verification.verified:
        return {
            "isError": True,
            "error": "VERIFICATION_FAILED",
            "message": "顧客の本人確認に失敗しました。",
        }
    
    # ゲートを通過した場合のみ実際の処理
    return execute_refund(customer_id, order_id, amount)
```

### 並列調査と共有コンテキスト

```python
import asyncio

async def investigate_multi_concern_request(customer_id: str, issues: list[str]) -> dict:
    """
    複数の懸念事項を並列で調査し、共有コンテキストで統合する。
    例: 「請求エラー」「配送遅延」「商品破損」を同時調査
    """
    # 各懸念事項を独立したタスクとして並列実行
    investigation_tasks = [
        investigate_issue(customer_id, issue)
        for issue in issues
    ]
    results = await asyncio.gather(*investigation_tasks, return_exceptions=True)
    
    # 共有コンテキストで統合
    shared_context = {
        "customer_id": customer_id,
        "investigations": [
            r if not isinstance(r, Exception) else {"error": str(r), "issue": issues[i]}
            for i, r in enumerate(results)
        ],
    }
    return shared_context
```

### ✅ 構造化ハンドオフサマリー

```python
def create_handoff_summary(
    customer_id: str,
    conversation_findings: dict,
    recommended_action: str,
) -> dict:
    """
    人間エージェントが会話履歴なしで対応できるよう、
    全ての必要情報を構造化サマリーに含める。
    """
    return {
        "handoff_summary": {
            # 顧客識別情報
            "customer_id": customer_id,
            "customer_name": conversation_findings.get("customer_name"),
            "account_tier": conversation_findings.get("account_tier"),
            
            # 根本原因分析
            "root_cause": conversation_findings.get("root_cause"),
            "contributing_factors": conversation_findings.get("factors", []),
            
            # 財務情報
            "refund_amount": conversation_findings.get("refund_amount"),
            "affected_orders": conversation_findings.get("affected_orders", []),
            
            # 推奨アクション
            "recommended_action": recommended_action,
            "urgency": conversation_findings.get("urgency", "normal"),
            
            # 引継ぎ理由
            "escalation_reason": conversation_findings.get("escalation_reason"),
            "attempted_resolutions": conversation_findings.get("attempts", []),
        }
    }
```

---

## Task 1.5: Agent SDK / Claude Code Hooks によるツール呼び出しインターセプション

### なぜ重要か

フックは**決定論的な保証**を提供します。プロンプト指示はモデルが従わない可能性がありますが、フックはコードレベルで強制されるため失敗率はゼロです。異なるソースからの異種データ形式の正規化や、ポリシー違反アクションのブロックに不可欠です。

> **重要**: 試験で出てくる「hooks」には **2つの文脈** があります。  
> - **Claude Agent SDK**: `ClaudeAgentOptions(hooks=...)` に **Python callback** を登録する  
> - **Claude Code**: `.claude/settings.json` に **command hook** を登録し、shell script / command を実行する

### Claude Agent SDK の Python hooks

```python
from claude_agent_sdk import ClaudeAgentOptions
from claude_agent_sdk.types import HookMatcher

options = ClaudeAgentOptions(
    hooks={
        "PreToolUse": [
            HookMatcher(
                matcher="mcp__support__process_refund",
                hooks=[pre_tool_use_refund_check],
            )
        ],
        "PostToolUse": [
            HookMatcher(
                matcher="mcp__support__lookup_order|mcp__support__process_refund",
                hooks=[post_tool_use_audit_and_normalize],
            )
        ],
    },
)
```

- **PreToolUse**: 実行前に allow / deny を返せるため、ポリシー強制に向く
- **PostToolUse**: 監査ログ、出力正規化、追加コンテキスト注入に向く
- Lab 01 ではこの Python hooks パターンを採用している

### Claude Code hooks の仕組み

Claude Code の hooks は、**設定ファイルに登録した外部コマンド**がエージェントのライフサイクルの各ポイントで自動実行されます。

```
ツール呼び出し要求
       ↓
 [PreToolUse フック]   ← .claude/settings.json で登録したコマンドが stdin でイベントを受け取る
       ↓                   exit 0 = 許可, exit 2 = ブロック
 ツール実行
       ↓
 [PostToolUse フック]  ← ツール実行後に自動実行（監査ログ、通知など）
       ↓
 モデルへ返却
```

**コミュニケーション方式**:
- **入力**: イベントデータが **stdin** に JSON 形式で渡される
- **出力**: **終了コード** でアクションを制御する
  - `exit 0` → 許可（処理を継続）
  - `exit 2` → **ブロック**（ツール呼び出しを中止してエラーをモデルに通知）
  - 他の値 → 警告（処理は継続、stderr のメッセージがユーザーに表示）

### ✅ `.claude/settings.json` によるフック設定

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "process_refund",
        "hooks": [
          {
            "type": "command",
            "command": ".claude/hooks/pre_tool_use_refund.sh"
          }
        ]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": ".claude/hooks/post_tool_use_audit.sh"
          }
        ]
      }
    ]
  }
}
```

- **`matcher`**: ツール名にマッチする正規表現（`""` = 全ツール、`"Write|Edit"` = Write か Edit のみ）
- **`type`**: `"command"` (シェルコマンド)、`"http"` (HTTPエンドポイント)、`"prompt"` (LLM判断) など
- 設定ファイルの場所: プロジェクト固有 → `.claude/settings.json` / ユーザー全体 → `~/.claude/settings.json`

### ✅ PreToolUse フック: ポリシー違反ブロック

フックスクリプトは **stdin** でイベントの JSON を受け取り、終了コードでアクションを制御します。

```bash
#!/usr/bin/env bash
# .claude/hooks/pre_tool_use_refund.sh
# 返金額が自動承認上限を超える場合にツール呼び出しをブロックする

set -euo pipefail

# stdin からイベントデータを読み込む
input=$(cat)

TOOL_NAME=$(echo "$input" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('tool_name',''))")
AMOUNT=$(echo "$input" | python3 -c "
import sys, json
d = json.load(sys.stdin)
print(d.get('tool_input', {}).get('amount', 0))
")

REFUND_LIMIT=500

if [ "$TOOL_NAME" = "process_refund" ]; then
    # awk で浮動小数点比較（bc より可搬性が高い）
    if awk -v amount="$AMOUNT" -v limit="$REFUND_LIMIT" 'BEGIN { exit (amount > limit) ? 0 : 1 }'; then
        echo "ポリシー違反: 返金額 \$$AMOUNT が自動承認上限 \$$REFUND_LIMIT を超過しています。" >&2
        exit 2  # ★ Claude にツール実行をブロックさせる
    fi
fi

exit 0  # 許可
```

### ✅ PostToolUse フック: 監査ログの記録

```bash
#!/usr/bin/env bash
# .claude/hooks/post_tool_use_audit.sh
# 全ツール実行を監査ログに記録する（PostToolUse は exit code によるブロック不可）

set -euo pipefail

input=$(cat)

TOOL_NAME=$(echo "$input" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('tool_name','unknown'))")
TIMESTAMP=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
LOG_FILE="${CLAUDE_PROJECT_DIR:-.}/.claude/hooks/audit.log"

echo "[$TIMESTAMP] tool_used=$TOOL_NAME" >> "$LOG_FILE"

exit 0
```

### 利用可能なフックイベント一覧

| イベント | 発火タイミング | ブロック可否 | 主な用途 |
|---|---|---|---|
| **PreToolUse** | ツール実行前 | ✅ 可 | セキュリティチェック、ポリシー強制 |
| **PostToolUse** | ツール実行後 | ❌ 不可 | 監査ログ、フォーマット、通知 |
| **SessionStart** | セッション開始時 | ❌ 不可 | コンテキスト初期化、ログ開始 |
| **Stop** | エージェント応答完了時 | ✅ 可 | 最終バリデーション、レポート生成 |
| **Notification** | 通知送信時 | ❌ 不可 | Slack 通知、メールアラート |
| **SubagentStart** | サブエージェント起動時 | ❌ 不可 | サブエージェントの追跡 |

### フック vs プロンプト指示の比較

| | プロンプト指示 | Hook |
|---|---|---|
| **コンプライアンス** | 確率的（失敗あり） | 決定論的（必ず実行） |
| **実装方法** | システムプロンプト | Agent SDK: Python callback / Claude Code: `.claude/settings.json` + スクリプト |
| **適用タイミング** | モデルが解釈する時 | ツール呼び出しの前後（コードレベル） |
| **用途** | スタイル・ヒューリスティック | セキュリティ・監査・変換 |

### フックの選択基準まとめ

```
Hook を使う場合:
  ✅ 返金額の上限チェック（決定論的保証が必要）
  ✅ 監査ログの記録（全ツール呼び出しを漏れなく記録）
  ✅ レート制限・スロットリング
  ✅ 危険なコマンドのブロック（rm -rf など）
  ✅ CI/CD との連携（フォーマット、テスト自動実行）

プロンプト指示で十分な場合:
  ✅ 回答の言語・トーン
  ✅ 出力フォーマット（Markdown vs プレーンテキスト）
  ✅ ベストプラクティスの推奨
```

---

## Task 1.6: タスク分解戦略

### なぜ重要か

複雑なタスクを適切に分解しないと、モデルの注意が希釈されて品質が低下します。**固定シーケンシャルパイプライン**（プロンプトチェイニング）と**動的適応分解**はそれぞれ適した用途があり、選択を誤るとコストと品質の両方が悪化します。

### パターン比較

| パターン | 適した用途 | 特徴 |
|---|---|---|
| **プロンプトチェイニング** | 予測可能な多面的レビュー | 固定ステップ、並列化容易 |
| **動的適応分解** | オープンエンドな調査 | 発見に基づいてサブタスクを生成 |

### ✅ プロンプトチェイニング: 大規模コードレビュー

```python
def large_codebase_review(files: list[str]) -> dict:
    """
    大規模コードレビューを2パスで実行:
    1. ファイル別ローカルパス（並列実行可能）
    2. クロスファイル統合パス（依存関係・アーキテクチャ）
    
    なぜ分けるか: 全ファイルを1回に渡すと注意が希釈される
    """
    # Pass 1: 各ファイルを独立してレビュー（並列実行）
    local_reviews = {}
    for filepath in files:
        code = read_file(filepath)
        local_reviews[filepath] = review_single_file(
            filepath=filepath,
            code=code,
            focus="ローカルなバグ・スタイル・セキュリティ問題のみ。他ファイルとの関係は無視。",
        )
    
    # Pass 2: クロスファイル統合パス（Pass 1の結果を全て渡す）
    integration_review = review_integration(
        local_reviews=local_reviews,
        focus=[
            "モジュール間の依存関係の問題",
            "アーキテクチャレベルの懸念",
            "インターフェースの不整合",
            "重複実装",
        ],
    )
    
    return {
        "per_file_issues": local_reviews,
        "integration_issues": integration_review,
        "summary": generate_review_summary(local_reviews, integration_review),
    }


def review_single_file(filepath: str, code: str, focus: str) -> dict:
    response = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=2048,
        messages=[{
            "role": "user",
            "content": f"ファイル: {filepath}\n\nフォーカス: {focus}\n\n```\n{code}\n```",
        }],
    )
    return parse_review(response)
```

### ✅ 動的適応分解: オープンエンドな調査

```python
def adaptive_investigation(problem_statement: str) -> dict:
    """
    オープンエンドな調査の動的分解:
    1. まず全体構造をマッピング
    2. 高影響領域を特定
    3. 発見に基づいて優先化された調査プランを生成
    4. 依存関係が発見されるたびにプランを適応
    """
    # ステップ1: 構造マッピング
    structure = map_problem_structure(problem_statement)
    
    # ステップ2: 高影響領域の特定
    high_impact_areas = identify_high_impact(structure)
    
    # ステップ3: 優先化されたプランを動的生成
    investigation_plan = create_prioritized_plan(
        structure=structure,
        high_impact_areas=high_impact_areas,
    )
    
    results = {}
    for area in investigation_plan:
        # 各調査結果に基づいてプランを適応
        result = investigate_area(area, context=results)
        results[area["id"]] = result
        
        # 新たな依存関係が発見された場合、プランを更新
        new_dependencies = find_new_dependencies(result, investigation_plan)
        if new_dependencies:
            investigation_plan = update_plan(investigation_plan, new_dependencies)
    
    return compile_findings(results)


def create_prioritized_plan(structure: dict, high_impact_areas: list) -> list:
    """
    固定ステップではなく、発見内容に基づいて
    優先化されたサブタスクリストを動的生成する。
    """
    response = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=2048,
        messages=[{
            "role": "user",
            "content": f"""
問題構造: {structure}
高影響領域: {high_impact_areas}

上記に基づいて、優先化された調査計画を作成してください。
各サブタスクには: id, description, priority, dependencies を含めてください。
            """,
        }],
    )
    return parse_investigation_plan(response)
```

### 分解パターンの選択ガイド

```
プロンプトチェイニングを選ぶ場合:
  ✅ ステップが予測可能（コードレビュー、文書要約、翻訳チェック）
  ✅ 各ステップが独立していて並列化できる
  ✅ パイプラインが安定していてメンテナンスしやすい

動的適応分解を選ぶ場合:
  ✅ 何を調べるべきかが事前にわからない（バグ調査、競合分析）
  ✅ 発見内容が次のステップに影響する
  ✅ スコープが広く、優先順位付けが必要
```

---

## Task 1.7: セッション状態・再開・フォーク

### なぜ重要か

長時間実行エージェントや複数の仮説を並列探索するシナリオでは、セッション状態の管理が重要です。**チェックポイント**で途中から再開でき、**フォーク**で共通ベースラインから分岐した複数のアプローチを探索できます。

### チェックポイントベースの状態保存と再開

```python
import json
import os
from datetime import datetime, timezone
from dataclasses import dataclass, asdict
from typing import Any, Optional

@dataclass
class SessionCheckpoint:
    session_id: str
    checkpoint_name: str
    created_at: str
    messages: list[dict]
    metadata: dict[str, Any]
    step_index: int
    completed_steps: list[str]


class SessionManager:
    """セッション状態の保存・再開・フォーク管理"""
    
    def __init__(self, storage_dir: str = "/tmp/agent_sessions"):
        self.storage_dir = storage_dir
        os.makedirs(storage_dir, exist_ok=True)
    
    def save_checkpoint(
        self,
        session_id: str,
        checkpoint_name: str,
        messages: list[dict],
        metadata: dict,
        step_index: int,
        completed_steps: list[str],
    ) -> str:
        """現在のセッション状態をチェックポイントとして保存"""
        checkpoint = SessionCheckpoint(
            session_id=session_id,
            checkpoint_name=checkpoint_name,
            created_at=datetime.now(tz=timezone.utc).isoformat(),
            messages=messages,
            metadata=metadata,
            step_index=step_index,
            completed_steps=completed_steps,
        )
        
        checkpoint_path = os.path.join(
            self.storage_dir,
            f"{session_id}_{checkpoint_name}.json",
        )
        with open(checkpoint_path, "w", encoding="utf-8") as f:
            json.dump(asdict(checkpoint), f, ensure_ascii=False, indent=2)
        
        return checkpoint_path
    
    def resume_session(
        self,
        session_id: str,
        checkpoint_name: str = "latest",
    ) -> Optional[SessionCheckpoint]:
        """名前付きセッションから再開"""
        if checkpoint_name == "latest":
            checkpoint_name = self._find_latest_checkpoint(session_id)
            if not checkpoint_name:
                return None
        
        checkpoint_path = os.path.join(
            self.storage_dir,
            f"{session_id}_{checkpoint_name}.json",
        )
        
        if not os.path.exists(checkpoint_path):
            return None
        
        with open(checkpoint_path, encoding="utf-8") as f:
            data = json.load(f)
        
        return SessionCheckpoint(**data)
    
    def fork_session(
        self,
        source_session_id: str,
        source_checkpoint: str,
        fork_name: str,
    ) -> str:
        """
        既存のセッション状態から新しいセッションをフォーク。
        共通ベースラインから異なるアプローチを並列探索するために使用。
        """
        source = self.resume_session(source_session_id, source_checkpoint)
        if not source:
            raise ValueError(f"Source checkpoint not found: {source_session_id}/{source_checkpoint}")
        
        # フォークされたセッションIDを生成
        forked_session_id = f"{source_session_id}_fork_{fork_name}"
        
        # 共通ベースラインの状態でフォークを保存
        self.save_checkpoint(
            session_id=forked_session_id,
            checkpoint_name="initial",
            messages=list(source.messages),  # コピー（独立したコンテキスト）
            metadata={
                **source.metadata,
                "forked_from": f"{source_session_id}/{source_checkpoint}",
                "fork_name": fork_name,
            },
            step_index=source.step_index,
            completed_steps=list(source.completed_steps),
        )
        
        return forked_session_id
    
    def _find_latest_checkpoint(self, session_id: str) -> Optional[str]:
        """セッションの最新チェックポイントを探す"""
        checkpoints = [
            f for f in os.listdir(self.storage_dir)
            if f.startswith(f"{session_id}_") and f.endswith(".json")
        ]
        if not checkpoints:
            return None
        # タイムスタンプで最新を選択
        latest = sorted(checkpoints)[-1]
        return latest.replace(f"{session_id}_", "").replace(".json", "")
```

### ✅ チェックポイント付きエージェント実行

```python
def run_agent_with_checkpoints(
    session_id: str,
    initial_task: str,
    steps: list[callable],
    resume_from: Optional[str] = None,
) -> dict:
    """
    チェックポイントを使って長時間エージェントを実行。
    中断から再開できる。
    """
    manager = SessionManager()
    
    # 既存セッションから再開、または新規開始
    checkpoint = manager.resume_session(session_id, resume_from or "latest")
    
    if checkpoint:
        messages = checkpoint.messages
        start_step = checkpoint.step_index
        completed = checkpoint.completed_steps
        print(f"セッション '{session_id}' をチェックポイント '{checkpoint.checkpoint_name}' から再開")
    else:
        messages = [{"role": "user", "content": initial_task}]
        start_step = 0
        completed = []
        print(f"セッション '{session_id}' を新規開始")
    
    results = {}
    for i, step_fn in enumerate(steps[start_step:], start=start_step):
        step_name = step_fn.__name__
        
        result, messages = step_fn(messages)
        results[step_name] = result
        completed.append(step_name)
        
        # ステップ完了後にチェックポイント保存
        manager.save_checkpoint(
            session_id=session_id,
            checkpoint_name=f"after_step_{i}_{step_name}",
            messages=messages,
            metadata={"task": initial_task},
            step_index=i + 1,
            completed_steps=completed,
        )
    
    return results
```

### ✅ フォークによる並列アプローチ探索

```python
import asyncio

async def explore_divergent_approaches(
    base_session_id: str,
    base_checkpoint: str,
    approaches: list[dict],
) -> dict:
    """
    共通ベースラインから複数のアプローチを並列でフォーク探索。
    例: 同じ初期調査結果から異なる仮説を並列検証。
    """
    manager = SessionManager()
    
    # 各アプローチのフォークセッションを作成
    fork_ids = []
    for approach in approaches:
        fork_id = manager.fork_session(
            source_session_id=base_session_id,
            source_checkpoint=base_checkpoint,
            fork_name=approach["name"],
        )
        fork_ids.append((fork_id, approach))
    
    # 並列で各フォークを実行
    async def run_fork(fork_id: str, approach: dict):
        return await run_approach_async(fork_id, approach)
    
    tasks = [run_fork(fid, appr) for fid, appr in fork_ids]
    fork_results = await asyncio.gather(*tasks, return_exceptions=True)
    
    # 結果を比較して最良のアプローチを選択
    return {
        approach["name"]: result
        for (_, approach), result in zip(fork_ids, fork_results)
        if not isinstance(result, Exception)
    }


# 使用例: セキュリティ調査で複数の仮説を並列検証
async def security_investigation():
    manager = SessionManager()
    
    # 初期調査を実行してベースラインチェックポイントを保存
    base_session = "security_audit_2024"
    initial_findings = await run_initial_scan(base_session)
    manager.save_checkpoint(
        session_id=base_session,
        checkpoint_name="after_initial_scan",
        messages=initial_findings["messages"],
        metadata={"scope": "full_audit"},
        step_index=1,
        completed_steps=["initial_scan"],
    )
    
    # 発見された脆弱性カテゴリごとに並列調査をフォーク
    results = await explore_divergent_approaches(
        base_session_id=base_session,
        base_checkpoint="after_initial_scan",
        approaches=[
            {"name": "sql_injection", "hypothesis": "SQLインジェクション脆弱性の深掘り"},
            {"name": "auth_bypass", "hypothesis": "認証バイパスの可能性を調査"},
            {"name": "data_exposure", "hypothesis": "機密データ露出リスクを評価"},
        ],
    )
    
    return results
```

---

## 試験対策: 重要ポイントまとめ

### Task 1.1 のポイント
- **ループ終了条件**: `stop_reason == "end_turn"` のみ（テキスト解析・回数制限を主要停止手段にしない）
- **`response.content` を丸ごと** assistant メッセージに追加する（部分追加は API エラー）
- 複数ツール結果は**1つの user メッセージ**にまとめる
- 最大イテレーション数は**セーフティネット**（主要停止手段ではない）

### Task 1.2 のポイント
- **Hub-and-spoke**: サブエージェント間の直接通信は禁止、必ずコーディネーター経由
- コーディネーターは**動的に**必要なサブエージェントを選択（常に全パイプライン実行しない）
- タスク分解が**狭すぎる**と重要領域のカバレッジが欠ける
- コーディネーターはギャップを評価して**反復的に精緻化**する

### Task 1.3 のポイント
- コーディネーターが `allowedTools` に **"Task"** を含めないとサブエージェントを起動できない
- サブエージェントのコンテキストは**プロンプトに明示的に含める**（自動継承なし）
- **1回の応答**で複数 Task ツールを呼び出すことで並列実行
- コンテンツとメタデータ（出典URL・ページ番号）を**構造化して分離**する

### Task 1.4 のポイント
- **プロンプト指示**のコンプライアンスは確率的（失敗率ゼロではない）
- 金融・医療等の重要操作には**プログラム的ゲート**（コードレベル強制）を使う
- エスカレーション時のハンドオフには、人間が会話履歴なしで対応できる**構造化サマリー**が必要
- 複数懸念事項の調査は**並列**で実行して効率化

### Task 1.5 のポイント
- **SDK フックは `.claude/settings.json` で設定する**（Python 関数を自前実装して呼び出すのではない）
- **PreToolUse フック**: `exit 2` でツール呼び出しをブロックしてポリシーを強制（stdin でイベント JSON を受け取る）
- **PostToolUse フック**: ツール実行後に監査ログや通知を実行（ブロック不可）
- フックはプロンプト指示より**確実**（決定論的）
- 監査・ログ記録にはフックが最適（漏れなし）
- 参照: https://platform.claude.com/docs/en/agent-sdk/hooks

### Task 1.6 のポイント
- **プロンプトチェイニング**: 予測可能なステップ（コードレビュー等）
- **動的適応分解**: 発見に基づいてプランが変わるオープンエンド調査
- 大規模コードレビューは**ファイル別ローカルパス + クロスファイル統合パス**の2段階
- 全ファイルを一度に渡すと**注意希釈**が起きる

### Task 1.7 のポイント
- **チェックポイント**: セッションIDと名前でステートを保存・復元
- **再開**: 名前付きチェックポイントから途中のステップを再開
- **フォーク**: 共通ベースラインから異なるアプローチを並列探索（独立コンテキスト）
- フォークされたセッションは親セッションと**独立した状態**を持つ
