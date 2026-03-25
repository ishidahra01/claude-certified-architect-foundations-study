# Lab 04: Multi-Agent Research Pipeline

**テーマ**: Coordinator / Subagent / Dynamic Selection / Iterative Refinement / Provenance  
**カバードメイン**: Domain 1 (Agentic Architecture), Domain 5 (Context & Reliability)

## 学習目標

1. **Hub-and-Spoke** Coordinator / Subagent パターンを実装する
2. Claude Agent SDK の `query()` で **specialized subagent** を起動する
3. **動的サブエージェント選択**: クエリ要件に基づいて必要な subagent だけを起動する
4. **反復改善ループ**: synthesis のギャップを評価し、不十分な場合に再委譲する
5. **明示的な文脈渡し**: subagent への情報は明示的に提供する
6. **並列委譲** (`asyncio.gather`) と partial failure の処理
7. **構造化メタデータ分離**: content と provenance (出典) を分離して渡す

## 実装するもの

```text
Research Coordinator (Hub-and-Spoke)
  │
  ├── analyze_query_requirements()       → query() + output_format で要件分析
  ├── web/doc/kb subagent                → query() + AgentDefinition で専門化
  ├── _dispatch_agents()                 → asyncio.gather による並列委譲
  ├── synthesize_results()               → provenance 付き統合
  └── evaluate_synthesis_coverage()      → ギャップ評価 + targeted re-delegation
```

## 実行方法

```bash
cd labs/04-multi-agent-research
pip install -r requirements.txt
export ANTHROPIC_API_KEY="your-api-key"

# 通常の調査
python main.py --query "Claude Code の plan mode の使い方"

# 学習モード
python main.py --query "テスト" --learn

# 動的選択のデモ
python main.py --show-dynamic-selection

# 反復改善のデモ
python main.py --query "テスト" --show-refinement

# 特定 subagent をタイムアウトさせる
python main.py --query "テスト" --simulate-timeout web_search

# 全 subagent を失敗させる
python main.py --query "テスト" --simulate-all-fail
```

> `main.py` は Claude Agent SDK の async `query()` を複数回オーケストレーションするため、エントリーポイントに `anyio.run(main)` を使っています。

## 設計のポイント

### 1. `query()` を subagent 実行に使う

```python
options = ClaudeAgentOptions(
    agents={
        "web-researcher": AgentDefinition(
            description="Summarizes web findings with provenance and caveats.",
            prompt="You are a web research specialist...",
            model="haiku",
        )
    },
    output_format={"type": "json_schema", "schema": SUBAGENT_RESULT_SCHEMA},
)

async for message in query(
    prompt="Use the web-researcher agent...",
    options=options,
):
    ...
```

- 旧実装の direct `anthropic.Anthropic().messages.create(...)` をやめ、**Coordinator も Subagent も Agent SDK の `query()`** で実行する
- subagent は **独立コンテキスト** なので、必要な evidence を prompt に明示的に埋め込む

### 2. 動的クエリ分析も structured output にする

```python
options = ClaudeAgentOptions(
    output_format={"type": "json_schema", "schema": QUERY_REQUIREMENTS_SCHEMA},
)
```

- `needs_web_search` / `needs_doc_analysis` / `needs_knowledge_base` を構造化して受け取る
- free-form text を JSON parse するより、Agent SDK の `structured_output` を使うほうが安定する

### 3. provenance を content と分離する

```python
@dataclass
class SourcedResult:
    content: str
    source: str
    confidence: float
    agent: str
    retrieved_at: float
```

- synthesis では本文と attribution を別々に prompt に含める
- 出典が不明な情報を混ぜないことで、信頼性評価がしやすくなる

### 4. partial failure の graceful degradation

```python
raw_results = await asyncio.gather(*tasks, return_exceptions=True)
```

- 一部の subagent が timeout / query error でも、成功した結果だけで synthesis を継続する
- 全失敗時のみ escalation にする

## 試験との対応

| 実装内容 | 試験 Task Statement |
|---|---|
| Hub-and-Spoke Coordinator / Subagent パターン | Task 1.2 |
| 動的クエリ分析 + 選択的 subagent 起動 | Task 1.2 |
| `query()` による specialized subagent 実行 | Task 1.2 |
| 反復改善ループ (ギャップ評価 + 再委譲) | Task 1.2 |
| 並列委譲 (`asyncio.gather`) + partial failure | Task 1.2, 1.3 |
| 明示的な文脈渡し (暗黙継承なし) | Task 1.3 |
| 構造化メタデータ分離 (content / provenance) | Task 1.3 |
| Provenance 保持した synthesis | Task 1.3, 5.x |
| Structured error + Timeout ハンドリング | Task 1.2 |

## 参考

- Claude Agent SDK overview: https://platform.claude.com/docs/en/agent-sdk/overview
- Claude Agent SDK Python reference: https://platform.claude.com/docs/en/agent-sdk/python
