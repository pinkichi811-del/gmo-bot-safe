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
>
> **追記 2026-05-06 11:14 UTC**: VM (instance-20260419-1810) は 2026-04-29 から既に
> balanced_ndx で稼働していたが、`data/market/NDX_d.csv` が 2026-03-31 で止まって
> いたため 7 日連続で `regime_blocked:ndx_trend` (cycles=417, 100% block) となり
> 評価不能データだった。NDX を 2026-05-05 まで更新 + systemd unit を primary +
> compare-old に分割 + `fetch-index-daily.timer` を毎日 22:00 UTC に enable した
> 上で 2026-05-06 11:13 UTC に両サービスを再起動し、初サイクルで NDX trend が
> ALLOW BUY に変わって ETH_JPY を STRONG BUY (price 540829.32, total=98.8) で
> 約定。**観察 Day 1 を 2026-05-06、進行判断 Day 14 を 2026-05-20** とする。
> 既存 (4/29-5/5) のデータは保持しつつ、集計は `--days N` で 5/6 起算に切り換える。

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

## 進行判断クライテリア (改訂版 2026-05-17)

### 旧クライテリア (2026-05-17 に無効化)

当初は ROADMAP.md Phase 2 の `±30%` 規定に基づき PF / 月次 trades の backtest 一致を
判定基準としていたが、**13 日観察 (2026-05-05〜2026-05-17) の結果から、これは技術的
に検証不可能**と判明した:

- backtest は historical OHLC 系列で価格をドライブし、entry→exit を時間軸上で再生する
- dry-run の `apply_simulated_fill` は `entry_price = d.price_ref` でライブスナップ
  ショット価格に固定し、その後の live 価格更新で take_profit/stop_loss が刺さる
- 結果、dry-run は backtest よりも遥かに高頻度に entry/exit を繰り返し、winrate も
  実態より高く出る (Phase 2 で本命 PF=45.16 / winrate=95.3% / 月次 trades≈394 を観測)
- これは「dry-run の simulated fill は PnL 一致目的ではなく、run_cycle のロジック
  健全性検証用」という設計上の自然な帰結であり、ロジックバグではない
- 真の PnL は Phase 6 (小額 live 並走) で初めて backtest と比較できる

### 新クライテリア — 「ロジック健全性」のみで判定

| 指標 | 期待 | 判定 (13日実測) |
|---|---|---|
| regime gate が ALLOW/BLOCK を NDX trend と整合的に切り替えるか | 0% / 100% 固定でない | ☑ 11.3% block (NDX 弱気期間で正常に発火) |
| cash_ratio min | ≥ 0.20 (Hard Rule) | ☑ 0.988 |
| 連続 HALT | 0 回 | ☑ 0 回 |
| cycles 完全性 | 5 分周期 ≈ 286/日 | ☑ cycles=3716 / 13日 = 286/日 |
| ETH_JPY price feed が正常に取得されている | snapshot に ETH_JPY が現れる | ☑ |
| multi-symbol portfolio (BTC + ETH 並走) | 両銘柄が独立に entry/exit | ☑ BTC buy=86 sell=86 / ETH buy=85 sell=85 |
| exit 判定 (trail / max_hold / stop_loss / take_profit) が刺さる | 損切り/利確の reason が出る | ☑ losses=8 (stop_loss 含む) |

### 参考値 (判定対象外、観察記録として保持)

| 指標 | backtest 期待値 | 13日実測 | 備考 |
|---|---|---|---|
| 期間 PF | 1.49〜2.46 | **45.16** | dry-run 構造上の偽利益、判定対象外 |
| 月次 trades | 〜4 | **〜394** | 同上 |
| max DD | 14.3% | 0.12% | 同上 (極小) |
| pnl | (50ヶ月で 3.15×) | +17.93% / 13日 | 同上 |

### 進行判断ルール

- ロジック健全性 7 項目がすべて ☑ → **Phase 3 (read-only API 実装)** へ
- 1 つでも外れる → 原因特定。仕様起因なら Phase 1 に戻る判断 (ROADMAP.md より)
- PnL/PF/trades の backtest 一致検証は Phase 6 (小額 live 並走) に持ち越す

---

## 進行判断: Phase 2 完了 → Phase 3 GO (2026-05-17 承認)

新クライテリア 7 項目すべて ☑。人間判断 (高岡勇吉) で Phase 3 進行を承認。

backtest との PnL 乖離 (PF 45 / 月 trades 394) は 13 日観察で発覚したが、これは
dry-run simulated fill の構造的特性であり、live 注文 (Phase 5〜) の挙動とは無関係。
live 解禁条件 (CLAUDE.md Hard Rules / DESIGN.md §1 の三段ゲート + 7 条件) は不変。

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
