# Phase 3 — GMOコイン API キー発行と疎通確認

Phase 3 は read-only API の疎通確認まで含む。コード側 (GmoApiClient / GmoMarketDataSource / SecretMaskFilter) は実装済みだが、**実際の API キー発行と疎通テストは人間の手動作業**。本書はその手順書。

> **Hard Rule**: ここで発行するキーは **read-only**。注文権限を絶対に付けない。
> CLAUDE.md `## 絶対に守ること (Hard Rules)` を満たす運用上の最後の防衛線。

---

## 1. GMOコイン側: API キーの発行

### 1.1 ログインと API メニュー

1. GMOコイン現物 Web 版 (`https://coin.z.com/jp/`) にログイン
2. 上部メニュー「**会員ホーム**」 → 左メニュー「**API**」を開く
3. 「**API設定**」 → 「**API追加**」

### 1.2 権限設定 — read-only に絞る

権限チェックリストでは以下のみを ON にする:

| 権限 | 設定 | 備考 |
|---|---|---|
| **資産情報の参照** | **ON** | `/v1/account/assets` に必要 |
| 注文情報の参照 | OFF | Phase 3 スコープ外 |
| 注文 | **OFF** | **絶対に ON にしない** (Hard Rule) |
| 約定情報の参照 | OFF | Phase 4 で扱う |
| 入出庫の申請 | **OFF** | **絶対に ON にしない** |
| その他（レバ・FX 関連） | OFF | 現物 bot なので不要 |

> Phase 4-5 で実注文を解禁する段になったら、その時点で **新しいキーを別途発行する**。
> Phase 3 用の read-only キーに後から権限を足さないこと。

### 1.3 IP 制限を必ず設定

「IP アドレス制限」欄に Oracle Cloud VM のグローバル IP を入力:

```
217.142.240.244
```

> 経路は ssh alias `gmo` で接続するあの VM。Always Free Tier は Public IP が固定されている前提だが、再起動・リサイズ後に変わったら API 401 が出る → キーの IP 制限を更新する。

### 1.4 2 段階認証で確定

- 登録メール宛の確認コードを入力
- もしくは Google Authenticator の TOTP

### 1.5 キーとシークレットを控える

確定すると画面に **API キー** と **API シークレット** が表示される。

- API キー: 後からでも確認できる
- **API シークレット**: **この画面でしか表示されない**。閉じると二度と見えない

控え方:
- 1Password / Bitwarden 等のパスワードマネージャに保存
- 紙にも書いておく (片方が壊れた時の保険)
- **絶対にコミットしない / Slack に貼らない / メールに書かない**

---

## 2. Oracle Cloud VM 側: `.env` に書き込む

### 2.1 既存 `.env` の確認

VM にログイン後、現状を確認:

```bash
ssh gmo
cd ~/gmo-bot-safe
grep -n "GMO_API" .env
```

`.env.example` から派生していれば `GMO_API_KEY=your_api_key_here` のダミー行があるはず。

### 2.2 値を上書き

エディタで `.env` を編集 (`vim .env` 等):

```ini
GMO_API_KEY=<発行されたキー>
GMO_API_SECRET=<発行されたシークレット>
```

> **注意**: `.env` は `.gitignore` 済みだが、念のため `git status` で出ないことを確認する。

### 2.3 ファイルパーミッション

`.env` は本人以外読めない設定に:

```bash
chmod 600 ~/gmo-bot-safe/.env
ls -l ~/gmo-bot-safe/.env   # -rw------- になっていること
```

---

## 3. 疎通確認 — `probe_gmo_assets.py`

### 3.1 スクリプト実行

```bash
ssh gmo
cd ~/gmo-bot-safe
.venv/bin/python scripts/probe_gmo_assets.py
```

### 3.2 成功時の出力例

```
[INFO] client = GmoApiClient(public_base='https://api.coin.z.com/public', has_credentials=True)
[INFO] requesting GET /private/v1/account/assets ...
[OK] response received.
[INFO] 3 asset rows:
  {"asset":"JPY","amount":"100000","available":"100000","conversionRate":"1","symbol":"JPY"}
  {"asset":"BTC","amount":"0.0001","available":"0.0001","conversionRate":"10000000","symbol":"BTC"}
  {"asset":"ETH","amount":"0","available":"0","conversionRate":"500000","symbol":"ETH"}
```

### 3.3 よくあるエラー

| エラー | 原因 | 対処 |
|---|---|---|
| `env not configured` | `.env` にキーが入っていない | `.env` を再確認、`chmod 600` |
| `API error status=401` | 署名失敗 / IP 制限不一致 | VM の Public IP を確認、GMO 側の IP 制限を更新 |
| `API error status=5` | API キー無効 / 失効 | GMOコイン側で再発行 |
| `unexpected error: ...` | ネットワーク / DNS | `curl https://api.coin.z.com/public/v1/status` で疎通確認 |

---

## 4. 完了条件 (ROADMAP Phase 3 の `完了基準`)

- [ ] 実環境で残高取得レスポンスが取れる (`probe_gmo_assets.py` が `[OK]` で終了)
- [ ] `bot.log` に API キー / シークレットが一切出ない (`grep <キー先頭文字>` で確認)
- [ ] 署名失敗 / nonce 衝突のテストが通る (`python -m unittest tests.test_gmo_api_client`)

3 つ揃ったら ROADMAP の `## 現在地` を Phase 4 に進める。

---

## 5. Phase 3 完了後の運用

Phase 3 完了後、Phase 4 (live 注文コード実装) に進むまでの間:

- **キーを失効させない** (Phase 4 の動作確認でも assets 取得は使う)
- **キーの権限を変えない** (Phase 4 で実注文を出す段になったら、上記の通り **新しいキーを別途発行**)
- 定期的に GMO Web の「APIアクセスログ」を見て、想定外の IP からの呼び出しが無いか確認

---

## 6. キー漏洩時の対応

万一 `.env` を間違って push した、ログに出した、SSH キーが盗まれた等の事故時:

1. **GMOコイン Web に即ログイン → API メニュー → 該当キーを「無効化」**
2. 新しいキーを発行 (上記手順を最初から)
3. 漏洩経路を特定 (git history なら `git filter-branch` で除去、ログなら全保管先から削除)
4. CLAUDE.md `## 失敗ログ` に経緯を記録 (再発防止のため)

> 漏洩判定に迷ったら**即無効化**してから考える。残高参照のキーが他人に渡っても直接の金銭被害は出ないが、注文権限付きのキーが漏れたら口座が空になる。Phase 3 のキーは read-only でもこの習慣を徹底する。
