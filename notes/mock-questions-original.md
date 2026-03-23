# 自作模擬問題集

> **注意**: これらはすべて自作のオリジナル問題です。試験の実際の問題とは異なります。

## Domain 1: Agentic Architecture & Orchestration

### Q1-01
顧客サポートエージェントを設計しています。「VIP 顧客には通常より優先度の高い対応をすること」というルールをどのように実装すべきか。

A) system prompt に「VIP 顧客には優先対応してください」と記述する  
B) `get_customer` ツールが VIP フラグを返し、orchestrator がそれを確認してから処理ルートを分岐する  
C) モデルに VIP 顧客かどうか判断させ、適切に対応させる  
D) VIP 用とそうでない用の 2 つのエージェントを用意する  

**正解: B**  
理由: VIP フラグはデータから確実に取得し、code で処理ルートを制御する。prompt だけでの制御はエッジケースで破られる可能性がある。

---

### Q1-02
Multi-agent システムで web-search subagent が timeout した場合、最も適切な対応はどれか。

A) エラーをそのまま orchestrator に伝播させ、全処理を中断する  
B) timeout を無視して他の subagent の結果だけで synthesis する  
C) structured error を返し、orchestrator が利用可能な結果で graceful degradation する  
D) timeout した subagent を自動的にリトライする  

**正解: C**  
理由: partial failure は structured error で表現し、orchestrator が利用可能なデータで処理を継続する。全中断も無視も適切でない。

---

### Q1-03
Subagent に「前の手順で取得した顧客情報を使って処理して」と指示している。最も問題のある点はどれか。

A) subagent が独立したコンテキストで動作するため、「前の手順」の情報が引き継がれない  
B) subagent が顧客情報を誤って変更する可能性がある  
C) subagent が多すぎてコストがかかる  
D) subagent の出力形式が統一されていない  

**正解: A**  
理由: Subagent は親の会話コンテキストを自動継承しない。必要な情報は明示的に渡す必要がある。

---

## Domain 2: Tool Design & MCP Integration

### Q2-01
MCP ツールで「注文が存在しない」場合のエラーを表現する最も適切な方法はどれか。

A) Python の例外 (Exception) を発生させる  
B) `{"isError": true, "content": [{"type": "text", "text": "Order not found"}]}` を返す  
C) `None` を返す  
D) 空のオブジェクト `{}` を返す  

**正解: B**  
理由: MCP 仕様では tool execution error は `isError: true` で返す形が明示されている。これにより モデルが適切な次のアクションを選択できる。

---

### Q2-02
`lookup_order` と `process_refund` を 1 つのツール `handle_order` にまとめることを提案された。最も適切な判断はどれか。

A) まとめることでコードがシンプルになるため、まとめるべき  
B) ツール数が減ってモデルの判断が簡単になるため、まとめるべき  
C) 読み取り操作と書き込み操作を分離すべきで、まとめるべきでない  
D) どちらでもよく、チームの好みで決める  

**正解: C**  
理由: 単一責任の原則に従い、読み取りと書き込みは分離する。神ツールは description の質が下がり、モデルが誤った操作を選ぶリスクがある。

---

## Domain 3: Claude Code Configuration & Workflows

### Q3-01
チーム開発で「本番データベースには絶対に書き込まない」というルールを強制したい。最も適切な方法はどれか。

A) CLAUDE.md に「本番 DB には書き込まないこと」と記述する  
B) .claude/rules/ に本番 DB 関連ファイルのルールを追加する  
C) 本番 DB の接続文字列を環境変数で管理し、CI 環境では設定しない  
D) plan mode を常に使用することでレビューを必須にする  

**正解: C**  
理由: 「本番に書き込まない」という制約は deterministic に実装すべき。credential を CI 環境に設定しないことが最も確実。A や B は ガイドラインであり強制力がない。

---

### Q3-02
大規模なリファクタリングを Claude Code で実施する前に取るべき最も適切なアクションはどれか。

A) system prompt に詳細な手順を書いて direct execution する  
B) plan mode で変更計画を生成・確認してから実行する  
C) subdir の CLAUDE.md に一時的なルールを追加する  
D) `-p` フラグを使って non-interactive で実行する  

**正解: B**  
理由: 大規模・複雑な変更は plan mode で先に計画を確認する。不可逆的な操作をレビューなしに直接実行するのは不適切。

---

## Domain 4: Prompt Engineering & Structured Output

### Q4-01
請求書から金額を抽出するとき、モデルが「合計額 $1,050」と抽出したが、行項目の合計を計算すると $1,000 だった。この状況への最適な対応はどれか。

A) stated_total ($1,050) を正解として採用する  
B) calculated_total ($1,000) を正解として採用する  
C) 乖離を semantic validation エラーとして記録し、human review に回す  
D) モデルに再計算させてリトライする  

**正解: C**  
理由: stated_total と calculated_total の乖離は semantic validation エラー。金額の乖離は human review で確認すべき。自動的にどちらかを正解とするのは不適切。

---

### Q4-02
JSON 出力を確実に取得したい。tool_choice に指定すべき最も適切な値はどれか。

A) `{"type": "auto"}`  
B) `{"type": "any"}`  
C) `{"type": "tool", "name": "extract_data"}`  
D) tool_choice は指定しない  

**正解: C**  
理由: 特定のツールの呼び出しを保証するには specific tool を指定する。auto では tool が使われない可能性がある。

---

## Domain 5: Context Management & Reliability

### Q5-01
長いシステムプロンプトで「返金ルール」が lost-in-the-middle になっている可能性がある。最も適切な対策はどれか。

A) プロンプトを短くする  
B) 返金ルールをシステムプロンプトの先頭に移動する  
C) 返金ルールを別のツールで取得させる  
D) 返金ルールを毎回ユーザーターンの末尾に追加する  

**正解: B**  
理由: システムプロンプト内では先頭が最もよく参照される。重要なルール・制約はシステムプロンプトの先頭に配置するのが最も効果的。D も有効な対策だが、「long いシステムプロンプトの中で lost になっている」という前提に対しては B が root cause に直接効く選択肢。

---

### Q5-02
web-search subagent が返した情報を synthesis するとき、最も重要なことはどれか。

A) 情報を自然な文章に変換する  
B) 情報量を削減して簡潔にまとめる  
C) 各情報の出典 (provenance) を保持したまま合成する  
D) 最新の情報を優先して古い情報を削除する  

**正解: C**  
理由: Multi-agent システムでは provenance の保持が重要。出典なしの情報は信頼性の評価ができず、誤情報混入時のトレースバックも不可能になる。
