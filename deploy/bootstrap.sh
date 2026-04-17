#!/usr/bin/env bash
# =============================================================================
# gmo-bot-safe — VPS 初回セットアップ（ワンショット）
# -----------------------------------------------------------------------------
# 前提: Ubuntu 22.04 / 24.04 LTS、sudo 可能なユーザーでログイン済み。
# 使い方:
#   curl -fsSL https://raw.githubusercontent.com/pinkichi811-del/gmo-bot-safe/master/deploy/bootstrap.sh | bash
# もしくは clone 後:
#   bash deploy/bootstrap.sh
# -----------------------------------------------------------------------------
# 挙動:
#   1. 必要パッケージ (python3-venv, git, logrotate) を apt install
#   2. ~/gmo-bot-safe を git clone（既にあれば pull）
#   3. venv 作成 + pip install -r requirements.txt
#   4. .env を .env.example から作成（dry_run 固定）
#   5. systemd サービス登録 + 起動
#   6. logrotate 登録
#   7. 稼働確認を表示
# -----------------------------------------------------------------------------
# 冪等: 再実行しても壊れない。既存 .env は上書きしない。
# =============================================================================

set -euo pipefail

REPO_URL="https://github.com/pinkichi811-del/gmo-bot-safe.git"
REPO_DIR="$HOME/gmo-bot-safe"
SERVICE_NAME="gmo-bot-safe"
USER_NAME="$(whoami)"

log() { printf "\033[1;36m[bootstrap]\033[0m %s\n" "$*"; }
warn() { printf "\033[1;33m[bootstrap]\033[0m %s\n" "$*" >&2; }

# --- 1. 依存パッケージ -------------------------------------------------------
log "apt update & install (python3-venv, git, logrotate) ..."
sudo DEBIAN_FRONTEND=noninteractive apt-get update -y
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y \
    python3 python3-venv python3-pip git logrotate ca-certificates curl

# --- 2. clone or pull --------------------------------------------------------
if [ -d "$REPO_DIR/.git" ]; then
    log "既存リポジトリを更新: $REPO_DIR"
    git -C "$REPO_DIR" fetch --all --prune
    git -C "$REPO_DIR" pull --ff-only
else
    log "clone: $REPO_URL -> $REPO_DIR"
    git clone "$REPO_URL" "$REPO_DIR"
fi

cd "$REPO_DIR"

# --- 3. venv + deps ----------------------------------------------------------
if [ ! -d ".venv" ]; then
    log "venv 作成"
    python3 -m venv .venv
fi
log "pip install -r requirements.txt"
.venv/bin/pip install --upgrade pip >/dev/null
.venv/bin/pip install -r requirements.txt

# --- 4. .env ------------------------------------------------------------------
if [ ! -f ".env" ]; then
    log ".env を .env.example から作成（dry_run 固定）"
    cp .env.example .env
else
    log ".env 既存のため保持"
fi
chmod 600 .env

# --- 5. データ・ログ ディレクトリ --------------------------------------------
mkdir -p data logs
chmod 700 data logs

# --- 6. systemd --------------------------------------------------------------
SERVICE_SRC="$REPO_DIR/deploy/gmo-bot-safe.service"
SERVICE_DST="/etc/systemd/system/${SERVICE_NAME}.service"
log "systemd service を展開: $SERVICE_DST"
sudo cp "$SERVICE_SRC" "$SERVICE_DST"
sudo sed -i "s|/home/USER|/home/${USER_NAME}|g; s|User=USER|User=${USER_NAME}|g; s|Group=USER|Group=${USER_NAME}|g" "$SERVICE_DST"

sudo systemctl daemon-reload
sudo systemctl enable "${SERVICE_NAME}" >/dev/null
sudo systemctl restart "${SERVICE_NAME}"

# --- 7. logrotate ------------------------------------------------------------
LOGROTATE_SRC="$REPO_DIR/deploy/logrotate.conf"
LOGROTATE_DST="/etc/logrotate.d/${SERVICE_NAME}"
log "logrotate を展開: $LOGROTATE_DST"
sudo cp "$LOGROTATE_SRC" "$LOGROTATE_DST"
sudo sed -i "s|/home/USER|/home/${USER_NAME}|g" "$LOGROTATE_DST"

# --- 8. 稼働確認 -------------------------------------------------------------
log "セットアップ完了。稼働状況:"
echo "----------------------------------------"
sudo systemctl status "${SERVICE_NAME}" --no-pager -l | head -n 15 || true
echo "----------------------------------------"
log "直近ログ:   journalctl -u ${SERVICE_NAME} -n 50 --no-pager"
log "追従:       journalctl -u ${SERVICE_NAME} -f"
log "更新:       bash $REPO_DIR/deploy/update.sh"
log "状態確認:   bash $REPO_DIR/deploy/status.sh"
