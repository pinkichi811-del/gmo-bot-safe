# Oracle Cloud セットアップ手順 — gmo-bot-safe Phase 2 観察

PC スリープによる中断を避けるため、Phase 2 dry-run 観察を Oracle Cloud Always Free
の Linux VM 上で 24/365 稼働させる手順。Phase 3 以降の read-only API 疎通でも
そのまま使える基盤になる。

このセットアップでは:
- 本命 (balanced_ndx champion, BTC+ETH + NDX trend filter)
- 並走 (旧 champion, 5min trend>=5 ma=5/20 BTC 単独)
- NDX/SPX/VIX 日次更新 (Yahoo Finance v8 chart API)

の 3 つを systemd で常駐させる。

---

## 前提条件

- Oracle Cloud アカウント (作成時に支払い情報登録が必要だが Always Free は課金されない)
- ローカル PC に SSH 鍵ペア
- このリポが GitHub 等にプッシュされていること

---

## Step 1 — VM プロビジョニング (Oracle Cloud コンソール)

1. Oracle Cloud にサインイン
2. **Compute → Instances → Create Instance**
3. 設定:
   - **Name**: `gmo-bot-safe`
   - **Image**: Canonical Ubuntu 22.04 (または 24.04)
   - **Shape**: 以下のどちらか
     - **VM.Standard.A1.Flex** (ARM Ampere, Always Free, 4 vCPU / 24GB まで)
       - 空きが無い時期がある
     - **VM.Standard.E2.1.Micro** (AMD x86, Always Free, 1 vCPU / 1GB)
       - 常に空いている。この bot は超軽量なので十分
   - **Networking**: デフォルト VCN を作成 / Public IP 有効
   - **SSH キー**: ローカルの公開鍵 (`~/.ssh/id_ed25519.pub` 等) を貼り付け
4. **Create** → 起動完了 (1〜3 分) → Public IP を控える

### Ingress ルール

デフォルトで 22 (SSH) のみ開いている。bot は外向き HTTPS 接続のみなので追加開放は不要。

---

## Step 2 — SSH 接続と基本セットアップ

```bash
# ローカルから
ssh ubuntu@<PUBLIC_IP>
```

VM 上で:

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip git
python3 --version    # 3.10 以上であることを確認
```

Ubuntu 22.04 LTS は Python 3.10、24.04 LTS は 3.12 が標準。どちらも要件を満たす。

---

## Step 3 — リポ取得 + venv

```bash
cd $HOME
git clone <your-repo-url> gmo-bot-safe
cd gmo-bot-safe
python3 -m venv .venv
.venv/bin/pip install -U pip
.venv/bin/pip install -r requirements.txt
```

依存は `PyYAML>=6.0` のみ。

---

## Step 4 — `.env` 作成

```bash
cp .env.example .env
$EDITOR .env
```

最低限以下を確認:

```ini
RUN_MODE=dry_run
CONFIG_PATH=./config/strategies/btc_eth_balanced_ndx.yaml
LIVE_OK=no
CONFIRM_LIVE=no
```

Phase 2 では live キーは不要なので `GMO_API_KEY` / `GMO_API_SECRET` は空のまま。

> Hard Rule (CLAUDE.md #2): `.env` は絶対にコミットしない。`.env.example` のみ git 管理。

---

## Step 5 — NDX/SPX/VIX CSV 初回取得

regime filter の入力ファイルを用意する。

```bash
.venv/bin/python scripts/fetch_index_daily.py --indices NDX,SPX,VIX
```

成功すると以下のように表示される:

```
[fetch_index] NDX: wrote 10227 bars to data/market/NDX_d.csv (was (none) -> now 2026-05-05)
[fetch_index] SPX: wrote 14206 bars to data/market/SPX_d.csv (was (none) -> now 2026-05-05)
[fetch_index] VIX: wrote 9152 bars to data/market/VIX_d.csv (was (none) -> now 2026-05-05)
```

---

## Step 6 — テストで動作確認

```bash
.venv/bin/python -m unittest discover -s tests -p "test_*.py"
```

`Ran 88 tests in ... OK` を確認。

---

## Step 7 — systemd unit 配置

unit ファイルの `USER` 部分を実ユーザー名 (Oracle Cloud デフォルトは `ubuntu`) に
置換してから `/etc/systemd/system/` にコピーする。

```bash
USER_NAME=$(whoami)
sudo install -m 644 deploy/gmo-bot-safe-primary.service       /etc/systemd/system/
sudo install -m 644 deploy/gmo-bot-safe-compare-old.service   /etc/systemd/system/
sudo install -m 644 deploy/fetch-index-daily.service          /etc/systemd/system/
sudo install -m 644 deploy/fetch-index-daily.timer            /etc/systemd/system/

# USER → 実ユーザー名 に書き換え
sudo sed -i "s|/home/USER/|/home/${USER_NAME}/|g; s|^User=USER|User=${USER_NAME}|; s|^Group=USER|Group=${USER_NAME}|" \
  /etc/systemd/system/gmo-bot-safe-primary.service \
  /etc/systemd/system/gmo-bot-safe-compare-old.service \
  /etc/systemd/system/fetch-index-daily.service

sudo systemctl daemon-reload
```

---

## Step 8 — 起動

```bash
# bot 2 本を起動・enable (再起動後も自動起動)
sudo systemctl enable --now gmo-bot-safe-primary.service
sudo systemctl enable --now gmo-bot-safe-compare-old.service

# NDX 日次更新 timer を起動・enable (毎日 22:00 UTC)
sudo systemctl enable --now fetch-index-daily.timer

# 状態確認
systemctl status gmo-bot-safe-primary.service gmo-bot-safe-compare-old.service
systemctl list-timers fetch-index-daily.timer
```

---

## Step 9 — 動作確認 (起動から 5〜10 分後)

```bash
# 1 サイクル目が回ったか
cat ~/gmo-bot-safe/data/state.json
cat ~/gmo-bot-safe/data/compare_old/state.json
# → halted=false, last_score_ts が最新であること

# ログ
journalctl -u gmo-bot-safe-primary.service --since "10 min ago" | tail -30
journalctl -u gmo-bot-safe-compare-old.service --since "10 min ago" | tail -30

# Phase 2 観察集計 (UTC 基準)
.venv/bin/python scripts/aggregate.py --date $(date -u +%F)
.venv/bin/python scripts/aggregate.py --date $(date -u +%F) --state-dir ./data/compare_old
```

---

## Step 10 — 観察期間中の運用 (Day 1 〜 Day 14)

```bash
# 数日おきに集計 (UTC)
.venv/bin/python scripts/aggregate.py --days 7
.venv/bin/python scripts/aggregate.py --days 7 --state-dir ./data/compare_old
```

[docs/PHASE_2_OBSERVATION.md](PHASE_2_OBSERVATION.md) の「日次観察ログ」に記入。

### 監視ポイント

| 項目 | 確認方法 | 対応 |
|---|---|---|
| HALT 発火 | `cat data/state.json` の `halted` | 即 `touch STOP` (2 つの STATE_DIR で) |
| `cash_min < 0.20` | `aggregate.py` の cash_ratio セクション | 仕様調査、Phase 1 戻り判断 |
| regime block 100% 連続 | `aggregate.py` の regime gate セクション | NDX CSV の鮮度を確認 (`tail data/market/NDX_d.csv`) |
| timer ジョブ失敗 | `systemctl status fetch-index-daily.timer` | journalctl で原因確認 |

---

## Step 11 — 進行判断 (Day 14 = 2026-05-18 以降)

```bash
.venv/bin/python scripts/aggregate.py --days 14 > /tmp/primary.txt
.venv/bin/python scripts/aggregate.py --days 14 --state-dir ./data/compare_old > /tmp/compare.txt
```

[docs/PHASE_2_OBSERVATION.md](PHASE_2_OBSERVATION.md) のクライテリア表に値を入れて判断。

---

## 停止・再起動

```bash
# 一時停止
sudo systemctl stop gmo-bot-safe-primary.service gmo-bot-safe-compare-old.service

# 完全停止 (再起動後も起動しない)
sudo systemctl disable --now gmo-bot-safe-primary.service gmo-bot-safe-compare-old.service

# 再開
sudo systemctl start gmo-bot-safe-primary.service gmo-bot-safe-compare-old.service

# HALT 復帰 (CLAUDE.md Hard Rule #5)
# 1. 原因をログで特定
# 2. data/state.json (or data/compare_old/state.json) の "halted" を false に手動編集
# 3. STOP ファイルがあれば削除
# 4. systemctl restart
```

---

## トラブルシューティング

### ログ場所

| ログ | パス |
|---|---|
| primary bot | `journalctl -u gmo-bot-safe-primary.service` または `~/gmo-bot-safe/logs/bot.log` |
| compare-old bot | `journalctl -u gmo-bot-safe-compare-old.service` または `~/gmo-bot-safe/logs/compare_old/bot.log` |
| NDX timer | `journalctl -u fetch-index-daily.service` |

### Yahoo Finance がブロックされた場合

非公式 API のため、Oracle Cloud の IP からレート制限がかかる可能性がある。
`fetch_index_daily.py` のエラーが連続する場合は:

1. `journalctl -u fetch-index-daily.service` で原因確認
2. 一時的にローカル PC で `python scripts/fetch_index_daily.py` を実行
3. `scp data/market/*.csv ubuntu@<PUBLIC_IP>:~/gmo-bot-safe/data/market/`

### ARM (A1) で Python パッケージビルドが失敗する場合

`PyYAML` は wheel 提供あり、ビルド不要。万一 source build 要求が出たら:

```bash
sudo apt-get install -y build-essential libyaml-dev
```

---

## live 発注 (Phase 5 以降) は別手順

Phase 5 の三段ゲート解除はこの手順書の対象外。CLAUDE.md Hard Rule #1 と
[ROADMAP.md](../ROADMAP.md) Phase 5 を参照。
