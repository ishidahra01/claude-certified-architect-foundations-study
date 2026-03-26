# 準備演習 Notebook

`prep-exercises/` は、試験ガイドの準備演習を **Notebook ベースの段階実行コンテンツ** としてまとめたディレクトリです。

- Notebook は **上から順にセルを実行するだけ** で、設計判断を少しずつ確認できる教材です
- `labs/` は完成版の **reference implementation** として残してあります
- Claude Code 設定演習を除き、Notebook の実装方針は **Claude Agent SDK ベース** です
- 具体的な API・型・推奨パターンは、**最新の公式ドキュメント** と対応する `labs/` を参照して更新してください

## 対応表

| 準備演習 | Notebook | 完成版 Lab |
|---|---|---|
| 01 | `01-multi-tool-agent.ipynb` | `../labs/01-support-agent/` |
| 02 | `02-claude-code-configuration.ipynb` | `../labs/02-claude-code-team-workflow/` |
| 03 | `03-structured-extraction.ipynb` | `../labs/03-structured-extraction/` |
| 04 | `04-multi-agent-research.ipynb` | `../labs/04-multi-agent-research/` |

## 学習の進め方

1. まず Notebook を開き、冒頭の「目的 / 前提知識 / 完成イメージ」を読む
2. Code Cell を上から順に実行し、各ステップの確認ポイントを見る
3. 最後の「完成版 Lab 参照」セクションで、Notebook と Lab の差分を確認する
4. 必要に応じて、最新の Anthropic / Claude Agent SDK 公式ドキュメントに戻って API を更新する
