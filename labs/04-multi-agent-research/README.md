# Lab 04: Multi-Agent Research Pipeline

**テーマ**: Coordinator / Subagent / Dynamic Selection / Iterative Refinement / Provenance  
**カバードメイン**: Domain 1 (Agentic Architecture), Domain 5 (Context & Reliability)

## 学習目標

1. **Hub-and-Spoke** Coordinator / Subagent パターンを実装する (Task 1.2)
2. **動的サブエージェント選択**: クエリ要件に基づいて必要な subagent だけを起動する (Task 1.2)
3. **反復改善ループ**: synthesis のギャップを評価し、不十分な場合に再委譲する (Task 1.2)
4. **明示的な文脈渡し**: subagent への情報は明示的に提供する (Task 1.3)
5. **並列委譲** (`asyncio.gather`) と partial failure の処理 (Task 1.3)
6. **構造化メタデータ分離**: content と provenance (出典) を分離して渡す (Task 1.3)
7. Timeout と structured error のハンドリング (Task 1.2)

## 実装するもの

```
Research Coordinator (Hub-and-Spoke)
  │
  ├── [Step 1] analyze_query_requirements()
  │     └── Claude がクエリを分析 → 必要な subagent を動的に決定
  │
  ├── [Step 2] _dispatch_agents() - 並列委譲 (asyncio.gather)
  │     ├── WebSearchAgent      → Web 検索 + 要約 (必要な場合のみ)
  │     ├── DocAnalysisAgent    → ドキュメント分析 (必要な場合のみ)
  │     └── KnowledgeBaseAgent  → KB 検索 (必要な場合のみ)
  │           ★ サブエージェント同士は直接通信しない (Hub経由のみ)
  │
  ├── [Step 3] synthesize_results() - Provenance 保持した統合
  │
  └── [Step 4] evaluate_synthesis_coverage() + 反復改善ループ
        └── ギャップがある場合 → 対象を絞ったクエリで再委譲
```

## 実行方法

```bash
cd labs/04-multi-agent-research
pip install -r requirements.txt
export ANTHROPIC_API_KEY="your-api-key"

# ────── 基本シナリオ ──────

# 通常の調査 (Claude が動的に subagent を選択)
python main.py --query "Claude Code の plan mode の使い方"

# ────── 学習モード ──────

# --learn フラグ: 各設計判断の WHY を解説しながら実行
python main.py --query "テスト" --learn

# ────── 動的選択のデモ ──────

# 3種類のクエリで subagent の選択が変わることを確認
python main.py --show-dynamic-selection

# ────── 反復改善のデモ ──────

# synthesis 評価ループを強制的に実行 (1回以上の再委譲を確認)
python main.py --query "テスト" --show-refinement

# ────── フォールト トレランス テスト ──────

# 特定 subagent をタイムアウトさせる
python main.py --query "テスト" --simulate-timeout web_search

# 全 subagent を失敗させる (escalation required)
python main.py --query "テスト" --simulate-all-fail
```

## 設計のポイント

### 1. Hub-and-Spoke アーキテクチャ (Task 1.2)

```
                  ┌─────────────────────┐
                  │     Coordinator     │  ← 全ての情報フローを管理
                  └──────────┬──────────┘
                   ┌─────────┼─────────┐
                   │         │         │
            ┌──────▼──┐ ┌────▼───┐ ┌──▼──────┐
            │WebSearch│ │DocAnal │ │  KBase  │
            └─────────┘ └────────┘ └─────────┘
            ★ サブエージェント同士は互いに通信しない
```

**なぜ Hub-and-Spoke か**: サブエージェント間の直接通信は可観測性を下げる。コーディネーター経由にすることで、エラーハンドリングと情報フローを一元管理できる。

### 2. 動的クエリ分析 (Task 1.2)

```python
# 毎回全パイプラインを通すのではなく、クエリに応じて選択
reqs = analyze_query_requirements(query, client)

# "最新ニュースを教えて" → web_search のみ
# "仕様書を確認して"    → doc_analysis のみ
# "FAQ を調べて"        → knowledge_base のみ
# "総合的に調査して"    → 全エージェント
```

**なぜ動的選択か**: 毎回全パイプラインを通すと、単純な質問に対してもリソースを無駄遣いする。クエリ要件に基づいて必要なエージェントだけ起動する。

### 3. 反復改善ループ (Task 1.2)

```python
MAX_REFINEMENT = 2

for i in range(MAX_REFINEMENT):
    synthesis = synthesize_results(query, sources, client)
    
    eval_result = evaluate_synthesis_coverage(query, synthesis, client)
    if eval_result.is_sufficient:
        break  # 十分なカバレッジ → 終了
    
    # ギャップに対して対象を絞ったクエリで再委譲
    for targeted_query in eval_result.targeted_queries:
        additional_sources = await _dispatch_agents(targeted_query, ...)
        sources.extend(additional_sources)
```

**なぜ反復改善か**: 1回の synthesis では情報が不足することがある。コーディネーターがギャップを評価して必要な場合だけ再委譲することで、品質と効率のバランスを取る。

### 4. 明示的な文脈渡し (Task 1.3)

```python
# NG: 暗黙の継承に依存
subagent_prompt = "この質問を調査して"

# OK: 必要な情報を明示的に渡す
subagent_prompt = f"""
調査クエリ: {query}
対象ドキュメント: {document_id}
必要な情報: {required_info}
出力形式: structured JSON with provenance
"""
```

**なぜ明示渡しか**: Subagent は独立したコンテキストで動作する。Coordinator の会話履歴や変数は自動的に共有されない。

### 5. 構造化メタデータ分離 (Task 1.3)

```python
@dataclass
class SourceMetadata:
    """コンテンツとメタデータを分離"""
    source_url: str    # 出典 URL
    agent_name: str    # 担当エージェント
    confidence: float  # 信頼度
    retrieved_at: float

# Synthesis 時にコンテンツと出典を分けて渡す
for result in sources:
    content_parts.append(result.content)
    metadata_parts.append(SourceMetadata(...))
```

**なぜ分離するか**: コンテンツと出典を混在させると、モデルが attribution (どの情報がどこから来たか) を追跡しにくくなる。分離することで信頼性評価と誤情報追跡が可能になる。

### 6. Partial failure の graceful degradation (Task 1.2)

```python
results = await asyncio.gather(*tasks, return_exceptions=True)

for result in results:
    if isinstance(result, Exception):
        failed.append(AgentError(...))  # structured error として記録
    else:
        successful.append(result)

# 一部失敗でも処理継続 (全失敗時のみ escalation)
if successful:
    return synthesize(successful)
```

## 試験との対応

| 実装内容 | 試験 Task Statement |
|---|---|
| Hub-and-Spoke Coordinator / Subagent パターン | Task 1.2 |
| 動的クエリ分析 + 選択的 subagent 起動 | Task 1.2 |
| 反復改善ループ (ギャップ評価 + 再委譲) | Task 1.2 |
| 全通信を Coordinator 経由でルーティング | Task 1.2 |
| 並列委譲 (asyncio.gather) + partial failure | Task 1.2, 1.3 |
| 明示的な文脈渡し (暗黙継承なし) | Task 1.3 |
| 構造化メタデータ分離 (content / provenance) | Task 1.3 |
| Provenance 保持した synthesis | Task 1.3, 5.x |
| Structured error + Timeout ハンドリング | Task 1.2 |
