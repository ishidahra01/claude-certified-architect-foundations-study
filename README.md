# Claude Certified Architect – Foundations Study

このリポジトリは **Claude Certified Architect – Foundations** の学習コンテンツを提供します。  
試験ガイドに沿った 5 ドメインの設計判断を、ハンズオン Lab として実装したものです。  

現在の Lab 実装は **Claude Agent SDK 前提** を中心に整理しつつ、補助的に **Claude API / Claude Code / MCP** も学べる構成に更新しています。

## 試験ドメインと配点

| ドメイン | 配点 |
|---|---|
| Domain 1: Agentic Architecture & Orchestration | 27% |
| Domain 2: Tool Design & MCP Integration | 18% |
| Domain 3: Claude Code Configuration & Workflows | 20% |
| Domain 4: Prompt Engineering & Structured Output | 20% |
| Domain 5: Context Management & Reliability | 15% |

合格ライン: 720 点

## リポジトリ構成

```text
claude-certified-architect-foundations-study/
  README.md
  docs/
    exam-map.md                         # ドメイン別出題マップ
    domain-1-agentic-architecture.md
    domain-2-mcp-tool-design.md
    domain-3-claude-code.md
    domain-4-structured-output.md
    domain-5-context-reliability.md
  labs/
    01-support-agent/                   # Claude Agent SDK: Hooks / Tool calling / Escalation
    02-claude-code-team-workflow/       # CLAUDE.md / rules / skills / MCP / plan mode
    03-structured-extraction/           # Claude Agent SDK: Structured extraction + validation-retry
    04-multi-agent-research/            # Coordinator / subagent / provenance
    05-tool-design-mcp/                 # Tool design / MCP integration / built-in tools
  notes/
    decision-patterns.md                # 設計判断パターン集
    anti-patterns.md                    # アンチパターン集
    mock-questions-original.md          # 自作模擬問題
```

## ハンズオン Lab 一覧

### Lab 01: Customer Support Agent
**テーマ**: Agentic loop + Tool calling + Escalation gate  
**カバードメイン**: Domain 1, 2, 5  
`ClaudeSDKClient`、`@tool`、`HookMatcher`、`create_sdk_mcp_server()` を使って
疑似ツール (`get_customer`, `lookup_order`, `process_refund`, `escalate_to_human`) を実装し、
refund 閾値超えを hook でブロックします。

→ [labs/01-support-agent/](labs/01-support-agent/)

### Lab 02: Claude Code Team Workflow
**テーマ**: CLAUDE.md 階層 / rules / skills / MCP / plan mode  
**カバードメイン**: Domain 3  
Project-level の `CLAUDE.md`、`.claude/rules/` のパスベース規約、
`.claude/skills/` の `context: fork`、`.mcp.json` の env var 展開まで実装します。

→ [labs/02-claude-code-team-workflow/](labs/02-claude-code-team-workflow/)

### Lab 03: Structured Extraction Pipeline
**テーマ**: Schema-first extraction + validation-retry + human review routing  
**カバードメイン**: Domain 4, 5  
Claude Agent SDK の custom tool で schema-first extraction を行い、
nullable fields、semantic validation (`calculated_total` vs `stated_total`)、validation-retry ループを実装します。

→ [labs/03-structured-extraction/](labs/03-structured-extraction/)

### Lab 04: Multi-Agent Research Pipeline
**テーマ**: Coordinator / subagent / provenance / partial failure  
**カバードメイン**: Domain 1, 5  
web-search・doc-analysis・synthesis の各 subagent を coordinator が並列委譲し、
timeout 時は structured error、provenance を維持した合成を実装します。

→ [labs/04-multi-agent-research/](labs/04-multi-agent-research/)

### Lab 05: Tool Design & MCP Integration
**テーマ**: Tool description / structured errors / tool distribution / MCP  
**カバードメイン**: Domain 2  
Agent SDK での `@tool` / `create_sdk_mcp_server()` と合わせて、
description 設計・構造化エラー・`.mcp.json`・組み込みツールの使い分けを学びます。

→ [labs/05-tool-design-mcp/](labs/05-tool-design-mcp/)

## 参考リンク

- [Claude Agent SDK Overview](https://platform.claude.com/docs/en/agent-sdk/overview)
- [Claude Agent SDK Python Reference](https://platform.claude.com/docs/en/agent-sdk/python)
- [Anthropic Docs – Claude Code](https://docs.anthropic.com/en/docs/claude-code)
- [Model Context Protocol Specification](https://modelcontextprotocol.io/specification/2025-11-25/server/tools)
- [Claude Partner Network](https://www.anthropic.com/news/claude-partner-network)
