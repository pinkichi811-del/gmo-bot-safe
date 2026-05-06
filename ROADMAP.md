# ROADMAP.md — GMOコイン live 発注までの段階計画

dry-run 前提の現段階から、GMOコイン本番発注に至るまでのフェーズを定義する。
判断基準は `CLAUDE.md`（Hard Rules）と `DESIGN.md`（live 解禁条件）を優先する。
このファイルは **合意済みの段取り** であり、AI アシスタントが単独で前進させない。

各 Phase 完了時に、人間が明示的に「次へ」と指示した場合のみ進む。

---

## Phase 0 — 現状（2026-04-16 時点）

- dry-run のみ稼働中（`data/dry_run_orders.csv` に記録）
- 現 champion: `5min trend>=5 ma=5/20`（BTC 単独）
- 候補 champion: `btc_eth_balanced_ndx.yaml` (trial 8545, 3.15× / DD 14.3%) — **未採用**
- live 発注は三段ゲートで封印:
  1. `ENABLE_LIVE_ORDER = False`（`src/order_executor.py`）
  2. `LIVE_OK` 環境変数（`.env` でのみ yes）
  3. `_send_live_order()` が `NotImplementedError`

---

## Phase 1 — 新 champion の実装ギャップを埋める（dry-run 内）

DESIGN.md `[2026-04-15 #2]` で「⚠️ 実装未完」と明記された 4 件を片付ける。
**この段階では `order_executor.py` には一切触らない。**

| # | 作業 | 影響モジュール |
| --- | --- | --- |
| 1 | `ETH_JPY` の price feed 追加 | `market_watcher.py` |
| 2 | `regime_filter.ndx_trend` の本実装と `run_cycle` 統合 | `main.py`, 新 `regime.py` |
| 3 | `trail_pct` / `max_hold_bars` exit 判定 | `risk_guard.py`（既存テストあり） |
| 4 | multi-symbol portfolio（`max_positions` グローバル） | `state_store.py`, `risk_guard.py` |

**完了基準**:
- `tests/` で各分岐がカバー済み
- `config/strategies/btc_eth_balanced_ndx.yaml` が設定変更だけで読める
- dry-run を 1 サイクル通しても HALT しない

**Phase 1 完了 (2026-05-04)**:
- [x] ETH_JPY price feed (`src/market_watcher.py`)
- [x] `regime_filter.ndx_trend` 本実装と統合 (`src/regime_filter.py`, `src/main.py:213-249, 333-336`)
- [x] `trail_pct` / `max_hold_bars` exit 判定 (`src/risk_guard.py:83-84, 173-198`)
- [x] multi-symbol portfolio / `max_positions` グローバル制約 (`src/state_store.py`, `src/risk_guard.py:51, 298-299`)
- [x] `config/strategies/btc_eth_balanced_ndx.yaml` が `.env` の `CONFIG_PATH` 指定で読める
- [x] 各分岐の `tests/` カバー (`tests/test_portfolio.py`, `tests/test_run_cycle.py`)

---

## Phase 2 — dry-run 観察（最低 2 週間）

新 champion を dry-run で流し、backtest 想定と大きく乖離しないか確認する。

**チェック項目**:
- `scripts/aggregate.py --days 14` で sells/buys 分布・HALT 発火履歴を見る
- 旧 champion と別ディレクトリで並走させ、PF・DD が backtest 想定の ±30% 内か
- 想定外挙動（例: regime filter が常時 block で 0 trade）が無いか
- `cash_ratio` が 20% を割りかけるケースが発生していないか

**進行判断**:
- 2 週間ログが健全 → Phase 3 へ
- 乖離が大きい / 頻繁に HALT → Phase 1 へ戻る

---

## Phase 3 — read-only API の疎通（live 注文はまだ出さない）

GMOコインの API に対して **読み取りのみ** 疎通させ、認証・署名周りを固める。

**作業内容**:
- `GmoMarketDataSource` を public + private（残高取得のみ）で実装
- HMAC-SHA256 署名・nonce 管理・clock skew 対策のユニットテスト
- `.env` からのキー読み込みとログマスクの検証（CLAUDE.md Hard Rule #2）
- `GET /v1/account/assets` を叩き、残高が読めるところまで

**完了基準**:
- 実環境で残高取得レスポンスが取れる
- ログに API キー・シークレットが一切出ない
- 署名失敗・nonce 衝突のテストがある

---

## Phase 4 — live 発注コード実装（別 PR 必須・レビュー前提）

`_send_live_order` の TODO を埋める。
**`ENABLE_LIVE_ORDER` はこの Phase では False のまま。** 単独 PR で統合しない。

**作業内容**:
- `POST /v1/order` 現物・成行（最小ロット想定）
- `order_id` 記録、`GET /v1/executions` での約定確認
- timeout / retry / 部分約定の扱い
- 失敗を `RiskGuard.on_error` に伝播
- `risk_guard` の HALT 条件に「注文 reject 連発」を追加
- モック API を使った `tests/` の全分岐テスト

**完了基準**:
- 全テストが通る
- コードレビューを経た状態でマージ済み
- `ENABLE_LIVE_ORDER` はまだ False

---

## Phase 5 — 三段ゲート解除（人間の手だけ）

DESIGN.md §1 の live 解禁 7 条件がすべて満たされた時のみ進む。

1. **単独 PR** で `src/order_executor.py` の `ENABLE_LIVE_ORDER = True`
2. `.env` に `LIVE_OK=yes` かつ `CONFIRM_LIVE=yes`
3. `config/app.yaml` の `risk.per_trade_jpy_max` を一時的に **1,000〜3,000 円** に絞る
4. `scripts/run_live.sh` で起動

AI アシスタントは **1 のコミットを絶対に自動で作らない**。

---

## Phase 6 — 小額 live 並走（1〜2 週間）

最小ロットで live を動かしつつ、dry-run と判定一致を確認する。

**チェック項目**:
- 同じ config で dry-run と live を並走、判定が一致するか比較
- 約定価格と `price_ref` の乖離分布の観測
- 毎日終わりに手動で残高チェック
- HALT 条件（reject・価格乖離）の発火履歴確認

**進行判断**:
- 判定一致率 ≥ 95%、乖離が許容内 → Phase 7 へ
- 想定外 reject / 大きな乖離 → 即 `touch STOP`、原因特定後 Phase 4 へ戻る

---

## Phase 7 — 段階的サイズアップ

`per_trade_jpy_max` を 2 倍ずつ、各段階で 1 週間以上観察。

**停止条件**:
- DD が backtest 想定の 1.5 倍を超えたら即 `touch STOP`
- 連続 HALT が起きたらサイズを前段階に戻す

---

## 現在地

**Phase 2**（dry-run 観察 / 開始日 2026-05-04）。

- 本命 = balanced_ndx champion (`.env` で `CONFIG_PATH=./config/strategies/btc_eth_balanced_ndx.yaml`)
- 並走 = 旧 champion (`config/app.yaml` = 5min trend>=5 ma=5/20, BTC 単独) を `./data/compare_old` で
- 起動 = `bash scripts/dry_run_compare.sh`
- 集計 = `python scripts/aggregate.py --days 14 [--state-dir ./data/compare_old]`
- 観察ノートと進行判断クライテリア = [`docs/PHASE_2_OBSERVATION.md`](docs/PHASE_2_OBSERVATION.md)
