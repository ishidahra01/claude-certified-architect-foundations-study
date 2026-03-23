# Project: Customer Support Agent

このプロジェクトは Claude Certified Architect 試験対策用のサンプルプロジェクトです。
Anthropic SDK for Python を使用した顧客サポートエージェントを実装します。

## 技術スタック

- Python 3.11+
- Anthropic SDK (`anthropic>=0.40.0`)
- pytest (テスト)

## コーディング規約

- 型ヒントを必ず付ける (`def func(x: str) -> dict:`)
- 関数は 40 行以内に収める (長い場合は分割する)
- docstring は関数の目的を 1 文で記述する
- f-string を優先する (`%` や `.format()` より)

## 禁止事項

- API キーのハードコード (環境変数を使用すること)
- 本番 DB への直接書き込み (テスト環境では mock を使用)
- bare `except:` の使用 (具体的な例外型を指定)

## ツール設計の原則

- 単一責任: 1 ツール 1 目的
- 明確な description: モデルが迷わない説明を書く
- isError パターン: エラーは例外ではなく戻り値で表現
- Gate パターン: 業務ルールは code で deterministic に実装

## テスト

```bash
pytest tests/ -v
pytest tests/ --cov=src --cov-report=term-missing
```

カバレッジ 80% 以上を維持してください。

## Claude Code での作業

- 小さな変更は direct execution で
- 大規模リファクタリングは plan mode で確認してから実行
- `.claude/skills/` のスキルを活用する
