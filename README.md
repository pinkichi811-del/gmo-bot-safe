# gmo-bot-safe

GMOコイン向け 暗号資産自動売買bot（安全寄り構造・現物のみ）

**現段階: dry-run 観察フェーズ。live 発注は三段ゲートで封印中。**

## いま何ができて、何が封じられているか

### できること（dry-run）

- 市場データ取得（スタブ `StubMarketDataSource` で動作 / 実 API 未接続）
- 1 サイクルの完全な制御フロー（HALT / STOP / score / sell / buy / portfolio制約 / 記録）
- サブスコア算出: `trend / liquidity(+spread) / heat(+rsi) / volatility / dup_penalty / cash_bonus`
- 発注判定（ルール最終判断・AI は補助）
- 発注候補の JSONL / CSV 記録（`data/dry_run_orders.*`）
- 観察用サイクルログ（`data/score_log/YYYY-MM-DD.jsonl`・日次ローテート）
- 擬似約定での state 更新（`data/state.json`）
- 集計スクリプト（`scripts/aggregate.py`）・状態表示（`scripts/show_status.sh`）
- アプリログの日次ローテート（`logs/bot.log`）
- Slack/Discord webhook 通知の抽象層
- systemd / logrotate の配置雛形（`deploy/`）
- スモークテスト 19 件（`python -m unittest discover tests`）

### 封じられていること（意図的）

1. **`ENABLE_LIVE_ORDER = False`**（`src/order_executor.py` のコードゲート）
   → PR でしか書き換えられない。`.env` や CLI からは変更不可
2. **`LIVE_OK=no`**（環境変数ゲート）
   → `yes` にしても 1. で弾かれる
3. **`_send_live_order()` は未実装**（`NotImplementedError`）
   → 1. と 2. の両方を通過してもここで止まる
4. `scripts/run_live.sh` は `CONFIRM_LIVE=yes` かつ `LIVE_OK=yes` がないと起動拒否
5. `.claude/settings.local.json` で `run_live.sh` と `.env` アクセスを deny

つまり **実注文を出すには、3 箇所のコード変更 + 2 つの環境変数 + CLI 明示承認** が揃う必要がある。

## 概要

- 取引所: GMOコイン
- 稼働環境: VPS（systemd 想定）
- 取引種別: 現物のみ
- 方針: 安全優先。異常時は HALT。**自動再開しない**。
- AI: 監視・スコア補助のみ（発注可否はルールが最終判断）

関連ドキュメント:
- `CLAUDE.md` … AI アシスタント向けの禁止事項とルール
- `DESIGN.md` … 運用ルール・HALT 復帰・live 解禁条件
- `deploy/README.md` … VPS 配置手順
- `config/app.yaml` … 閾値・リスク上限・スコア定数（**すべてここに集約**）

## 制約

| 項目 | 値 |
| --- | --- |
| 監視銘柄数 | ≤ 5 |
| 同時保有数 | ≤ 3 |
| 最低現金比率 | ≥ 20% |
| core 1銘柄あたり最大 | 35% |
| satellite 1銘柄あたり最大 | 25% |
| 1サイクル最大注文 | 2 |
| core銘柄 | BTC_JPY, ETH_JPY |
| satellite銘柄 | SOL_JPY, XRP_JPY, DOGE_JPY |
| 市場監視周期 | 5分 |
| スコア更新周期 | 15分 |

## フォルダ構成

```
gmo-bot-safe/
├── README.md
├── CLAUDE.md
├── DESIGN.md
├── requirements.txt
├── .gitignore
├── .env.example
├── config/app.yaml
├── .claude/settings.local.json
├── deploy/
│   ├── README.md
│   ├── gmo-bot-safe.service
│   └── logrotate.conf
├── src/
│   ├── main.py             # エントリポイント・ループ・観察ログ
│   ├── market_watcher.py   # 抽象ソース + Stub
│   ├── scorer.py           # trend/liq/heat/vol/dup + cash_bonus
│   ├── risk_guard.py       # 判定・HALT・制約
│   ├── order_executor.py   # dry-run 記録 / live は三段ゲート封印
│   ├── notifier.py         # Console + Webhook
│   └── state_store.py      # JSON 永続化
├── scripts/
│   ├── dry_run.sh
│   ├── run_live.sh         # 封印中
│   ├── stop_bot.sh
│   ├── aggregate.py        # 観察ログ集計
│   └── show_status.sh      # 現状ダイジェスト
├── tests/test_smoke.py
├── data/                   # state.json / dry_run_orders.* / score_log/
└── logs/                   # bot.log（日次ローテート）
```

## セットアップ

```bash
git clone <repo> gmo-bot-safe
cd gmo-bot-safe

python -m venv .venv
source .venv/bin/activate         # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
# .env: RUN_MODE=dry_run / LIVE_OK=no / CONFIRM_LIVE=no は変えない

mkdir -p data logs
```

### 起動前チェック

- [ ] `.env` の `RUN_MODE=dry_run`
- [ ] `LIVE_OK=no`, `CONFIRM_LIVE=no`
- [ ] `config/app.yaml` の `mode: dry_run`
- [ ] `STOP` ファイルは状況に応じて（通常は存在させない）

## 実行

```bash
# dry-run（現段階はこれのみ）
bash scripts/dry_run.sh

# 状態の確認
bash scripts/show_status.sh

# 今日の集計
python scripts/aggregate.py

# 直近 7 日
python scripts/aggregate.py --days 7

# 停止
bash scripts/stop_bot.sh

# 新規買いを即止め（保有の損切り・利確は継続）
touch STOP
# 解除
rm STOP
```

## 観察ログ

### コンソール（サイクルごと）

```
INFO main portfolio cash=1000000 equity=1000000 cash_ratio=1.000
INFO main   symbol      total  trend    liq   heat    vol    dup   cash   rule     ai  verdict
INFO main   XRP_JPY      97.6   15.4   12.2    5.0    0.0    0.0    5.0   97.6    0.0  below_threshold:trend(15.4<18)
INFO main   ETH_JPY      84.2    3.5   10.7    5.0    0.0    0.0    5.0   84.2    0.0  below_threshold:trend(3.5<18)
INFO main decisions sells=0 buys=0
```

### JSONL（`data/score_log/YYYY-MM-DD.jsonl`・日次ローテート）

1 行 = 1 サイクル。`jq` で集計可能:

```bash
# 今日のサイクル数
wc -l data/score_log/$(date -u +%F).jsonl

# 見送り理由の分布
jq -r '.evaluations[].verdict' data/score_log/$(date -u +%F).jsonl \
  | sort | uniq -c | sort -rn

# 銘柄ごとの buy_candidate 回数
jq -r '.evaluations[] | select(.buy_candidate) | .symbol' \
   data/score_log/$(date -u +%F).jsonl | sort | uniq -c
```

もっと楽に見たい時は `python scripts/aggregate.py` が verdict 分布・平均スコア・決定数を一括で出す。

### verdict の種類

| verdict | 意味 |
| --- | --- |
| `selected` | 今サイクルで発注候補に選ばれた |
| `passed_but_not_selected` | 閾値は満たしたが `max_orders_per_cycle` 等で選外 |
| `portfolio:max_positions(3)` | 同時保有上限で弾かれた |
| `portfolio:cash_ratio(0.18<0.20)` | 約定後の現金比率が下限を割る |
| `below_threshold:trend(14.7<18),liquidity(7.4<10)` | スコア閾値未達 |
| `already_held` | 既に保有 |
| `cooldown(120min_left)` | 再エントリー禁止期間（残時間付き） |
| `dup_penalty(-10.0<=-8.0)` | 重複保有ペナルティで弾かれた |
| `stop_file` | `STOP` ファイル存在中 |

## 安全設計サマリー

- `risk_guard.py` がすべての発注の前段に立つ
- 異常検知・連続エラーで HALT → **自動再開しない**
- `STOP` ファイルで新規買いを即時停止
- `order_executor.py` は dry-run ではログ出力のみ
- live は三段ゲート（`ENABLE_LIVE_ORDER` / `LIVE_OK` / 未実装）で完全封印
- `.claude/settings.local.json` で `run_live.sh` と `.env` を deny
- API キーはコミットしない（`.gitignore` 済み）

## 観察フェーズの調整候補

仮実装の数式なので、観察データを踏まえて config を調整する（コード変更なし）:

### スコア計算（`config/app.yaml` の `scorer:` 以下）
- [ ] `trend.ratio_clamp` ±0.05 のスケールが妥当か
- [ ] `liquidity.volume_divisor` 5.0 を実データの出来高レンジに合わせる
- [ ] `spread.tight/wide_threshold_pct` を実スプレッドの分布に合わせる
- [ ] `heat.up/down_threshold_pct` 5% を銘柄ボラに応じて可変にするか
- [ ] `rsi.overbought/oversold` 75/25 を実データで校正
- [ ] `volatility.low/high_threshold_pct` はスタブだと発動しない。実データで見直し
- [ ] `dup_penalty.same_group` -5 が弱すぎないか
- [ ] `base_score` 60 のオフセットが閾値 70/78 に対して妥当か
- [ ] `cash_bonus` が常時発動して実質閾値を下げていないか

### 閾値（`scorer.thresholds`）
- [ ] `buy_candidate` がほぼ出ない / 多すぎる → 再調整
- [ ] `strong_buy` と `buy_candidate` の差が効いているか

### ポートフォリオ・リスク
- [ ] `per_trade_jpy_max=10000` は観察スケール。実資金では equity ベースの % に切替検討
- [ ] `portfolio.initial_cash_jpy` は dry-run 擬似専用

### 未実装（コード側）
- [ ] `GmoMarketDataSource`（実 API 接続）
- [ ] `_ai_score`（AI 補助・現状 0 固定）
- [ ] `_send_live_order`（実注文送信・現状 NotImplementedError）

## live 化する前に必要なこと

（`DESIGN.md` と重複するが要点を再掲）

1. dry-run で最低 2 週間の観察ログがある
2. `aggregate.py` でスコア分布・verdict 分布・HALT 頻度が健全
3. `GmoMarketDataSource` を実装し、スタブと二重に走らせて健全性確認
4. `_ai_score` を実装するならしておく（任意）
5. `_send_live_order` を実装（別 PR、小額から）
6. `risk_guard` の全分岐に対するテスト整備
7. 手動で `ENABLE_LIVE_ORDER = True` に変更（PR レビュー必須）
8. `.env` に `LIVE_OK=yes` `CONFIRM_LIVE=yes` を設定
9. `per_trade_jpy_max` を小額から開始

## 現段階の実装状況

- [x] フォルダ構成 / 設定ファイル / 主要モジュール
- [x] dry-run 運用ルール（`DESIGN.md`）
- [x] 市場データ抽象層 + Stub
- [x] スコアリング（仮実装・観察フェーズ）
- [x] 観察ログ（日次 JSONL ローテート + コンソールテーブル）
- [x] 集計スクリプト（`aggregate.py` / `show_status.sh`）
- [x] live 三段ゲート（封印状態）
- [x] Console + Webhook 通知
- [x] アプリログ日次ローテート
- [x] systemd / logrotate 雛形
- [x] スモークテスト（19 件）
- [ ] スコア数式の本実装（観察結果を見てから）
- [ ] AI スコア
- [ ] GMOコイン実 API
- [ ] live 発注本体（**封印中・現段階では開けない**）
