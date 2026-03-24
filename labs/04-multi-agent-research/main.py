"""
Lab 04: Multi-Agent Research Pipeline

学習目標:
- Coordinator / Subagent パターン (Hub-and-Spoke)
- 動的クエリ分析: Claude がどの subagent を呼ぶかを決定 (Task 1.2)
- 並列委譲 (asyncio.gather) + partial failure の処理
- Subagent への明示的な文脈渡し (暗黙の継承に頼らない)
- Provenance 付き情報収集・合成 (構造化メタデータ分離)
- 反復改善ループ: synthesis のギャップ評価と再委譲 (Task 1.2)
- Timeout と structured error のハンドリング
"""

import asyncio
import json
import os
import argparse
import time
from dataclasses import dataclass, field, asdict
from typing import Any

import anthropic

from dotenv import load_dotenv
load_dotenv()

# ────────────────────────────────────────────────
# データモデル
# ────────────────────────────────────────────────


@dataclass
class SourcedResult:
    """Provenance (情報源) 付きの調査結果"""
    content: str
    source: str           # 情報源の識別子 (URL, doc ID, agent name)
    confidence: float     # 信頼度 0.0-1.0
    agent: str            # 担当 subagent の名前
    retrieved_at: float   # 取得時刻 (Unix timestamp)
    key_points: list[str] = field(default_factory=list)


@dataclass
class SourceMetadata:
    """
    コンテンツとメタデータを分離した構造化形式。
    ★ 構造化データフォーマット分離: content と provenance を明示的に分ける。
    """
    source_url: str
    agent_name: str
    confidence: float
    retrieved_at: float


@dataclass
class AgentError:
    """Subagent のエラーを structured に表現する"""
    agent: str
    error_type: str       # "timeout" | "api_error" | "validation_error"
    message: str
    is_retryable: bool
    failed_at: float      # エラー発生時刻


@dataclass
class ResearchResult:
    """調査全体の結果"""
    query: str
    synthesis: str
    sources_used: list[SourcedResult]
    sources_failed: list[AgentError]
    overall_confidence: float
    is_partial: bool      # 一部の subagent が失敗した場合 True
    refinement_iterations: int = 0  # 反復改善の回数


@dataclass
class QueryRequirements:
    """analyze_query_requirements() の戻り値"""
    needs_web_search: bool
    needs_doc_analysis: bool
    needs_knowledge_base: bool
    reasoning: str


@dataclass
class SynthesisCoverageEval:
    """evaluate_synthesis_coverage() の戻り値"""
    is_sufficient: bool
    gaps: list[str]
    targeted_queries: list[str]


# ────────────────────────────────────────────────
# 擬似データ (実際の web search / doc analysis の代わり)
# ────────────────────────────────────────────────

MOCK_WEB_RESULTS = {
    "default": {
        "summary": "Web 検索により関連する情報を取得しました。",
        "url": "https://example.com/article",
        "relevance": 0.85,
    }
}

MOCK_DOCS = {
    "default": {
        "content": "ドキュメント分析により重要な情報を抽出しました。",
        "doc_id": "DOC-2024-001",
        "relevance": 0.90,
    }
}

MOCK_KB = {
    "default": {
        "content": "ナレッジベースから既知の情報を取得しました。",
        "kb_id": "KB-ARCH-001",
        "relevance": 0.75,
    }
}

# ────────────────────────────────────────────────
# 学習用ユーティリティ
# ────────────────────────────────────────────────


def learn(msg: str, enabled: bool) -> None:
    """--learn フラグが有効な場合のみ教育ノートを表示する"""
    if enabled:
        print(f"\n🎓 [LEARN] {msg}\n")


# ────────────────────────────────────────────────
# Dynamic Query Analysis (Task 1.2: 動的選択)
# ────────────────────────────────────────────────


def analyze_query_requirements(
    query: str,
    client: anthropic.Anthropic,
    verbose: bool = True,
    learn_mode: bool = False,
) -> QueryRequirements:
    """
    ★ Hub-and-Spoke: Coordinator がクエリを分析してどの subagent を起動するか決める。
    サブエージェント同士は互いに通信しない — 全ての判断はここを通る。

    Claude にクエリを見せて、必要な subagent を JSON で返してもらう。
    """
    # ★ なぜ動的選択か (--learn で表示)
    learn(
        "なぜ動的選択か: 毎回全パイプラインを通すと、単純な質問に対してもリソースを無駄遣いする。"
        "クエリ要件に基づいて必要なエージェントだけ起動する",
        learn_mode,
    )

    if verbose:
        print(f"  [Coordinator] Analyzing query requirements with Claude...")

    response = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=256,
        messages=[
            {
                "role": "user",
                "content": (
                    "あなたは調査パイプラインのコーディネーターです。\n"
                    "以下のクエリに対して、どの情報源が必要かを判断してください。\n\n"
                    f"クエリ: {query}\n\n"
                    "利用可能な情報源:\n"
                    "- web_search: 最新情報・ニュース・外部記事の検索\n"
                    "- doc_analysis: 社内ドキュメント・仕様書の分析\n"
                    "- knowledge_base: 既存の FAQ・ナレッジベースの検索\n\n"
                    "以下の JSON 形式だけで回答してください (説明不要):\n"
                    '{"needs_web_search": true/false, '
                    '"needs_doc_analysis": true/false, '
                    '"needs_knowledge_base": true/false, '
                    '"reasoning": "理由を1文で"}'
                ),
            }
        ],
    )

    raw = response.content[0].text.strip()
    # JSON ブロックが ```json ... ``` で囲まれていた場合も対応
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    parsed = json.loads(raw)

    reqs = QueryRequirements(
        needs_web_search=bool(parsed.get("needs_web_search", True)),
        needs_doc_analysis=bool(parsed.get("needs_doc_analysis", True)),
        needs_knowledge_base=bool(parsed.get("needs_knowledge_base", True)),
        reasoning=parsed.get("reasoning", ""),
    )

    if verbose:
        selected = [
            name
            for name, needed in [
                ("web_search", reqs.needs_web_search),
                ("doc_analysis", reqs.needs_doc_analysis),
                ("knowledge_base", reqs.needs_knowledge_base),
            ]
            if needed
        ]
        print(f"  [Coordinator] Selected agents: {selected}")
        print(f"  [Coordinator] Reasoning: {reqs.reasoning}")

    return reqs


# ────────────────────────────────────────────────
# Iterative Refinement Evaluation (Task 1.2: 反復改善)
# ────────────────────────────────────────────────


def evaluate_synthesis_coverage(
    query: str,
    synthesis: str,
    client: anthropic.Anthropic,
    verbose: bool = True,
    learn_mode: bool = False,
) -> SynthesisCoverageEval:
    """
    ★ Hub-and-Spoke: Coordinator が synthesis の品質を評価し、
    ギャップがあれば subagent への追加委譲クエリを生成する。
    サブエージェントはこの評価結果を直接受け取らない — Coordinator 経由。
    """
    # ★ なぜ反復改善か (--learn で表示)
    learn(
        "なぜ反復改善か: 1回の synthesis では情報が不足することがある。"
        "コーディネーターがギャップを評価して再委譲する",
        learn_mode,
    )

    if verbose:
        print(f"  [Coordinator] Evaluating synthesis coverage...")

    response = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=512,
        messages=[
            {
                "role": "user",
                "content": (
                    "以下の調査クエリと合成結果を評価してください。\n\n"
                    f"調査クエリ: {query}\n\n"
                    f"合成結果:\n{synthesis}\n\n"
                    "合成結果にカバレッジのギャップがあるかどうかを判断し、"
                    "以下の JSON 形式だけで回答してください (説明不要):\n"
                    '{"is_sufficient": true/false, '
                    '"gaps": ["不足している情報1", "不足している情報2"], '
                    '"targeted_queries": ["ギャップ1のための具体的クエリ", "ギャップ2のための具体的クエリ"]}'
                ),
            }
        ],
    )

    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    parsed = json.loads(raw)

    eval_result = SynthesisCoverageEval(
        is_sufficient=bool(parsed.get("is_sufficient", True)),
        gaps=parsed.get("gaps", []),
        targeted_queries=parsed.get("targeted_queries", []),
    )

    if verbose:
        if eval_result.is_sufficient:
            print(f"  [Coordinator] Synthesis is sufficient.")
        else:
            print(f"  [Coordinator] Gaps found: {eval_result.gaps}")
            print(f"  [Coordinator] Targeted queries: {eval_result.targeted_queries}")

    return eval_result


# ────────────────────────────────────────────────
# Subagent 実装
# ────────────────────────────────────────────────
# ★ Hub-and-Spoke パターン: サブエージェントは互いに通信しない。
#   全ての入出力は Coordinator (research_coordinator) を経由する。
#   これにより、エラーハンドリングと情報フローを一元管理できる。


async def web_search_agent(
    query: str,
    client: anthropic.Anthropic,
    simulate_timeout: bool = False,
    verbose: bool = True,
    learn_mode: bool = False,
) -> SourcedResult:
    """
    Web 検索 Subagent

    ★ Hub-and-Spoke: この関数は Coordinator からのみ呼ばれる。
    ★ 独立コンテキスト: 親の会話履歴は引き継がない。query を明示的に受け取る。
    """
    # ★ なぜ独立コンテキストか (--learn で表示)
    learn(
        "なぜ独立コンテキストか: サブエージェントはコーディネーターの会話履歴を自動継承しない。"
        "必要な情報は明示的に渡す",
        learn_mode,
    )

    if verbose:
        print(f"  [WebSearch] Searching: {query}")

    if simulate_timeout:
        await asyncio.sleep(0.1)
        raise TimeoutError("Web search timed out after 5s")

    # 擬似 web search (実際は web API を呼び出す)
    mock_result = MOCK_WEB_RESULTS.get("default")

    # ★ 構造化メタデータ分離: コンテンツと provenance を明示的に分ける
    metadata = SourceMetadata(
        source_url=mock_result["url"],
        agent_name="web_search_agent",
        confidence=mock_result["relevance"],
        retrieved_at=time.time(),
    )

    # ★ Subagent には必要な情報だけを渡す (クリーンなコンテキスト)
    response = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=512,
        messages=[
            {
                "role": "user",
                "content": (
                    f"以下の調査クエリに対して、web 検索で得られた情報を要約してください。\n\n"
                    f"調査クエリ: {query}\n\n"
                    f"検索結果の概要: {mock_result['summary']}\n\n"
                    f"要約を 3 点以内の箇条書きで返してください。"
                ),
            }
        ],
    )

    summary = response.content[0].text
    key_points = [line.strip() for line in summary.split("\n") if line.strip().startswith("-")]

    result = SourcedResult(
        content=summary,
        source=metadata.source_url,
        confidence=metadata.confidence,
        agent=metadata.agent_name,
        retrieved_at=metadata.retrieved_at,
        key_points=key_points,
    )

    if verbose:
        print(f"  [WebSearch] Done. Confidence: {result.confidence:.0%}")

    return result


async def doc_analysis_agent(
    query: str,
    client: anthropic.Anthropic,
    simulate_timeout: bool = False,
    verbose: bool = True,
    learn_mode: bool = False,
) -> SourcedResult:
    """
    ドキュメント分析 Subagent

    ★ Hub-and-Spoke: この関数は Coordinator からのみ呼ばれる。
    ★ 独立コンテキスト: 親の会話履歴は引き継がない。query を明示的に受け取る。
    """
    learn(
        "なぜ独立コンテキストか: サブエージェントはコーディネーターの会話履歴を自動継承しない。"
        "必要な情報は明示的に渡す",
        learn_mode,
    )

    if verbose:
        print(f"  [DocAnalysis] Analyzing docs for: {query}")

    if simulate_timeout:
        await asyncio.sleep(0.1)
        raise TimeoutError("Document analysis timed out after 10s")

    mock_doc = MOCK_DOCS.get("default")

    # ★ 構造化メタデータ分離
    metadata = SourceMetadata(
        source_url=f"doc://{mock_doc['doc_id']}",
        agent_name="doc_analysis_agent",
        confidence=mock_doc["relevance"],
        retrieved_at=time.time(),
    )

    # Claude でドキュメントを分析
    response = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=512,
        messages=[
            {
                "role": "user",
                "content": (
                    f"以下のドキュメントから、調査クエリに関連する重要な情報を抽出してください。\n\n"
                    f"調査クエリ: {query}\n\n"
                    f"ドキュメント (ID: {mock_doc['doc_id']}):\n{mock_doc['content']}\n\n"
                    f"重要ポイントを 3 点以内で返してください。"
                ),
            }
        ],
    )

    analysis = response.content[0].text
    key_points = [line.strip() for line in analysis.split("\n") if line.strip().startswith("-")]

    result = SourcedResult(
        content=analysis,
        source=metadata.source_url,
        confidence=metadata.confidence,
        agent=metadata.agent_name,
        retrieved_at=metadata.retrieved_at,
        key_points=key_points,
    )

    if verbose:
        print(f"  [DocAnalysis] Done. Confidence: {result.confidence:.0%}")

    return result


async def knowledge_base_agent(
    query: str,
    client: anthropic.Anthropic,
    simulate_timeout: bool = False,
    verbose: bool = True,
    learn_mode: bool = False,
) -> SourcedResult:
    """
    ナレッジベース検索 Subagent

    ★ Hub-and-Spoke: この関数は Coordinator からのみ呼ばれる。
    ★ 独立コンテキスト: 親の会話履歴は引き継がない。query を明示的に受け取る。
    """
    learn(
        "なぜ独立コンテキストか: サブエージェントはコーディネーターの会話履歴を自動継承しない。"
        "必要な情報は明示的に渡す",
        learn_mode,
    )

    if verbose:
        print(f"  [KnowledgeBase] Querying KB for: {query}")

    if simulate_timeout:
        await asyncio.sleep(0.1)
        raise TimeoutError("Knowledge base query timed out after 3s")

    mock_kb = MOCK_KB.get("default")

    # ★ 構造化メタデータ分離
    metadata = SourceMetadata(
        source_url=f"kb://{mock_kb['kb_id']}",
        agent_name="knowledge_base_agent",
        confidence=mock_kb["relevance"],
        retrieved_at=time.time(),
    )

    response = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=512,
        messages=[
            {
                "role": "user",
                "content": (
                    f"以下の既知情報から、調査クエリに関連する部分を抽出してください。\n\n"
                    f"調査クエリ: {query}\n\n"
                    f"ナレッジベース (ID: {mock_kb['kb_id']}):\n{mock_kb['content']}\n\n"
                    f"関連する情報を簡潔にまとめてください。"
                ),
            }
        ],
    )

    kb_result = response.content[0].text

    result = SourcedResult(
        content=kb_result,
        source=metadata.source_url,
        confidence=metadata.confidence,
        agent=metadata.agent_name,
        retrieved_at=metadata.retrieved_at,
        key_points=[],
    )

    if verbose:
        print(f"  [KnowledgeBase] Done. Confidence: {result.confidence:.0%}")

    return result


# ────────────────────────────────────────────────
# Synthesis
# ────────────────────────────────────────────────


def synthesize_results(
    query: str,
    sources: list[SourcedResult],
    client: anthropic.Anthropic,
    verbose: bool = True,
) -> str:
    """
    複数の Subagent 結果を統合する

    ★ 構造化データフォーマット分離: メタデータ (出典・信頼度) と
      コンテンツを別フィールドで Claude に渡す。Provenance を保持したまま合成する。

    ★ Hub-and-Spoke: この関数は Coordinator からのみ呼ばれる。
      サブエージェントは互いの結果を直接参照しない。
    """
    if verbose:
        print(f"\n  [Synthesis] Combining {len(sources)} sources...")

    # ★ なぜハブ・アンド・スポークか (--learn で表示) は Coordinator で出力。
    #   ここでは構造化分離を実演: content と metadata を別々に整形して渡す。

    # メタデータリスト (attribution)
    attributions = [
        {
            "source_url": s.source,
            "agent_name": s.agent,
            "confidence": s.confidence,
        }
        for s in sources
    ]

    # コンテンツリスト (本文)
    contents = [
        f"[情報源 {i+1}] {s.content}"
        for i, s in enumerate(sources)
    ]

    response = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=1024,
        messages=[
            {
                "role": "user",
                "content": (
                    f"以下の複数の情報源から得られた情報を統合して、調査クエリへの回答を作成してください。\n\n"
                    f"調査クエリ: {query}\n\n"
                    # ★ attribution (メタデータ) を content と分離して渡す
                    f"情報源メタデータ (JSON):\n{json.dumps(attributions, ensure_ascii=False, indent=2)}\n\n"
                    f"情報源コンテンツ:\n" + "\n\n".join(contents) + "\n\n"
                    f"回答には情報の信頼度や出典を適切に反映してください。"
                    f"不確実な情報は明示的に「不確実」として示してください。"
                ),
            }
        ],
    )

    return response.content[0].text


# ────────────────────────────────────────────────
# Parallel Dispatch Helper
# ────────────────────────────────────────────────


async def _dispatch_agents(
    query: str,
    client: anthropic.Anthropic,
    requirements: QueryRequirements,
    simulate_timeout_agents: list[str],
    simulate_all_fail: bool,
    verbose: bool,
    learn_mode: bool,
) -> tuple[list[SourcedResult], list[AgentError]]:
    """
    ★ Hub-and-Spoke: Coordinator が必要と判断した subagent だけを並列起動する。
    サブエージェント同士は互いを知らない — Coordinator がオーケストレーションする。
    """
    # ★ なぜ並列実行か (--learn で表示)
    learn(
        "なぜ並列実行か: 独立したタスクを並列化することで、合計実行時間を短縮できる",
        learn_mode,
    )

    # ★ なぜハブ・アンド・スポークか (--learn で表示)
    learn(
        "なぜハブ・アンド・スポークか: サブエージェント間の直接通信は可観測性を下げる。"
        "コーディネーター経由にすることで、エラーハンドリングと情報フローを一元管理できる",
        learn_mode,
    )

    # 動的選択: requirements に基づいてタスクリストを構築
    tasks = []
    agent_names = []

    if requirements.needs_web_search:
        tasks.append(
            web_search_agent(
                query=query,
                client=client,
                simulate_timeout=simulate_all_fail or "web_search" in simulate_timeout_agents,
                verbose=verbose,
                learn_mode=learn_mode,
            )
        )
        agent_names.append("web_search_agent")

    if requirements.needs_doc_analysis:
        tasks.append(
            doc_analysis_agent(
                query=query,
                client=client,
                simulate_timeout=simulate_all_fail or "doc_analysis" in simulate_timeout_agents,
                verbose=verbose,
                learn_mode=learn_mode,
            )
        )
        agent_names.append("doc_analysis_agent")

    if requirements.needs_knowledge_base:
        tasks.append(
            knowledge_base_agent(
                query=query,
                client=client,
                simulate_timeout=simulate_all_fail or "knowledge_base" in simulate_timeout_agents,
                verbose=verbose,
                learn_mode=learn_mode,
            )
        )
        agent_names.append("knowledge_base_agent")

    if not tasks:
        # フォールバック: 全エージェントを起動
        return await _dispatch_agents(
            query=query,
            client=client,
            requirements=QueryRequirements(
                needs_web_search=True,
                needs_doc_analysis=True,
                needs_knowledge_base=True,
                reasoning="fallback: no agents selected",
            ),
            simulate_timeout_agents=simulate_timeout_agents,
            simulate_all_fail=simulate_all_fail,
            verbose=verbose,
            learn_mode=False,
        )

    # asyncio.gather: return_exceptions=True で partial failure を graceful に処理
    raw_results = await asyncio.gather(*tasks, return_exceptions=True)

    # ────── Partial failure の処理 ──────
    successful: list[SourcedResult] = []
    failed: list[AgentError] = []

    for agent_name, result in zip(agent_names, raw_results):
        if isinstance(result, TimeoutError):
            failed.append(
                AgentError(
                    agent=agent_name,
                    error_type="timeout",
                    message=str(result),
                    is_retryable=True,
                    failed_at=time.time(),
                )
            )
            if verbose:
                print(f"  [TIMEOUT] {agent_name}: {result}")
        elif isinstance(result, Exception):
            failed.append(
                AgentError(
                    agent=agent_name,
                    error_type="api_error",
                    message=str(result),
                    is_retryable=False,
                    failed_at=time.time(),
                )
            )
            if verbose:
                print(f"  [ERROR] {agent_name}: {result}")
        else:
            successful.append(result)

    return successful, failed


# ────────────────────────────────────────────────
# Coordinator
# ────────────────────────────────────────────────


async def research_coordinator(
    query: str,
    client: anthropic.Anthropic,
    simulate_timeout_agents: list[str] | None = None,
    simulate_all_fail: bool = False,
    verbose: bool = True,
    learn_mode: bool = False,
    force_refinement: bool = False,
) -> ResearchResult:
    """
    Research Coordinator (Hub-and-Spoke)

    ★ Hub-and-Spoke: このコーディネーターがハブ。
      全ての subagent はスポーク。サブエージェント間の直接通信は存在しない。

    設計のポイント:
    1. 動的選択: Claude がクエリを分析してどの subagent を起動するか決定 (Task 1.2)
    2. 並列委譲: asyncio.gather で選択された subagent を並列実行
    3. Partial failure: 一部失敗しても処理継続
    4. Structured error: エラーは AgentError で表現
    5. Provenance: SourcedResult で情報源を追跡
    6. 反復改善: synthesis のギャップを評価して再委譲 (Task 1.2)
    """
    simulate_timeout_agents = simulate_timeout_agents or []

    if verbose:
        print(f"\n[Coordinator] Starting research: {query}")

    # ────── Step 1: 動的クエリ分析 ──────
    # ★ Coordinator が Claude を使って、どの subagent を起動するか決める
    requirements = analyze_query_requirements(
        query=query,
        client=client,
        verbose=verbose,
        learn_mode=learn_mode,
    )

    if verbose:
        print("[Coordinator] Dispatching to selected subagents in parallel...")

    # ────── Step 2: 並列委譲 ──────
    # ★ 各 subagent には query を明示的に渡す (暗黙の文脈継承に依存しない)
    successful, failed = await _dispatch_agents(
        query=query,
        client=client,
        requirements=requirements,
        simulate_timeout_agents=simulate_timeout_agents,
        simulate_all_fail=simulate_all_fail,
        verbose=verbose,
        learn_mode=learn_mode,
    )

    # 全エージェント失敗の場合は例外
    if not successful:
        raise RuntimeError(
            f"All agents failed: {[f.agent for f in failed]}. "
            f"Escalation required."
        )

    # ────── Step 3: 初回 Synthesis (provenance 付き) ──────
    synthesis = synthesize_results(
        query=query,
        sources=successful,
        client=client,
        verbose=verbose,
    )

    refinement_iterations = 0

    # ────── Step 4: 反復改善ループ ──────
    # Coordinator が synthesis を評価してギャップがあれば再委譲する
    MAX_REFINEMENT_ITERATIONS = 1  # 無限ループ防止

    coverage_eval = evaluate_synthesis_coverage(
        query=query,
        synthesis=synthesis,
        client=client,
        verbose=verbose,
        learn_mode=learn_mode,
    )

    if (not coverage_eval.is_sufficient or force_refinement) and MAX_REFINEMENT_ITERATIONS > 0:
        refinement_iterations += 1

        if verbose:
            print(f"\n[Coordinator] Refinement iteration {refinement_iterations}: re-delegating...")

        # ギャップを補完するための targeted_queries で再委譲
        # ★ Hub-and-Spoke: 再委譲も Coordinator 経由
        any_extra = False
        for targeted_query in coverage_eval.targeted_queries[:2]:  # 最大2クエリで補完
            if verbose:
                print(f"  [Coordinator] Targeted re-delegation: {targeted_query}")

            extra_successful, extra_failed = await _dispatch_agents(
                query=targeted_query,
                client=client,
                # 再委譲では web_search と knowledge_base を使う (ギャップ補完のため)
                requirements=QueryRequirements(
                    needs_web_search=True,
                    needs_doc_analysis=False,
                    needs_knowledge_base=True,
                    reasoning="refinement targeted query",
                ),
                simulate_timeout_agents=simulate_timeout_agents,
                simulate_all_fail=simulate_all_fail,
                verbose=verbose,
                learn_mode=False,  # 反復時は learn ノートを重複表示しない
            )

            successful.extend(extra_successful)
            failed.extend(extra_failed)
            if extra_successful:
                any_extra = True

        if any_extra:
            synthesis = synthesize_results(
                query=query,
                sources=successful,
                client=client,
                verbose=verbose,
            )

    # overall confidence: 成功したエージェントの最低信頼度
    overall_confidence = min(s.confidence for s in successful)

    return ResearchResult(
        query=query,
        synthesis=synthesis,
        sources_used=successful,
        sources_failed=failed,
        overall_confidence=overall_confidence,
        is_partial=len(failed) > 0,
        refinement_iterations=refinement_iterations,
    )


# ────────────────────────────────────────────────
# Demo: Dynamic Selection Comparison
# ────────────────────────────────────────────────


def show_dynamic_selection_demo(client: anthropic.Anthropic) -> None:
    """
    --show-dynamic-selection: 異なるクエリに対して動的選択がどう変わるかを示す
    """
    demo_queries = [
        "最新の Claude API の料金は？",           # web_search が必要
        "社内の API 利用ガイドラインを確認したい",  # doc_analysis が必要
        "よくある質問: API キーの管理方法",         # knowledge_base が必要
    ]

    print(f"\n{'='*60}")
    print("Dynamic Selection Demo: クエリごとに選択される subagent が変わる")
    print(f"{'='*60}")

    for q in demo_queries:
        print(f"\nクエリ: {q}")
        reqs = analyze_query_requirements(query=q, client=client, verbose=False)
        selected = [
            name
            for name, needed in [
                ("web_search", reqs.needs_web_search),
                ("doc_analysis", reqs.needs_doc_analysis),
                ("knowledge_base", reqs.needs_knowledge_base),
            ]
            if needed
        ]
        print(f"  → 選択された subagent: {selected}")
        print(f"  → 理由: {reqs.reasoning}")

    print()


# ────────────────────────────────────────────────
# Main
# ────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="Multi-Agent Research Pipeline Lab")
    parser.add_argument("--query", default="Claude Code の plan mode の使い方")
    parser.add_argument(
        "--simulate-timeout",
        nargs="*",
        choices=["web_search", "doc_analysis", "knowledge_base"],
        default=[],
        metavar="AGENT",
        help="指定した subagent をタイムアウトさせる",
    )
    parser.add_argument(
        "--simulate-all-fail",
        action="store_true",
        help="全 subagent を失敗させる",
    )
    parser.add_argument(
        "--learn",
        action="store_true",
        help="各設計決定の教育ノート (🎓 [LEARN]) を表示する",
    )
    parser.add_argument(
        "--show-dynamic-selection",
        action="store_true",
        help="異なるクエリに対して動的選択がどう変わるかのデモを表示する",
    )
    parser.add_argument(
        "--show-refinement",
        action="store_true",
        help="反復改善ループを強制的に実行してパターンをデモする",
    )
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

    # ────── Dynamic Selection Demo ──────
    if args.show_dynamic_selection:
        show_dynamic_selection_demo(client)
        return

    print(f"\n{'='*60}")
    print(f"調査クエリ: {args.query}")
    if args.simulate_timeout:
        print(f"タイムアウトシミュレーション: {args.simulate_timeout}")
    if args.simulate_all_fail:
        print("全エージェント失敗シミュレーション")
    if args.learn:
        print("学習モード: ON (🎓 [LEARN] ノートを表示)")
    if args.show_refinement:
        print("反復改善デモ: ON (強制的に refinement を実行)")
    print(f"{'='*60}")

    try:
        result = asyncio.run(
            research_coordinator(
                query=args.query,
                client=client,
                simulate_timeout_agents=args.simulate_timeout,
                simulate_all_fail=args.simulate_all_fail,
                verbose=not args.quiet,
                learn_mode=args.learn,
                force_refinement=args.show_refinement,
            )
        )

        print(f"\n{'='*60}")
        print("調査結果:")
        print(f"{'='*60}")
        print(result.synthesis)

        print(f"\n{'='*60}")
        print("メタデータ:")
        print(f"{'='*60}")
        print(f"全体信頼度: {result.overall_confidence:.0%}")
        print(f"部分的失敗: {result.is_partial}")
        print(f"反復改善回数: {result.refinement_iterations}")
        print(f"成功エージェント: {[s.agent for s in result.sources_used]}")

        if result.sources_failed:
            print(f"失敗エージェント: {[f.agent for f in result.sources_failed]}")
            print("失敗詳細:")
            for err in result.sources_failed:
                print(
                    f"  - {err.agent}: [{err.error_type}] {err.message} "
                    f"(retryable: {err.is_retryable})"
                )

        print(f"\n情報源:")
        for src in result.sources_used:
            print(f"  - {src.source} (信頼度: {src.confidence:.0%}, エージェント: {src.agent})")

    except RuntimeError as e:
        print(f"\n[ESCALATION REQUIRED]")
        print(f"全エージェントが失敗しました: {e}")
        print("人間によるレビューが必要です。")


if __name__ == "__main__":
    main()
