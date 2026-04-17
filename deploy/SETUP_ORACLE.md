# Oracle Cloud Always Free で dry-run を24時間稼働させる手順

**対象**: 持ち歩く PC ではなく、クラウド上で bot を常時稼働させたい人。
**コスト**: 永年無料（Always Free 枠内）。
**所要時間**: アカウント作成 30〜60分、VM 作成 10分、セットアップ自動化 5分。

---

## あなたがやる部分（自動化不可）

### 1. Oracle Cloud アカウント作成

1. <https://www.oracle.com/jp/cloud/free/> にアクセス → 「無料で始める」
2. メールアドレス・国（Japan）・個人情報を入力
3. **クレジットカード登録**（本人確認用。Always Free 枠のみ使えば課金されない）
4. 電話番号認証（SMS）
5. ホームリージョン選択（**Tokyo 推奨**。一度決めたら変更不可）
6. 登録完了までメール確認まで含めて 30分〜数時間

> ⚠️ 住所・氏名はローマ字で正確に。審査落ちの主因は住所表記ミス。

### 2. VM (Compute Instance) 作成

Oracle Cloud コンソールで:

1. 左上ハンバーガー → **Compute** → **Instances** → **Create instance**
2. 設定:
   - **Name**: `gmo-bot-safe`
   - **Image**: `Canonical Ubuntu 24.04`（または 22.04）
   - **Shape**: **Change shape** → **Ampere** → `VM.Standard.A1.Flex`
     - OCPU: **2**、Memory: **6 GB**（Always Free 枠内、dry-run には余裕）
   - **Networking**: デフォルトで OK（パブリック IP あり）
   - **SSH keys**:
     - **Generate a key pair for me** を選び **秘密鍵（.key ファイル）を必ずダウンロード**
     - もしくは既存の公開鍵（`~/.ssh/id_ed25519.pub`）を貼り付け
3. **Create** をクリック → 起動まで 1〜2分

### 3. SSH 接続

Windows の PowerShell で:

```powershell
# ダウンロードした秘密鍵の権限を絞る（初回のみ）
icacls C:\path\to\ssh-key.key /inheritance:r /grant:r "$env:USERNAME:R"

# 接続
ssh -i C:\path\to\ssh-key.key ubuntu@<VMのパブリックIP>
```

パブリック IP は VM 詳細ページに表示されています。

> ⚠️ 初回は `ubuntu` ユーザーが既定。`pinkichi` など独自ユーザーを使いたければ後で作成。

---

## ここから自動（1コマンドで完了）

SSH でつないだ直後に、以下を貼り付けるだけ:

```bash
curl -fsSL https://raw.githubusercontent.com/pinkichi811-del/gmo-bot-safe/master/deploy/bootstrap.sh | bash
```

これで以下が全部自動で走ります:

- apt update & 依存パッケージ (`python3-venv`, `git`, `logrotate`) インストール
- `~/gmo-bot-safe` に git clone
- Python venv 作成 + `pip install -r requirements.txt`
- `.env` を `.env.example` から生成（dry_run 固定、API キーはダミーのまま）
- systemd サービス登録 + 起動（`gmo-bot-safe.service`）
- logrotate 登録
- 稼働確認表示

再実行しても壊れない作りにしてあります。

---

## 稼働確認

```bash
# 状態まとめ
bash ~/gmo-bot-safe/deploy/status.sh

# ライブログ追従
journalctl -u gmo-bot-safe -f

# 今日の集計
cd ~/gmo-bot-safe && .venv/bin/python scripts/aggregate.py
```

## 更新（コードを変えた時）

ローカルで `git push` した後、VPS で:

```bash
bash ~/gmo-bot-safe/deploy/update.sh
```

## 停止

```bash
# ソフト停止（新規買いだけ止める、保有は evaluate 継続）
touch ~/gmo-bot-safe/STOP

# サービス停止
sudo systemctl stop gmo-bot-safe

# 再開
rm ~/gmo-bot-safe/STOP
sudo systemctl start gmo-bot-safe
```

---

## トラブル時

| 症状 | 確認 |
| --- | --- |
| サービスが failed | `journalctl -u gmo-bot-safe -n 100 --no-pager` |
| HALT した | `cat ~/gmo-bot-safe/data/state.json` で `"halt": true` 確認 → ログで原因特定 → 人間の手で `halt: false` に戻す |
| ディスク不足 | `df -h` / logrotate が効いているか確認 |
| SSH 切れた | Oracle コンソールから VM 再起動 |

## セキュリティメモ（dry-run でも守る）

- `.env` は `chmod 600`（bootstrap で自動）
- API キーはダミーのまま（live 実装まで実キーを置かない）
- SSH は鍵認証のみ。パスワード認証は無効化推奨:
  ```bash
  sudo sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
  sudo systemctl restart ssh
  ```
- ファイアウォールは Oracle の Security List で管理（SSH 22 のみ許可）
