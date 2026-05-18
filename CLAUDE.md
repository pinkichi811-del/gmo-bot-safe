# CLAUDE.md

このリポジトリで作業する AI アシスタント（Claude など）向けの指針。
人間の開発者は `README.md` と `DESIGN.md` を参照すること。

---

## プロジェクトの目的

GMOコイン向けの暗号資産現物自動売買bot。
**安全寄りの構造を先に固めること**が現段階のゴール。live 発注はまだ実装しない。

---

## 絶対に守ること（Hard Rules）

1. **live 発注コードを勝手に有効化しない**
   - `src/order_executor.py` の `ENABLE_LIVE_ORDER = False` を **True に書き換えない**（コードゲート）。
   - `_send_live_order()` の `NotImplementedError` を実装に差し替えない（実装ゲート）。
   - 実注文 API を呼び出すコードを追加する場合は、必ず人間の明示的な指示を得ること。
   - `scripts/run_live.sh` は `CONFIRM_LIVE=yes` かつ `LIVE_OK=yes` 無しでは起動しない設計。三段ゲートを外さない。
   - live を有効化する条件は `DESIGN.md` を読んでから。

2. **API キー・秘密情報をコード・ログ・コミットに出さない**
   - `.env` はコミットしない（`.gitignore` 済み）。
   - `.env.example` にはダミー値のみ。
   - ログ出力時にシークレットをマスクすること。

3. **発注可否の最終判断はルール（`risk_guard.py`）が握る**
   - AI のスコアはあくまで補助。AI スコア単体で注文を発火させない。
   - `scorer.py` は優先度付けのみ。発注可否の判定はしない。

4. **制約値を勝手に緩めない**（人間の指示がある時のみ変更）
   | 項目 | 値 |
   | --- | --- |
   | 監視銘柄数 | ≤ 5 |
   | 同時保有数 | ≤ 3 |
   | 最低現金比率 | ≥ 20% |
   | core 1銘柄あたり最大 | 35% |
   | satellite 1銘柄あたり最大 | 25% |
   | 1サイクル最大注文 | 2 |
   | 1注文あたり最大JPY | `risk.per_trade_jpy_max` |

5. **異常時は HALT。自動再開しない。**
   - 連続 `max_consecutive_errors` 回のエラー、想定外レスポンス、価格乖離 `halt_on_price_gap_pct` 超過で HALT。
   - HALT からの復帰は人間の手による `state.json` 編集 or `STOP` ファイル削除 + 再起動。

6. **`STOP` ファイル尊重**
   - ルート直下に `STOP` が存在する間、新規買いを出さない。保有の損切り・利確は通常通り評価。

---

## 買い／売りの判定ルール（要約）

判定の詳細は `DESIGN.md` 参照。実装時はここと齟齬が無いか確認すること。

### 買い候補（`buy_candidate`）
すべて満たす場合のみ候補入り:
- TotalScore ≥ 70
- Trend ≥ 18
- Liquidity ≥ 10
- Heat ≥ -8

### 強い買い候補（`strong_buy`）
優先度が上がる:
- TotalScore ≥ 78
- Trend ≥ 22
- Liquidity ≥ 12
- Heat ≥ -5

### 見送り
- DupPenalty ≤ -8 なら基本見送り（重複保有リスク）

### エグジット候補
- 損切り: 建値から -4%
- 利確: 建値から +6%

### クールダウン
- 同一銘柄の再エントリーは 180 分経過後

### 新規買いのブロック
- 約定後に現金比率 < 20% になる注文は出さない
- core 1銘柄が 35% を超える注文は出さない
- satellite 1銘柄が 25% を超える注文は出さない
- `STOP` ファイルが存在すれば全て停止

---

## 設計方針

- **YAGNI**: 最小構成を維持する。仮定の未来要件のための抽象化を足さない。
- **Fail-safe**: 判断に迷ったら「止める」を選ぶ。
- **分離**: market_watcher（観測） / scorer（評価） / risk_guard（判断） / order_executor（実行）。順序を跨がない。
- **明示的な状態**: HALT・保有・クールダウンは `state_store.py` 経由で永続化する。
- **テスト可能性**: 外部 API 呼び出しは差し替え可能な形で書く（モックできること）。

---

## モジュール責務

| モジュール | 役割 | 注意 |
| --- | --- | --- |
| `main.py` | エントリポイント・ループ制御 | ビジネスロジックを書かない |
| `market_watcher.py` | 価格・板情報の取得 | 5分周期想定 |
| `scorer.py` | 指標＋AI補助によるスコア算出 | 発注権限なし |
| `risk_guard.py` | 発注可否判定・HALT 管理 | すべての注文はここを通る |
| `order_executor.py` | 発注実行（現状 dry-run のみ） | live は未実装 |
| `notifier.py` | 通知 | HALT・エラー・約定 |
| `state_store.py` | 保有状態・フラグの永続化 | `data/state.json` |

---

## 作業時のヒント

- 実装を追加する前に既存モジュールの責務に収まるか確認する。
- 設定値の変更はまず `config/app.yaml` で完結するようにする（マジックナンバーを散らさない）。
- 新しい外部 API 呼び出しは必ずモック可能な形で書く。
- ログにはシンボル・スコア・判定理由を含める。約定・HALT は WARN 以上。
- テストは `tests/` に入れる（未作成）。

---

## 失敗ログ

### 2026-05-17: logrotate が日付付き jsonl を二重 rotate して aggregate を狂わせた

**現象**: Phase 2 観察の進行判断を出そうとして `python3 scripts/aggregate.py --days 13`
を叩いたところ、`100% regime_blocked:ndx_trend / 0 trades / 0 PnL` という嘘の結果が
出た。これだけ見ると「regime gate が 13 日間ずっと block しっぱなしで観察不能」と
誤判断し、Phase 1 戻りや観察リセットを検討するハメになるところだった。

**真の状況**: bot 自体は健全稼働中 (cycles 286/日 = 5分周期完全、PF 45 / 月 trades 394
の偽利益が出るほど entry/exit 連発、regime block 率は妥当な 11.3%)。直近 cycle の
生 JSON に `regime: {allow_buy: true}` と明記されていたため矛盾に気付いた。

**根本原因**: `deploy/logrotate.conf` が `data/score_log/*.jsonl` を `daily / rotate 60`
で rotate していた。アプリ側は `<YYYY-MM-DD>.jsonl` 形式の日付ファイル名を使う設計
だが、logrotate はこれもさらに rotate して `<date>.jsonl.1` にリネームする。
`aggregate.py` の glob は `<date>.jsonl` のみを拾うため `.jsonl.1` の 5/5〜5/15 が
集計対象から漏れ、残った 5/16〜5/17 の 2 ファイル分しか拾えていなかった。
compare_old 側は logrotate のパスマッチ (`data/score_log/`) から外れていたため無傷で、
これが「同じ aggregate で並走側だけ正常」という非対称を生み、原因切り分けを難しくした。

**修正**:
- VM 上で `data/score_log/*.jsonl.1` を `*.jsonl` にリネームして集計対象を救出
- `/etc/logrotate.d/gmo-bot-safe` を `.disabled` にリネームして一旦無効化
- `deploy/logrotate.conf` を修正、score_log は logrotate 対象から外す
  (理由のコメントを設定ファイルに明記して同じ事故を防ぐ)

**教訓**:
- aggregate の "想定外の集計結果" を見たら、aggregate のデータソース (どのファイル
  glob を読んでいるか) と実際のファイル名を必ず突き合わせる。"bot が壊れた" と思う前に
  "集計が壊れた" を疑う。
- 直近 cycle の生 JSON が真実、aggregate のサマリは加工結果。**矛盾したら生 JSON が勝つ**。
- 日付ファイル名のローテート設計は logrotate と二重に当てるな。
  (アプリ側 TimedRotatingFileHandler などで日付付きファイルを作るときは logrotate 不要)

### 2026-05-18: API シークレットがチャット履歴に露出 (Phase 3 疎通確認時)

**現象**: Phase 3 read-only API キーの疎通確認手順で、AI assistant 側が以下 2 行を
1 つのコードブロックで提示した:

```
grep -E "^GMO_API" .env    # 値が入っていることを目視（本人だけ見える）
.venv/bin/python scripts/probe_gmo_assets.py
```

ユーザーは両方を実行し、ターミナル出力をそのまま Claude Code チャットにペースト
した。結果として `GMO_API_KEY` / `GMO_API_SECRET` の実値が assistant のコンテキスト
と Anthropic 側ログに記録された。

**真の状況**: 漏洩したのは read-only スコープ (資産残高を取得のみ) のキー。注文・
入出庫不可、直接の金銭被害ゼロ。だが CLAUDE.md Hard Rule #2「秘密情報をログ・
コミットに出さない」「漏洩判定に迷ったら即無効化してから考える」に違反。

**修正**:
- GMO Web で旧キー (yukichibot) を即時無効化
- 新キー (yukichibot2) を同権限・同 IP 制限で発行
- VM の `.env` を新値に差し替え
- probe スクリプトを再実行、`[OK]` の有無だけをチャットで報告 (出力は貼らない)

**根本原因**: AI assistant 側の手順設計ミス。
- 「目視」「本人だけ見える」というコメントは ターミナル上の話。ユーザーが履歴ごと
  ペーストすると当然チャットに乗る。コメントによる注意書きは防衛線として弱い。
- 機密値を表示するコマンド (`grep ^GMO_API`) と、貼って共有することが自然な出力を
  出すコマンド (`probe スクリプト`) を**同じブロックに置いてはいけない**。

**教訓**:
- 機密値の確認は「中身を表示する」のではなく「設定されているかだけ」を返す形に。
  例: `grep -c "^GMO_API_KEY=" .env` (件数のみ) や `[ -s .env ] && echo loaded` 等。
- AI 側が出すコマンドブロックは「ペーストされても困らない」ものに絞る。
  本人にしか見えてはいけない出力を出すコマンドは、別ブロックで明示的に分離し
  「**これは絶対にペーストしない**」と本文で警告する。
- probe スクリプト側で「キーが設定されているか」を返すフラグを足すことも検討
  (現状は from_env() の例外メッセージで間接確認するしかない)。
