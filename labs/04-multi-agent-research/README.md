# Lab 04: Multi-Agent Research Pipeline

**テーマ**: Coordinator / Subagent / Provenance / Partial failure  
**カバードメイン**: Domain 1 (Agentic Architecture), Domain 5 (Context & Reliability)

## 学習目標

1. Coordinator / Subagent パターンを実装する
2. 並列委譲 (`asyncio.gather`) と partial failure の処理を実装する
3. Subagent への明示的な文脈渡しを実践する
4. Provenance 付きの情報収集・合成を実装する
5. Timeout と structured error のハンドリングを実装する

## 実装するもの

```
Multi-Agent Research Pipeline
  ├── WebSearchAgent             キーワード検索 + 要約 (provenance 付き)
  ├── DocAnalysisAgent           ドキュメント分析 + key points 抽出
  ├── KnowledgeBaseAgent         既知情報の取得
  └── ResearchCoordinator
        ├── 並列委譲 (asyncio.gather)
        ├── Partial failure 処理 (structured error)
        ├── Provenance 保持した synthesis
        └── 信頼度スコアの計算
```

## 実行方法

```bash
cd labs/04-multi-agent-research
pip install -r requirements.txt
export ANTHROPIC_API_KEY="your-api-key"

# 通常の調査
python main.py --query "Claude Code の plan mode の使い方"

# タイムアウトシミュレーション
python main.py --query "テスト" --simulate-timeout web_search

# 全エージェント失敗のシミュレーション
python main.py --query "テスト" --simulate-all-fail
```

## 設計のポイント

### 1. Subagent への明示的な文脈渡し

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

### 2. Partial failure の graceful degradation

```python
results = await asyncio.gather(*tasks, return_exceptions=True)

for result in results:
    if isinstance(result, Exception):
        # structured error として記録
        failed.append({"error": str(result)})
    else:
        successful.append(result)

# 一部失敗でも処理継続
if successful:
    return synthesize(successful)  # 利用可能な結果で synthesis
```

### 3. Provenance の保持

```python
@dataclass
class SourcedResult:
    content: str
    source: str        # 情報源の識別子
    confidence: float  # 信頼度
    agent: str         # 担当エージェント
```

## 試験との対応

| 実装内容 | 試験ドメイン |
|---|---|
| Coordinator / Subagent パターン | Domain 1 |
| 並列委譲 + partial failure | Domain 1 |
| 明示的な文脈渡し | Domain 1 |
| Provenance 保持 | Domain 5 |
| Structured error | Domain 2 |
| Timeout ハンドリング | Domain 5 |
