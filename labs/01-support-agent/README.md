# Lab 01: Customer Support Agent

**テーマ**: Agentic loop + Tool calling + Escalation gate + Hooks + Anti-patterns  
**カバードメイン**: Domain 1 (Agentic Architecture), Domain 2 (Tool Design), Domain 5 (Context & Reliability)

## 学習目標

1. `stop_reason` による agentic loop の制御を理解する (Task 1.1)
2. 単一責任のツール設計と `isError` パターンを実装する (Task 1.1)
3. **プログラム的前提条件ゲート** による順序強制を実装する (Task 1.4)
4. **PostToolUse フック** によるデータ正規化を理解する (Task 1.5)
5. **構造化ハンドオフサマリー** を実装する (Task 1.4)
6. Prompt vs Code での制約の使い分けを体験する (Task 1.5)
7. **アンチパターン** を実際に確認し、なぜ失敗するかを理解する (Task 1.1)

## 実装するもの

```
Customer Support Agent
  ├── get_customer(customer_id)         顧客情報取得 (read-only)
  │     └── 成功時: session_state["customer_verified"] = True にセット
  ├── lookup_order(order_id)            注文情報取得 (read-only)
  ├── process_refund(order_id, amount)  返金処理 + 閾値 Gate
  │     └── 前提条件ゲート: customer_verified でないとブロック (Task 1.4)
  └── escalate_to_human(reason, ctx)   人間へのエスカレーション
        └── 構造化ハンドオフサマリーを出力 (Task 1.4)

PostToolUse フック (全ツール共通):
  ├── Unix タイムスタンプ → ISO 8601 変換
  └── 数値ステータスコード → 人間可読な説明に変換 (Task 1.5)
```

## 実行方法

```bash
cd labs/01-support-agent
pip install -r requirements.txt
export ANTHROPIC_API_KEY="your-api-key"

# ────── 基本シナリオ ──────

# 通常の返金シナリオ (閾値以内)
python main.py --mode normal

# 閾値超え → escalation シナリオ (構造化ハンドオフを確認)
python main.py --mode escalation

# 存在しない顧客・注文のシナリオ
python main.py --mode auth_fail

# 返金不可の注文 (処理中ステータス)
python main.py --mode not_refundable

# ────── 学習モード ──────

# --learn フラグ: 各ステップの WHY を解説しながら実行
python main.py --mode normal --learn

# ────── アンチパターン デモ ──────

# API キー不要! アンチパターンをシミュレーションで確認
python main.py --mode antipatterns
```

## 設計のポイント

### 1. プログラム的前提条件ゲート (Task 1.4)

```python
# NG: prompt だけで順序を保証しようとする
# "必ず get_customer を先に呼んでから process_refund してください"
# ↑ モデルが従わないケースがある (確率的なコンプライアンス)

# OK: code で順序を deterministic に強制する
session_state = {"customer_verified": False, "verified_customer_id": None}

def process_refund(order_id: str, amount: float, reason: str) -> dict:
    # ★ PREREQUISITE GATE: 顧客確認前の返金をブロック
    if not session_state["customer_verified"]:
        return {
            "isError": True,
            "error": "PREREQUISITE_NOT_MET",
            "message": "get_customer must be called successfully before process_refund",
        }
    # ...
```

**なぜ重要か**: 金融操作の前に身元確認が必要なシステムでは、prompt での指示だけでは非ゼロの失敗率がある。code で前提条件を強制することでゼロ失敗率を達成できる。

### 2. PostToolUse フック によるデータ正規化 (Task 1.5)

```python
def post_tool_use_hook(tool_name: str, result: dict, learn: bool = False) -> dict:
    """
    ★ PostToolUse Hook: ツール結果をモデルが処理する前に正規化する
    異なるシステムから返ってくる形式の不一致を統一する
    """
    def normalize(obj):
        if isinstance(obj, dict):
            normalized = {}
            for k, v in obj.items():
                if k.endswith("_at") and isinstance(v, int):
                    # Unix timestamp → ISO 8601
                    normalized[k] = datetime.datetime.fromtimestamp(v).isoformat()
                elif k == "status_code" and isinstance(v, int):
                    # 数値ステータスコード → 人間可読な説明
                    normalized[k] = v
                    normalized["status_code_description"] = STATUS_CODE_DESCRIPTIONS.get(v, "Unknown")
                else:
                    normalized[k] = normalize(v)
            return normalized
        return obj
    return normalize(result)
```

**なぜ重要か**: 複数の MCP ツールやシステムから返ってくるデータ形式は異なる (Unix timestamp vs ISO 8601、数値コード vs 文字列など)。フックで統一することで、モデルは一貫した形式で情報を処理できる。

### 3. stop_reason によるループ制御 (Task 1.1)

```python
while iteration < max_iterations:  # max_iterations は安全ネット (主要停止機構ではない)
    response = client.messages.create(...)
    
    if response.stop_reason == "end_turn":
        # ★ API の意味的シグナルでループ終了 (テキスト解析に依存しない)
        break
    elif response.stop_reason == "tool_use":
        messages.append({"role": "assistant", "content": response.content})
        # ★ ツール結果を会話履歴に追加 (モデルが次の判断に組み込む)
        messages.append({"role": "user", "content": tool_results})
```

### 4. 構造化ハンドオフサマリー (Task 1.4)

エスカレーション時、人間オペレーターが会話履歴にアクセスできなくても対応できるよう、
必要な情報を構造化して引き継ぐ:

```python
escalate_to_human(
    reason="Refund amount exceeds automatic processing threshold",
    priority="high",
    context={
        "customer_id": "CUST-001",
        "order_id": "ORD-001",
        "root_cause": "Customer reports product defect on delivery",
        "refund_amount": 120000,
        "recommended_action": "Verify product condition and approve full refund",
        "conversation_summary": "Customer CUST-001 reported ORD-001 arrived damaged...",
    }
)
```

### 5. アンチパターン デモ (Task 1.1)

`--mode antipatterns` で3つのアンチパターンをシミュレーション確認:

| アンチパターン | 問題 | 正しいアプローチ |
|---|---|---|
| テキストで終了判断 | モデルの出力形式が変わると壊れる | `stop_reason == "end_turn"` を使う |
| 低い反復上限 | ツール呼び出しが多い複雑なタスクが途中で打ち切られる | `max_iterations` は安全ネット、主要停止機構は `end_turn` |
| 部分的なtool_use保存 | APIエラー (missing tool_use block) が発生する | `response.content` をそのまま保存する |

## 試験との対応

| 実装内容 | 試験 Task Statement |
|---|---|
| agentic loop + stop_reason + ループ終了条件 | Task 1.1 |
| tool 設計と description | Task 1.1, 2.x |
| isError パターン | Task 1.1 |
| プログラム的前提条件ゲート | Task 1.4 |
| 構造化ハンドオフサマリー | Task 1.4 |
| PostToolUse フック (データ正規化) | Task 1.5 |
| Hook vs Prompt 使い分け | Task 1.5 |
| アンチパターンの理解 | Task 1.1 |
