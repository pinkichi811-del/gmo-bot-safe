# deploy/

VPS 配置用の雛形とスクリプト。

## 🚀 Oracle Cloud Always Free で動かす場合

**最短手順は [`SETUP_ORACLE.md`](./SETUP_ORACLE.md) 参照。**

- VPS に SSH 接続後、以下1コマンドで全自動セットアップ:
  ```bash
  curl -fsSL https://raw.githubusercontent.com/pinkichi811-del/gmo-bot-safe/master/deploy/bootstrap.sh | bash
  ```

## ファイル一覧

| ファイル | 用途 |
| --- | --- |
| `SETUP_ORACLE.md` | Oracle Cloud 側の手動手順（アカウント作成〜VM作成〜SSH接続） |
| `bootstrap.sh` | VPS 初回セットアップ（clone / venv / systemd / logrotate 全自動） |
| `update.sh` | 更新（`git pull` + `systemctl restart`） |
| `status.sh` | 稼働状況まとめ（systemd / STOP / ログ / state.json） |
| `gmo-bot-safe.service` | systemd ユニットファイル（`bootstrap.sh` が USER を置換して展開） |
| `logrotate.conf` | logrotate 設定（同上） |

## 手動セットアップ（bootstrap.sh を使わない場合）

USER 部分は実アカウント名に置換すること。

## systemd

```bash
sudo cp deploy/gmo-bot-safe.service /etc/systemd/system/gmo-bot-safe.service
sudo sed -i 's/USER/your-username/g' /etc/systemd/system/gmo-bot-safe.service
sudo systemctl daemon-reload
sudo systemctl enable --now gmo-bot-safe
journalctl -u gmo-bot-safe -f
```

## logrotate

```bash
sudo cp deploy/logrotate.conf /etc/logrotate.d/gmo-bot-safe
sudo sed -i 's/USER/your-username/g' /etc/logrotate.d/gmo-bot-safe
sudo logrotate -d /etc/logrotate.d/gmo-bot-safe   # dry-run で検証
```

## 初回 setup 手順

```bash
# 1. clone
git clone <repo> /home/USER/gmo-bot-safe
cd /home/USER/gmo-bot-safe

# 2. venv
python -m venv .venv
.venv/bin/pip install -r requirements.txt

# 3. 設定
cp .env.example .env
# ※ RUN_MODE=dry_run のまま、LIVE_OK=no / CONFIRM_LIVE=no を維持

# 4. ディレクトリ権限
mkdir -p data logs
chmod 700 data logs .env

# 5. 起動
sudo systemctl enable --now gmo-bot-safe
```

## 稼働確認

```bash
# 現在のサイクル状況
bash scripts/show_status.sh

# 今日の集計
python scripts/aggregate.py

# 直近7日
python scripts/aggregate.py --days 7

# ログ末尾
journalctl -u gmo-bot-safe -n 100 --no-pager
# または
tail -f logs/bot.log
```
