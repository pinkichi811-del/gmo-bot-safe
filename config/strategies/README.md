# config/strategies/

CHAMPION から外れた設定 or 研究用の並走候補を保管する。

`config/app.yaml` は常に **現 CHAMPION** を指す。これを直接上書きせず、過去候補や研究用候補はこのディレクトリに別ファイルとして置く。

## 現在の候補

| ファイル | 用途 | 期待頻度 | 状態 |
| --- | --- | --- | --- |
| `../app.yaml` | **CHAMPION** 5min trend>=5 ma=5/20 | 月 10-20 回 | live-dry-run |
| `baseline_5min_trend3_ma3_10.yaml` | 旧 Pattern A（参考） | 月 15-17 回 | 保管 |
| `research_1h_trend5_ma5_20.yaml` | 1H 版（並走研究用） | 月 10 回前後 | 研究用 |

## 運用ルール

- CHAMPION の差し替えは `DESIGN.md` の「Champion History」セクションに理由を記録
- 差し替え後の旧 CHAMPION は必ずこのディレクトリに保管
- 研究用設定は live では使わない。バックテスト or 別の dry-run インスタンスでのみ

## 別設定での起動例

```bash
# 1H 研究候補を並走で確認したい時
CONFIG_PATH=./config/strategies/research_1h_trend5_ma5_20.yaml \
  STATE_DIR=./data/research_1h \
  LOG_DIR=./logs/research_1h \
  bash scripts/dry_run.sh
```

`STATE_DIR` と `LOG_DIR` を分けておかないと本命 bot の state と混ざるので注意。
