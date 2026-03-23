"""
Lab 04: Multi-Agent Research Pipeline

学習目標:
- Coordinator / Subagent パターン
- 並列委譲 (asyncio.gather) + partial failure の処理
- Subagent への明示的な文脈渡し (暗黙の継承に頼らない)
- Provenance 付き情報収集・合成
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
# Subagent 実装
# ────────────────────────────────────────────────


async def web_search_agent(
    query: str,
    client: anthropic.Anthropic,
    simulate_timeout: bool = False,
    verbose: bool = True,
) -> SourcedResult:
    """
    Web 検索 Subagent

    ★ 重要: 親の文脈は引き継がない。query を明示的に受け取る。
    """
    if verbose:
        print(f"  [WebSearch] Searching: {query}")

    if simulate_timeout:
        await asyncio.sleep(0.1)
        raise TimeoutError("Web search timed out after 5s")

    # 擬似 web search (実際は web API を呼び出す)
    mock_result = MOCK_WEB_RESULTS.get("default")

    # Claude で検索結果を要約
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
        source=mock_result["url"],
        confidence=mock_result["relevance"],
        agent="web_search_agent",
        retrieved_at=time.time(),
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
) -> SourcedResult:
    """
    ドキュメント分析 Subagent

    ★ 重要: 親の文脈は引き継がない。query を明示的に受け取る。
    """
    if verbose:
        print(f"  [DocAnalysis] Analyzing docs for: {query}")

    if simulate_timeout:
        await asyncio.sleep(0.1)
        raise TimeoutError("Document analysis timed out after 10s")

    mock_doc = MOCK_DOCS.get("default")

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
        source=f"doc://{mock_doc['doc_id']}",
        confidence=mock_doc["relevance"],
        agent="doc_analysis_agent",
        retrieved_at=time.time(),
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
) -> SourcedResult:
    """
    ナレッジベース検索 Subagent

    ★ 重要: 親の文脈は引き継がない。query を明示的に受け取る。
    """
    if verbose:
        print(f"  [KnowledgeBase] Querying KB for: {query}")

    if simulate_timeout:
        await asyncio.sleep(0.1)
        raise TimeoutError("Knowledge base query timed out after 3s")

    mock_kb = MOCK_KB.get("default")

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
        source=f"kb://{mock_kb['kb_id']}",
        confidence=mock_kb["relevance"],
        agent="knowledge_base_agent",
        retrieved_at=time.time(),
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

    ★ Provenance を保持したまま合成する
    """
    if verbose:
        print(f"\n  [Synthesis] Combining {len(sources)} sources...")

    # ★ 各情報に出典を付けてコンテキストを構築
    context_parts = []
    for source in sources:
        context_parts.append(
            f"【出典: {source.source} | 担当: {source.agent} | 信頼度: {source.confidence:.0%}】\n"
            f"{source.content}"
        )

    context = "\n\n---\n\n".join(context_parts)

    response = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=1024,
        messages=[
            {
                "role": "user",
                "content": (
                    f"以下の複数の情報源から得られた情報を統合して、調査クエリへの回答を作成してください。\n\n"
                    f"調査クエリ: {query}\n\n"
                    f"情報源:\n{context}\n\n"
                    f"回答には情報の信頼度や出典を適切に反映してください。"
                    f"不確実な情報は明示的に「不確実」として示してください。"
                ),
            }
        ],
    )

    return response.content[0].text


# ────────────────────────────────────────────────
# Coordinator
# ────────────────────────────────────────────────


async def research_coordinator(
    query: str,
    client: anthropic.Anthropic,
    simulate_timeout_agents: list[str] | None = None,
    simulate_all_fail: bool = False,
    verbose: bool = True,
) -> ResearchResult:
    """
    Research Coordinator

    設計のポイント:
    1. 並列委譲: asyncio.gather で全 subagent を並列実行
    2. Partial failure: 一部失敗しても処理継続
    3. Structured error: エラーは AgentError で表現
    4. Provenance: SourcedResult で情報源を追跡
    """
    simulate_timeout_agents = simulate_timeout_agents or []

    if verbose:
        print(f"\n[Coordinator] Starting research: {query}")
        print("[Coordinator] Dispatching to subagents in parallel...")

    # ────── 並列委譲 ──────
    # ★ 各 subagent には query を明示的に渡す (暗黙の文脈継承に依存しない)
    tasks = [
        web_search_agent(
            query=query,
            client=client,
            simulate_timeout=simulate_all_fail or "web_search" in simulate_timeout_agents,
            verbose=verbose,
        ),
        doc_analysis_agent(
            query=query,
            client=client,
            simulate_timeout=simulate_all_fail or "doc_analysis" in simulate_timeout_agents,
            verbose=verbose,
        ),
        knowledge_base_agent(
            query=query,
            client=client,
            simulate_timeout=simulate_all_fail or "knowledge_base" in simulate_timeout_agents,
            verbose=verbose,
        ),
    ]

    # asyncio.gather: return_exceptions=True で partial failure を graceful に処理
    raw_results = await asyncio.gather(*tasks, return_exceptions=True)

    # ────── Partial failure の処理 ──────
    successful: list[SourcedResult] = []
    failed: list[AgentError] = []
    agent_names = ["web_search_agent", "doc_analysis_agent", "knowledge_base_agent"]

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

    # 全エージェント失敗の場合は例外
    if not successful:
        raise RuntimeError(
            f"All agents failed: {[f.agent for f in failed]}. "
            f"Escalation required."
        )

    # ────── Synthesis (provenance を保持) ──────
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
    )


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
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

    print(f"\n{'='*60}")
    print(f"調査クエリ: {args.query}")
    if args.simulate_timeout:
        print(f"タイムアウトシミュレーション: {args.simulate_timeout}")
    if args.simulate_all_fail:
        print("全エージェント失敗シミュレーション")
    print(f"{'='*60}")

    try:
        result = asyncio.run(
            research_coordinator(
                query=args.query,
                client=client,
                simulate_timeout_agents=args.simulate_timeout,
                simulate_all_fail=args.simulate_all_fail,
                verbose=not args.quiet,
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
