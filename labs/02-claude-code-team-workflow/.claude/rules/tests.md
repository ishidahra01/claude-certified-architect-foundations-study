# テスト記述ルール

## 対象ファイル

`tests/**/*`

## 基本原則

- テスト関数名は `test_` で始める
- Arrange-Act-Assert (AAA) パターンに従う
- 各テストは独立して実行できるようにする (テスト間の依存禁止)

## 命名規則

```python
# Good: テスト対象と期待する動作が分かる
def test_process_refund_returns_error_when_amount_exceeds_threshold():
    ...

# Bad: 何をテストしているか不明
def test_refund():
    ...
```

## モック

- 外部 API 呼び出しは必ずモックする
- `unittest.mock.patch` を使用する
- 実際の Anthropic API は呼び出さない

## テストデータ

- テストデータは `tests/fixtures/` に置く
- ハードコードされた値にはコメントで理由を記述する

## カバレッジ

- 最低 80% を維持する
- ハッピーパス + エラーパスの両方をテストする
- Gate / Hook のテストを必ず書く (境界値テスト)
