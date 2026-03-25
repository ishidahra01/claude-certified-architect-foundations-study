"""
Lab 04: Multi-Agent Research Pipeline (Claude Agent SDK 版)

学習目標:
- Coordinator / Subagent パターン (Hub-and-Spoke)
- Claude Agent SDK の query() で subagent を実行する
- 動的クエリ分析: 必要な subagent を選択する
- 並列委譲 (asyncio.gather) + partial failure の処理
- 明示的な文脈渡しと provenance の保持
- synthesis のギャップ評価と targeted re-delegation
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any

import anyio
from claude_agent_sdk import AgentDefinition, ClaudeAgentOptions, query
from claude_agent_sdk.types import AssistantMessage, ResultMessage, TextBlock
from dotenv import load_dotenv

load_dotenv()


@dataclass
class SourcedResult:
    """Provenance (情報源) 付きの調査結果"""

    content: str
    source: str
    confidence: float
    agent: str
    retrieved_at: float
    key_points: list[str] = field(default_factory=list)


@dataclass
class SourceMetadata:
    """コンテンツとメタデータを分離した構造化形式。"""

    source_url: str
    agent_name: str
    confidence: float
    retrieved_at: float


@dataclass
class AgentError:
    """Subagent のエラーを structured に表現する"""

    agent: str
    error_type: str  # "timeout" | "agent_execution_error"
    message: str
    is_retryable: bool
    failed_at: float


@dataclass
class ResearchResult:
    """調査全体の結果"""

    query: str
    synthesis: str
    sources_used: list[SourcedResult]
    sources_failed: list[AgentError]
    overall_confidence: float
    is_partial: bool
    refinement_iterations: int = 0


@dataclass
class QueryRequirements:
    needs_web_search: bool
    needs_doc_analysis: bool
    needs_knowledge_base: bool
    reasoning: str


@dataclass
class SynthesisCoverageEval:
    is_sufficient: bool
    gaps: list[str]
    targeted_queries: list[str]


MOCK_WEB_RESULTS = {
    "default": {
        "summary": "Web 検索により関連する最新情報を取得しました。",
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

QUERY_REQUIREMENTS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "needs_web_search": {"type": "boolean"},
        "needs_doc_analysis": {"type": "boolean"},
        "needs_knowledge_base": {"type": "boolean"},
        "reasoning": {"type": "string"},
    },
    "required": [
        "needs_web_search",
        "needs_doc_analysis",
        "needs_knowledge_base",
        "reasoning",
    ],
}

SUBAGENT_RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "content": {"type": "string"},
        "key_points": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "number"},
    },
    "required": ["content", "key_points", "confidence"],
}

SYNTHESIS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "synthesis": {"type": "string"},
    },
    "required": ["synthesis"],
}

COVERAGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "is_sufficient": {"type": "boolean"},
        "gaps": {"type": "array", "items": {"type": "string"}},
        "targeted_queries": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["is_sufficient", "gaps", "targeted_queries"],
}


def learn(msg: str, enabled: bool) -> None:
    if enabled:
        print(f"\n🎓 [LEARN] {msg}\n")


def build_requirements_prompt(query_text: str) -> str:
    return (
        "あなたは調査パイプラインのコーディネーターです。"
        "以下のクエリに対して、どの情報源が必要かを判断してください。\n\n"
        f"クエリ: {query_text}\n\n"
        "利用可能な情報源:\n"
        "- web_search: 最新情報・ニュース・外部記事の検索\n"
        "- doc_analysis: 社内ドキュメント・仕様書の分析\n"
        "- knowledge_base: FAQ・ナレッジベースの検索\n"
        "冗長な説明は不要です。"
    )


def build_subagent_prompt(query_text: str, evidence_label: str, evidence_body: str) -> str:
    return (
        f"調査クエリ: {query_text}\n\n"
        f"利用可能な入力 ({evidence_label}):\n{evidence_body}\n\n"
        "与えられた情報だけを使って調査結果をまとめてください。"
        "推測や新しい外部情報の追加は禁止です。"
    )


def build_synthesis_prompt(query_text: str, sources: list[SourcedResult]) -> str:
    attributions = [
        {
            "source_url": s.source,
            "agent_name": s.agent,
            "confidence": s.confidence,
            "retrieved_at": s.retrieved_at,
        }
        for s in sources
    ]
    contents = [f"[情報源 {i + 1}] {s.content}" for i, s in enumerate(sources)]
    return (
        f"調査クエリ: {query_text}\n\n"
        f"情報源メタデータ (JSON):\n{json.dumps(attributions, ensure_ascii=False, indent=2)}\n\n"
        "情報源コンテンツ:\n"
        + "\n\n".join(contents)
        + "\n\n出典と不確実性を反映しながら統合回答を作成してください。"
    )


def build_coverage_prompt(query_text: str, synthesis: str) -> str:
    return (
        f"調査クエリ: {query_text}\n\n"
        f"現在の synthesis:\n{synthesis}\n\n"
        "カバレッジの不足があるか評価し、必要なら targeted query を提案してください。"
    )


async def run_query_with_schema(
    *,
    prompt: str,
    schema: dict[str, Any],
    verbose: bool = False,
    label: str | None = None,
    agent_name: str | None = None,
    agent_description: str | None = None,
    agent_prompt: str | None = None,
    agent_model: str = "haiku",
) -> dict[str, Any]:
    agents = None
    final_prompt = prompt
    context = label or agent_name or "unknown operation"
    should_log_text = verbose and label is not None
    if agent_name and agent_description and agent_prompt:
        agents = {
            agent_name: AgentDefinition(
                description=agent_description,
                prompt=agent_prompt,
                model=agent_model,
            )
        }
        final_prompt = f"Use the {agent_name} agent.\n\n{prompt}"

    options = ClaudeAgentOptions(
        agents=agents,
        output_format={"type": "json_schema", "schema": schema},
        max_turns=4,
    )

    result_message: ResultMessage | None = None
    async for message in query(prompt=final_prompt, options=options):
        if isinstance(message, AssistantMessage) and should_log_text:
            for block in message.content:
                if isinstance(block, TextBlock):
                    print(f"  [{label}] {block.text}")
        elif isinstance(message, ResultMessage):
            result_message = message

    if result_message is None:
        raise RuntimeError(f"No result returned from Claude Agent SDK query ({context})")
    if result_message.is_error:
        raise RuntimeError(result_message.result or f"Claude Agent SDK query failed ({context})")
    if result_message.structured_output is None:
        raise RuntimeError("Structured output was not returned")
    return result_message.structured_output


async def analyze_query_requirements(
    query_text: str,
    verbose: bool = True,
    learn_mode: bool = False,
) -> QueryRequirements:
    learn(
        "なぜ動的選択か: 毎回全パイプラインを通すと、単純な質問にも不要な subagent を起動してしまう。",
        learn_mode,
    )
    if verbose:
        print("  [Coordinator] Analyzing query requirements with query()...")

    parsed = await run_query_with_schema(
        prompt=build_requirements_prompt(query_text),
        schema=QUERY_REQUIREMENTS_SCHEMA,
        verbose=False,
        label="Coordinator",
    )
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


async def evaluate_synthesis_coverage(
    query_text: str,
    synthesis: str,
    verbose: bool = True,
    learn_mode: bool = False,
) -> SynthesisCoverageEval:
    learn(
        "なぜ反復改善か: synthesis が一度で十分とは限らないため、Coordinator が不足を評価して再委譲する。",
        learn_mode,
    )
    if verbose:
        print("  [Coordinator] Evaluating synthesis coverage...")

    parsed = await run_query_with_schema(
        prompt=build_coverage_prompt(query_text, synthesis),
        schema=COVERAGE_SCHEMA,
        verbose=False,
        label="Coverage",
    )
    return SynthesisCoverageEval(
        is_sufficient=bool(parsed.get("is_sufficient", True)),
        gaps=list(parsed.get("gaps", [])),
        targeted_queries=list(parsed.get("targeted_queries", [])),
    )


async def run_subagent(
    *,
    agent_name: str,
    agent_description: str,
    agent_prompt: str,
    query_text: str,
    evidence_label: str,
    evidence_body: str,
    source_url: str,
    default_confidence: float,
    simulate_timeout: bool,
    timeout_message: str,
    verbose: bool,
    learn_mode: bool,
) -> SourcedResult:
    learn(
        "なぜ明示的な文脈渡しか: subagent は Coordinator の会話履歴を自動継承しないため、必要情報を明示的に渡す。",
        learn_mode,
    )
    if verbose:
        print(f"  [{agent_name}] Running subagent via query()...")
    if simulate_timeout:
        await asyncio.sleep(0.1)
        raise TimeoutError(timeout_message)

    parsed = await run_query_with_schema(
        prompt=build_subagent_prompt(query_text, evidence_label, evidence_body),
        schema=SUBAGENT_RESULT_SCHEMA,
        verbose=False,
        label=agent_name,
        agent_name=agent_name,
        agent_description=agent_description,
        agent_prompt=agent_prompt,
    )
    retrieved_at = time.time()
    metadata = SourceMetadata(
        source_url=source_url,
        agent_name=agent_name,
        confidence=float(parsed.get("confidence", default_confidence)),
        retrieved_at=retrieved_at,
    )
    return SourcedResult(
        content=parsed.get("content", ""),
        source=metadata.source_url,
        confidence=metadata.confidence,
        agent=metadata.agent_name,
        retrieved_at=metadata.retrieved_at,
        key_points=list(parsed.get("key_points", [])),
    )


async def web_search_agent(
    query_text: str,
    simulate_timeout: bool = False,
    verbose: bool = True,
    learn_mode: bool = False,
) -> SourcedResult:
    mock_result = MOCK_WEB_RESULTS["default"]
    return await run_subagent(
        agent_name="web-researcher",
        agent_description="Summarizes web findings with provenance and caveats.",
        agent_prompt="You are a web research specialist. Use only the provided evidence. Include caveats when confidence is low.",
        query_text=query_text,
        evidence_label="web result",
        evidence_body=f"URL: {mock_result['url']}\nSummary: {mock_result['summary']}",
        source_url=mock_result["url"],
        default_confidence=mock_result["relevance"],
        simulate_timeout=simulate_timeout,
        timeout_message="Web search timed out after 5s",
        verbose=verbose,
        learn_mode=learn_mode,
    )


async def doc_analysis_agent(
    query_text: str,
    simulate_timeout: bool = False,
    verbose: bool = True,
    learn_mode: bool = False,
) -> SourcedResult:
    mock_doc = MOCK_DOCS["default"]
    return await run_subagent(
        agent_name="doc-analyst",
        agent_description="Analyzes internal documents and extracts only relevant findings.",
        agent_prompt="You are a document analysis specialist. Use only the supplied document text and cite the document id implicitly in the summary.",
        query_text=query_text,
        evidence_label="document",
        evidence_body=f"Document ID: {mock_doc['doc_id']}\nContent: {mock_doc['content']}",
        source_url=f"doc://{mock_doc['doc_id']}",
        default_confidence=mock_doc["relevance"],
        simulate_timeout=simulate_timeout,
        timeout_message="Document analysis timed out after 10s",
        verbose=verbose,
        learn_mode=learn_mode,
    )


async def knowledge_base_agent(
    query_text: str,
    simulate_timeout: bool = False,
    verbose: bool = True,
    learn_mode: bool = False,
) -> SourcedResult:
    mock_kb = MOCK_KB["default"]
    return await run_subagent(
        agent_name="knowledge-base-specialist",
        agent_description="Finds reusable internal knowledge for the given query.",
        agent_prompt="You are a knowledge base specialist. Use only the provided KB entry and summarize the reusable guidance.",
        query_text=query_text,
        evidence_label="knowledge base entry",
        evidence_body=f"KB ID: {mock_kb['kb_id']}\nContent: {mock_kb['content']}",
        source_url=f"kb://{mock_kb['kb_id']}",
        default_confidence=mock_kb["relevance"],
        simulate_timeout=simulate_timeout,
        timeout_message="Knowledge base query timed out after 3s",
        verbose=verbose,
        learn_mode=learn_mode,
    )


async def synthesize_results(
    query_text: str,
    sources: list[SourcedResult],
    verbose: bool = True,
) -> str:
    if verbose:
        print(f"\n  [Synthesis] Combining {len(sources)} sources with query()...")
    parsed = await run_query_with_schema(
        prompt=build_synthesis_prompt(query_text, sources),
        schema=SYNTHESIS_SCHEMA,
        verbose=False,
        label="Synthesis",
        agent_name="synthesis-agent",
        agent_description="Combines sourced findings into one answer while preserving uncertainty.",
        agent_prompt="You are a synthesis specialist. Merge the provided sourced findings, keep provenance in mind, and avoid unsupported claims.",
        agent_model="sonnet",
    )
    return parsed.get("synthesis", "")


async def _dispatch_agents(
    query_text: str,
    requirements: QueryRequirements,
    simulate_timeout_agents: list[str],
    simulate_all_fail: bool,
    verbose: bool,
    learn_mode: bool,
) -> tuple[list[SourcedResult], list[AgentError]]:
    learn(
        "なぜ並列実行か: 独立した subagent を並列化することで待ち時間を短縮できる。",
        learn_mode,
    )
    tasks: list[asyncio.Future[Any] | Any] = []
    agent_names: list[str] = []

    if requirements.needs_web_search:
        tasks.append(
            web_search_agent(
                query_text=query_text,
                simulate_timeout=simulate_all_fail or "web_search" in simulate_timeout_agents,
                verbose=verbose,
                learn_mode=learn_mode,
            )
        )
        agent_names.append("web_search_agent")

    if requirements.needs_doc_analysis:
        tasks.append(
            doc_analysis_agent(
                query_text=query_text,
                simulate_timeout=simulate_all_fail or "doc_analysis" in simulate_timeout_agents,
                verbose=verbose,
                learn_mode=learn_mode,
            )
        )
        agent_names.append("doc_analysis_agent")

    if requirements.needs_knowledge_base:
        tasks.append(
            knowledge_base_agent(
                query_text=query_text,
                simulate_timeout=simulate_all_fail or "knowledge_base" in simulate_timeout_agents,
                verbose=verbose,
                learn_mode=learn_mode,
            )
        )
        agent_names.append("knowledge_base_agent")

    if not tasks:
        fallback = QueryRequirements(
            needs_web_search=True,
            needs_doc_analysis=True,
            needs_knowledge_base=True,
            reasoning="fallback: no agents selected",
        )
        return await _dispatch_agents(
            query_text=query_text,
            requirements=fallback,
            simulate_timeout_agents=simulate_timeout_agents,
            simulate_all_fail=simulate_all_fail,
            verbose=verbose,
            learn_mode=False,
        )

    raw_results = await asyncio.gather(*tasks, return_exceptions=True)
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
                    error_type="agent_execution_error",
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


async def research_coordinator(
    query_text: str,
    simulate_timeout_agents: list[str] | None = None,
    simulate_all_fail: bool = False,
    verbose: bool = True,
    learn_mode: bool = False,
    force_refinement: bool = False,
) -> ResearchResult:
    simulate_timeout_agents = simulate_timeout_agents or []
    if verbose:
        print(f"\n[Coordinator] Starting research: {query_text}")

    requirements = await analyze_query_requirements(
        query_text=query_text,
        verbose=verbose,
        learn_mode=learn_mode,
    )
    if verbose:
        print("[Coordinator] Dispatching to selected subagents in parallel...")

    successful, failed = await _dispatch_agents(
        query_text=query_text,
        requirements=requirements,
        simulate_timeout_agents=simulate_timeout_agents,
        simulate_all_fail=simulate_all_fail,
        verbose=verbose,
        learn_mode=learn_mode,
    )
    if not successful:
        raise RuntimeError(
            f"All agents failed: {[f.agent for f in failed]}. Escalation required."
        )

    synthesis = await synthesize_results(
        query_text=query_text,
        sources=successful,
        verbose=verbose,
    )
    refinement_iterations = 0
    coverage_eval = await evaluate_synthesis_coverage(
        query_text=query_text,
        synthesis=synthesis,
        verbose=verbose,
        learn_mode=learn_mode,
    )

    if (not coverage_eval.is_sufficient or force_refinement) and coverage_eval.targeted_queries:
        refinement_iterations += 1
        if verbose:
            print(f"\n[Coordinator] Refinement iteration {refinement_iterations}: re-delegating...")
        any_extra = False
        for targeted_query in coverage_eval.targeted_queries[:2]:
            extra_successful, extra_failed = await _dispatch_agents(
                query_text=targeted_query,
                requirements=QueryRequirements(
                    needs_web_search=True,
                    needs_doc_analysis=False,
                    needs_knowledge_base=True,
                    reasoning="refinement targeted query",
                ),
                simulate_timeout_agents=simulate_timeout_agents,
                simulate_all_fail=simulate_all_fail,
                verbose=verbose,
                learn_mode=False,
            )
            successful.extend(extra_successful)
            failed.extend(extra_failed)
            if extra_successful:
                any_extra = True
        if any_extra:
            synthesis = await synthesize_results(
                query_text=query_text,
                sources=successful,
                verbose=verbose,
            )

    overall_confidence = min(source.confidence for source in successful)
    return ResearchResult(
        query=query_text,
        synthesis=synthesis,
        sources_used=successful,
        sources_failed=failed,
        overall_confidence=overall_confidence,
        is_partial=len(failed) > 0,
        refinement_iterations=refinement_iterations,
    )


async def show_dynamic_selection_demo() -> None:
    demo_queries = [
        "最新の Claude API の料金は？",
        "社内の API 利用ガイドラインを確認したい",
        "よくある質問: API キーの管理方法",
    ]
    print(f"\n{'=' * 60}")
    print("Dynamic Selection Demo: クエリごとに選択される subagent が変わる")
    print(f"{'=' * 60}")
    for query_text in demo_queries:
        print(f"\nクエリ: {query_text}")
        reqs = await analyze_query_requirements(query_text=query_text, verbose=False)
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


async def main() -> None:
    parser = argparse.ArgumentParser(description="Multi-Agent Research Pipeline Lab (Claude Agent SDK)")
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
        help="各設計決定の教育ノートを表示する",
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

    if args.show_dynamic_selection:
        await show_dynamic_selection_demo()
        return

    print(f"\n{'=' * 60}")
    print(f"調査クエリ: {args.query}")
    if args.simulate_timeout:
        print(f"タイムアウトシミュレーション: {args.simulate_timeout}")
    if args.simulate_all_fail:
        print("全エージェント失敗シミュレーション")
    if args.learn:
        print("学習モード: ON")
    if args.show_refinement:
        print("反復改善デモ: ON")
    print(f"{'=' * 60}")

    try:
        result = await research_coordinator(
            query_text=args.query,
            simulate_timeout_agents=args.simulate_timeout,
            simulate_all_fail=args.simulate_all_fail,
            verbose=not args.quiet,
            learn_mode=args.learn,
            force_refinement=args.show_refinement,
        )
        print(f"\n{'=' * 60}")
        print("調査結果:")
        print(f"{'=' * 60}")
        print(result.synthesis)

        print(f"\n{'=' * 60}")
        print("メタデータ:")
        print(f"{'=' * 60}")
        print(f"全体信頼度: {result.overall_confidence:.0%}")
        print(f"部分的失敗: {result.is_partial}")
        print(f"反復改善回数: {result.refinement_iterations}")
        print(f"成功エージェント: {[s.agent for s in result.sources_used]}")
        if result.sources_failed:
            print(f"失敗エージェント: {[f.agent for f in result.sources_failed]}")
            for err in result.sources_failed:
                print(
                    f"  - {err.agent}: [{err.error_type}] {err.message} "
                    f"(retryable: {err.is_retryable})"
                )
        print("\n情報源:")
        for source in result.sources_used:
            print(
                f"  - {source.source} (信頼度: {source.confidence:.0%}, エージェント: {source.agent})"
            )
    except RuntimeError as error:
        print("\n[ESCALATION REQUIRED]")
        print(f"全エージェントが失敗しました: {error}")
        print("人間によるレビューが必要です。")


if __name__ == "__main__":
    anyio.run(main)
