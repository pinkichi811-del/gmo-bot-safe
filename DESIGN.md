# DESIGN.md — dry-run 前提の運用ルール

## Champion History（本命設定の差し替え履歴）

### [2026-04-15 #2] 候補更新: BTC+ETH + NDX trend filter (10K trial 拡張後)

**概要**: 多銘柄空間を 10,000 trial に拡張、**より良い候補が 2 つ判明**:
- **balanced (推奨)**: `trial 8545` → **3.15× / DD 14.3%** → `config/strategies/btc_eth_balanced_ndx.yaml`
- **aggressive**: `trial 4988` → **3.76× / DD 20.3%** → `config/strategies/btc_eth_aggressive_ndx.yaml`

**両候補の共通パターン**:
- `symbols: [BTC_JPY, ETH_JPY]`
- `trend: ma_long ~44` (前回 champion 2547 の 29 より長い)
- `tp 6〜7`, `sl -3〜-4`
- **`max_hold_bars=288` (24h) + `trail_pct=3.0` + `cooldown_min=180`** のセット
- **`ndx_trend: ma_short=5, ma_long=10`** 単独 (us_hours / vix 不使用でも勝てる)

**trial 8545 (balanced) を推奨する理由**:
- DD が trial 4988 より 6 pt 低い (14% vs 20%) → 自動化で人間が止めたくならない
- max_positions=1 で CLAUDE.md の現状制約 (同時 1 本) を維持
- 4 期間 PF: 1.49 / 1.71 / 2.46 / 2.16 と全部 1.5 以上 (final も強い)

**vs 前回 candidate (trial 2547, 3.32× / DD 25%)**:
- balanced は倍率ほぼ同等 (-5%) だが DD を 11 pt 削減
- aggressive は倍率 +13%、DD -5 pt

**⚠️ 実装未完 (同じく)**: `trail_pct` / `max_hold_bars` / `regime_filter.ndx_trend` / ETH price feed / multi-position portfolio。

---

### [2026-04-15] 候補発見: `BTC+ETH 多銘柄 + 多層 filter` (未実装)

**概要**: 13,000+ trial のローカル探索で発見された新候補。**4 年 3 ヶ月複利運用で 10K → 33,156 円 (3.32×) / DD 25%**。
→ `config/strategies/btc_eth_multi_champion.yaml`

**パラメータ (trial 2547)**:
- `symbols: [BTC_JPY, ETH_JPY]`, `max_positions: 1`
- `trend: ma_short=5, ma_long=29`, `buy_trend=3`
- `tp=+5.0`, `sl=-3.5`, **`trail_pct=3.0`**, `cooldown_min=60`
- regime filter: `spx_trend(5/10)` + `vix<=25` + `us_hours` + `events_avoid(60min)`

**vs 現 champion (BTC 単独 5min trend>=5 ma=5/20)**:
| 指標 | 現 champion | 新候補 |
|---|---|---|
| 4 年複利倍率 | 〜1.1× | **3.32×** |
| 最大 DD | 0.56% (単利) / ~2% (複利) | 0.29% (単利) / **25% (複利)** |
| 全期間 PF | val 1.00 / final 1.02 | val 1.44 / final 1.48 |

**発見事項**:
1. **us_hours フィルターが top 20 の全 20 候補に登場** — 最大の共通因子
2. ETH 追加で BTC 単独 (2.74×) を +21% 上回る — 旧 PDCA の「多銘柄は罠」を完全に上書き
3. `trail_pct` と `max_hold_bars` が DD 抑制に効く

**⚠️ 差し替え保留**: 以下の実装が src/ に未完のため、dry-run/live で直ちに動かせない。
1. `src/market_watcher.py` に `ETH_JPY` の price feed 追加
2. `src/main.py::run_cycle` に `regime_filter` 呼び出し統合 (SPX/VIX/US hours/events)
3. `src/` に `trail_pct` / `max_hold_bars` の exit 判定
4. multi-symbol portfolio 管理 (max_positions グローバル制約)

**次の段階**: 上記実装 → dry-run で現 champion と並走観測 (2〜4 週間) → 本採用判断

---

### [2026-04-14] champion 差し替え: `5min trend>=5 ma=5/20`

**直前の champion**: `5min trend>=3 ma=3/10`（旧 v1 Pattern A）
→ `config/strategies/baseline_5min_trend3_ma3_10.yaml` に保管

**差分**:
| key | 旧 | 新 |
| --- | --- | --- |
| trend.short_ma | 3 | **5** |
| trend.long_ma | 10 | **20** |
| buy_candidate.trend | 3 | **5** |
| 他（TP/SL/cooldown/symbols/max_positions） | 同じ | 同じ |

**成績比較（fee 0.05% 込み）**:
| 期間 | 旧 net / PF | 新 net / PF |
| --- | --- | --- |
| train 2024-01〜2025-06 | +1.345% / 1.38 | +1.06% / **1.45** |
| val 2025-07〜2025-12 | +0.077% / 1.15 | +0.00% / **1.00** |
| final 2026-01〜2026-03 | **-0.193%** / **0.75** | **+0.00%** / **1.02** |

**差し替え理由**:
- 旧は train の edge は大きいが val/final で overfit 露呈（final PF 0.75 で明確に劣化）
- 新は train の絶対収益は 0.3 ポイント下がるが、**val/final ともに PF ≥ 1.0** を維持
- max DD も 0.43% → 0.20% に改善
- 「多少地味でも期間をまたいで壊れない」方針に沿う

**並走研究候補**:
- `1H trend>=5 ma=5/20` → `config/strategies/research_1h_trend5_ma5_20.yaml`
- 別ディレクトリで dry-run すれば同時観察可能

---

## 運用ルール

このドキュメントは、**現段階（live 未実装・dry-run 前提）** における
運用・意思決定・緊急対応のルールをまとめたもの。

変更は人間の合意を経てから行う。AI アシスタントが勝手に閾値を触らない。

---

## 1. 稼働モード

| モード | 説明 | 現段階の扱い |
| --- | --- | --- |
| `dry_run` | 発注判定までは本番同様に走るが、注文はログ出力のみ | **これだけ使う** |
| `live` | 実際に GMOコインへ発注する | **未実装。封印。** |

live の解禁は、以下すべてが揃った時のみ:

1. dry-run で最低 2 週間の観察ログがある（`scripts/aggregate.py --days 14` で確認）
2. `risk_guard` の全分岐に対するテストが通っている
3. `GmoMarketDataSource` が実装され、スタブと整合している
4. `_send_live_order()` が実装されている（別 PR・レビュー必須）
5. `src/order_executor.py` の `ENABLE_LIVE_ORDER = True` に書き換える（**コードゲート解除**）
6. `.env` で `LIVE_OK=yes` かつ `CONFIRM_LIVE=yes` を設定（**環境ゲート解除**）
7. 小額（`risk.per_trade_jpy_max` を一時的に小さく）から始める

上記 5 と 6 の両方が揃わないと `OrderExecutor._live_execute` は
`blocked_by_code_gate` / `blocked_by_env_gate` / `not_implemented` を返して注文を drop する。

---

## 2. ループ設計

- 市場監視（`market_watcher.fetch`）: **5 分ごと**（`watch_interval_sec: 300`）
- スコア更新（`scorer.score`）: **15 分ごと**（`score_interval_sec: 900`）
- 1 サイクル（= 1 score 更新）で出す新規注文の上限: **2 件**（`max_orders_per_cycle: 2`）

監視は頻繁に、判断はゆっくり、発注は少なく。

---

## 3. 銘柄と配分

| 区分 | 銘柄 | 1銘柄あたり上限 |
| --- | --- | --- |
| core | BTC_JPY, ETH_JPY | 総資産の 35% |
| satellite | SOL_JPY, XRP_JPY, DOGE_JPY | 総資産の 25% |

- 監視銘柄数は最大 5
- 同時保有は最大 3
- 約定後に**現金比率が 20% を下回る新規買いは出さない**

---

## 4. 買い判定

### 4.1 買い候補（`buy_candidate`）
以下を**すべて**満たす銘柄のみ候補に上がる:

| 指標 | 閾値 |
| --- | --- |
| TotalScore | ≥ 70 |
| Trend | ≥ 18 |
| Liquidity | ≥ 10 |
| Heat | ≥ -8 |

### 4.2 強い買い候補（`strong_buy`）
さらに以下を満たせば優先度を上げる:

| 指標 | 閾値 |
| --- | --- |
| TotalScore | ≥ 78 |
| Trend | ≥ 22 |
| Liquidity | ≥ 12 |
| Heat | ≥ -5 |

### 4.3 見送り条件
- DupPenalty ≤ -8（同種銘柄の重複保有を避ける）
- `STOP` ファイルが存在する
- HALT 中
- 現金比率 < 20% になる

---

## 5. 売り判定

| 条件 | 閾値 |
| --- | --- |
| 損切り候補 | 建値から -4% |
| 利確候補 | 建値から +6% |

いずれも**候補**。最終的に発注するか否かは `risk_guard` が健全性（板・スプレッド・乖離）
を見て決める。

---

## 6. クールダウン

同一銘柄の再エントリーは **180 分経過後** まで禁止。
クールダウンは `state_store` で銘柄ごとに管理。

---

## 7. HALT（停止）

### 7.1 HALT する条件
- 連続 `max_consecutive_errors (=5)` 回のエラー
- 直近価格との乖離が `halt_on_price_gap_pct (=10%)` を超えた
- 想定外の API レスポンス
- 保有・残高の整合性が取れない

### 7.2 HALT 中の挙動
- 新規注文・売り注文ともに出さない
- ループ自体は生き、状態の観測・ログ出力は続ける
- **自動再開しない**（`auto_resume: false`）

### 7.3 HALT からの復帰手順（人間のみ）
1. ログで HALT の原因を特定する
2. 必要であればコード修正・設定変更
3. `data/state.json` の `halted` を `false` に戻す（または削除して再生成）
4. bot を再起動

---

## 8. STOP ファイル

ルート直下の `STOP` ファイルはキルスイッチ。

- `touch STOP` → 以降、**新規買いを出さない**（保有の損切り・利確は通常通り評価）
- `rm STOP` → 通常運用に戻せる

HALT との違い:

| | HALT | STOP ファイル |
| --- | --- | --- |
| 発火 | 自動（異常検知） | 手動 |
| 解除 | 手動（state 編集＋再起動） | 手動（ファイル削除のみ） |
| 売り注文 | 出さない | 通常通り評価する |
| 想定用途 | 異常時のフェイルセーフ | 運用都合の一時停止 |

---

## 9. ログと観測

- `logs/` に日次ローテーションで出力（今後実装）
- 必ず記録するもの:
  - スコア算出結果（銘柄 / total / trend / liquidity / heat / dup_penalty）
  - 発注候補の採否理由
  - HALT 発火時の原因
  - `STOP` ファイルによる抑止
- シークレット（API キー等）はログに出さない

---

## 10. 変更管理ルール

以下の値を変更する場合は **commit メッセージに理由を明記** すること:

- `limits.*`（ポジション・配分上限）
- `risk.*`（HALT 条件、1注文上限、現金比率ブロック）
- `scorer.thresholds.*`（買い閾値）
- `exits.*`（損切り・利確・クールダウン）
- `loop.max_orders_per_cycle`

AI アシスタントはこれらを**単独判断で変更しない**。人間の指示が必要。

---

## 11. dry-run 期間のチェックリスト（運用者向け）

毎日:
- [ ] `logs/` に想定通りのサイクルが記録されているか
- [ ] HALT が発火していないか、発火していれば原因は妥当か
- [ ] スコアの分布が極端に偏っていないか

週次:
- [ ] 買い候補がゼロだった/多すぎた日の傾向を確認
- [ ] DupPenalty の効き方が意図通りか
- [ ] 想定と違うスコアを出す銘柄が無いか
