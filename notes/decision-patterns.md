# 設計判断パターン集

試験で問われる「最も proportionate で root cause に効く一手」を選ぶための判断パターンです。

## パターン 1: Prompt vs Hook / Gate

### 判断軸

```
「この制約が破られたとき、ビジネス上の損害が発生するか？」

YES → Hook / Gate (code で担保)
NO  → Prompt で十分
```

### 具体例

| 制約 | 適切な実装 | 理由 |
|---|---|---|
| 返金は $500 以下のみ自動処理 | **Gate (code)** | 超過時に金銭的損害 |
| 丁寧な言葉遣いで回答 | **Prompt** | 違反しても致命的でない |
| 本人確認後でないと個人情報変更不可 | **Gate (code)** | 認証バイパスのリスク |
| 回答は 3 段落以内で | **Prompt** | フォーマット指示 |
| 在庫ゼロ商品は注文不可 | **Tool 内ロジック** | データ整合性の問題 |

---

## パターン 2: ツール粒度の設計

### 判断軸

```
「1 つのツールで複数の目的を達成しようとしていないか？」

YES → 分割する
NO  → 現状維持
```

### 分割の基準

```python
# NG: 1 つのツールが複数の副作用を持つ
def handle_order(action: str, order_id: str, ...):
    if action == "lookup": ...
    if action == "cancel": ...
    if action == "refund": ...

# OK: 単一責任
def lookup_order(order_id: str): ...   # 読み取り専用
def cancel_order(order_id: str): ...   # キャンセル専用
def process_refund(order_id: str, amount: float): ...  # 返金専用
```

---

## パターン 3: Orchestrator / Subagent の文脈設計

### 判断軸

```
「subagent はこの情報がなくても正しく動けるか？」

NO → orchestrator が明示的に渡す
```

### 渡すべき情報の優先度

1. **必ず渡す**: タスクの目的・制約・入力データ
2. **必要に応じて渡す**: 前の subagent の結果・エラー情報
3. **渡さなくてよい**: orchestrator の内部状態・他タスクの詳細

---

## パターン 4: Structured Output の方式選択

### 判断軸

```
「JSON の構造を保証する必要があるか？」

YES → Agent SDK の custom tool / output_format、または Claude API の tool_use + tool_choice
「ツール呼び出しが必要なのか、出力フォーマットだけが必要なのか？」
構造化データを tool call として扱いたい → custom tool
出力フォーマットだけ保証したい → output_format (json_schema)
Claude API を直接使う → tool_choice: tool / any / auto を選ぶ
```

---

## パターン 5: Retry 戦略

### エラーの種類と対応

| エラーの種類 | retryable | 対応 |
|---|---|---|
| not found | false | モデルに別アクションを選ばせる |
| network timeout | true | 指数バックオフでリトライ |
| validation error | true (有限) | エラーコンテキスト付きでリトライ |
| threshold exceeded | false | エスカレーション |
| permission denied | false | エスカレーション |

---

## パターン 6: Context Window 管理

### 情報の配置戦略

```
[先頭] 最重要ルール・制約 (must not forget)
[中間] 参照コンテキスト・背景情報 (nice to have)
[末尾] 現在の case facts・ユーザー質問 (most recent)
```

### ツール出力の管理

```python
# 返すべき情報の優先度
優先度 1: 現在のタスクに直接関連する情報
優先度 2: エラー・警告情報
優先度 3: 補足情報 (件数、日時等)
削除対象: 内部 ID、生データ、冗長フィールド
```

---

## パターン 7: Escalation の判断

### 自動処理できる条件

- [ ] 金額が閾値以下
- [ ] 信頼度スコアが基準以上
- [ ] 既知のパターンに一致
- [ ] 連続失敗なし
- [ ] 機密操作でない

### Human Review にする条件 (いずれか 1 つでも該当したら)

- [ ] 金額が閾値超え
- [ ] 信頼度が基準未満
- [ ] 複数のバリデーションエラー
- [ ] 最大リトライ回数到達
- [ ] 機密操作 (個人情報変更等)

---

## パターン 8: Provenance の設計

### 追跡すべき情報

```python
class SourcedResult:
    content: str          # 取得した情報
    source: str           # 情報源 (URL, document ID)
    confidence: float     # 信頼度
    retrieved_at: str     # 取得日時
    agent: str            # 担当 subagent
```

### synthesis 時の原則

- 情報源が不明なデータは使わない
- 信頼度が低いデータは「不確実」として明示
- conflicting 情報は両方を提示して判断を求める
