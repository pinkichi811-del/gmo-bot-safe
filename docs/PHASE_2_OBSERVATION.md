# Phase 2 観察ノート — balanced_ndx champion (BTC+ETH)

新 champion を dry-run で 2 週間以上流し、backtest 想定との乖離を検証する。
進行判断は ROADMAP.md Phase 2、Hard Rules は CLAUDE.md / DESIGN.md を優先。

---

## 観察期間

- **開始日**: Oracle Cloud 上で `systemctl start gmo-bot-safe-primary gmo-bot-safe-compare-old` した日 (UTC)
- **最低期間**: 14 日 (ROADMAP.md Phase 2 規定)
- **終了予定**: 開始日 + 14 日以降で進行判断
- **稼働基盤**: Oracle Cloud Always Free Linux VM + systemd ([ORACLE_CLOUD_SETUP.md](ORACLE_CLOUD_SETUP.md))
- **NDX 更新**: `fetch-index-daily.timer` が毎日 22:00 UTC に [scripts/fetch_index_daily.py](../scripts/fetch_index_daily.py) を発火、Yahoo Finance v8 chart API から NDX/SPX/VIX を更新

> 旧記述では 2026-05-04 を開始日としていたが、PC スリープ問題で Oracle Cloud に
> 基盤を移したため、観察開始は VM 上で起動した日にリセットする (2026-05-06 修正)。

---

## 観察対象

### 本命 — balanced_ndx champion (BTC+ETH, NDX trend filter)
- config: `config/strategies/btc_eth_balanced_ndx.yaml`
- backtest 想定: 複利 3.15× / max DD 14.3% / 50ヶ月 190 trades
- 期間別 PF: train_extra 1.49 / train 1.71 / val 2.46 / final 2.16
- STATE_DIR: `./data`
- LOG_DIR: `./logs`

### 並走 — 旧 champion (5min trend>=5 ma=5/20, BTC 単独)
- config: `config/app.yaml`
- backtest 想定: 複利 1.1× (train +1.06% PF 1.45 / val +0.00% PF 1.00)
- STATE_DIR: `./data/compare_old`
- LOG_DIR: `./logs/compare_old`

### 起動コマンド (本番 = Oracle Cloud)
```bash
sudo systemctl enable --now gmo-bot-safe-primary.service
sudo systemctl enable --now gmo-bot-safe-compare-old.service
sudo systemctl enable --now fetch-index-daily.timer
```
詳細は [ORACLE_CLOUD_SETUP.md](ORACLE_CLOUD_SETUP.md) Step 7-8 参照。

### 起動コマンド (ローカル動作確認のみ)
```bash
bash scripts/dry_run_compare.sh
```

両プロセスを別 STATE_DIR で同時起動する。Ctrl+C で両方停止。長期観察には PC スリープが
障害になるため使わない (Oracle Cloud に移行する経緯参照)。

---

## 進行判断クライテリア

ROADMAP.md Phase 2 の `±30%` 規定に基づく許容範囲。

| 指標 | backtest 期待値 | 許容範囲 (±30%) | 判定 |
|---|---|---|---|
| 期間 PF | 1.49〜2.46 | 1.04〜3.20 | ☐ |
| max DD | 14.3% | 〜18.6% (1.3 倍まで) | ☐ |
| 月次 trades | 〜4 (190 / 50ヶ月) | 2〜6 | ☐ |
| regime gate block 率 | 不明 (NDX trend 次第) | 0% (常時 pass) も 100% (常時 block) も要調査 | ☐ |
| cash_ratio min | max_core_ratio=0.50 想定 | 0.20 を絶対割らない | ☐ |
| 連続 HALT | 0 回 | 1 サイクルで 2 回以上で要調査 | ☐ |

### 進行判断ルール

- 全クライテリアが許容範囲内 → **Phase 3 (read-only API 実装)** へ
- 1 つでも外れる → 原因特定。仕様起因なら Phase 1 に戻る判断 (ROADMAP.md より)

---

## 集計コマンドのチートシート

```bash
# 14 日サマリ (本命)
python scripts/aggregate.py --days 14

# 14 日サマリ (並走)
python scripts/aggregate.py --days 14 --state-dir ./data/compare_old

# 詳細トレード一覧
python scripts/aggregate.py --days 14 --show-trades

# 偽損失閾値を調整 (再起動跨ぎの phantom loss を除外する閾値)
python scripts/aggregate.py --days 14 --fake-loss-pct -25

# 単日サマリ
python scripts/aggregate.py --date 2026-05-05
```

集計が出す主要セクション:
- `pnl summary` — trades / wins / losses / pnl_jpy / pnl_pct / PF / max_DD
- `regime gate` — blocks / total cycles / block_rate
- `cash_ratio` — min / mean / below_0.20 件数
- `verdict distribution`, `decisions by symbol` — 既存

---

## 日次観察ログ

各日、両ディレクトリで `aggregate.py --date <UTC_date>` を実行し、要点を記録する。

### Day 1 (2026-05-04)
- (本命): `cycles=N halted=N trades=N pnl=±X.XX% PF=X.XX DD=-X.XX% block_rate=X.X% cash_min=0.XXX`
- (並走): 同上
- 気づき:

### Day 2 (2026-05-05)
- ...

### Day N ...

---

## トラブル発生時の手順

### HALT 連発
1. `touch STOP` (両ディレクトリで `STOP` ファイル作成。`stop_file` 設定はデフォルト `./STOP`)
2. 該当時刻のログを `logs/bot.log` / `logs/compare_old/bot.log` で確認
3. 連続エラーが `risk.max_consecutive_errors=5` を超えていないかチェック
4. 仕様起因 → ROADMAP Phase 1 へ戻る判断
5. 復帰: `state.json` の `halted=false` 編集 + `STOP` 削除 + 再起動

### 偽損失再発 (state.json と stub 価格の不整合)
- 集計は `--fake-loss-pct -30` のフィルタで除外されるため実害なし
- 観察は中断しない
- 頻発する場合は閾値調整 (`--fake-loss-pct -25` など)

### 想定外挙動 (例: regime filter が常時 block で 0 trade)
- `regime gate` セクションの `block_rate_pct` を毎日確認
- 100% 続く場合 → `data/market/NDX_d.csv` の鮮度を確認
- CSV 更新が止まっていれば `scripts/fetch_backtest_data.py` で更新

---

## 進行判断の最終チェック (Day 14 以降)

集計を 14 日分まとめて取得:
```bash
python scripts/aggregate.py --days 14 > /tmp/primary.txt
python scripts/aggregate.py --days 14 --state-dir ./data/compare_old > /tmp/compare.txt
```

クライテリア表に値を記入し、すべて ☑ なら Phase 3 進行を人間判断で承認。
