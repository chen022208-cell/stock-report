#!/usr/bin/env bash
# 盤後快訊 — 本機一鍵設定
#
# 用法：bash setup.sh
#
# 這支腳本只做「不涉及你帳號憑證」的事：
#   環境檢查 → 安裝套件 → 跑測試 → 初始化 git
# API key、GitHub Secrets 這些請自己在對應網站填，不要寫進任何檔案。

set -e

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
ok()   { echo -e "${GREEN}✓${NC} $1"; }
warn() { echo -e "${YELLOW}!${NC} $1"; }
fail() { echo -e "${RED}✗${NC} $1"; exit 1; }

echo ""
echo "═══════════════════════════════════════"
echo "  盤後快訊 · 本機設定"
echo "═══════════════════════════════════════"
echo ""

# ── 1. Python 版本 ──────────────────────────────────
echo "[1/5] 檢查 Python..."
# Windows 常常只有 python 沒有 python3；兩個名字都試
if command -v python3 >/dev/null 2>&1; then
    PY=python3
elif command -v python >/dev/null 2>&1; then
    PY=python
else
    fail "找不到 python，請先安裝 Python 3.10 以上：https://www.python.org/downloads/"
fi

PY_VER=$($PY -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
PY_OK=$($PY -c 'import sys; print(1 if sys.version_info >= (3,10) else 0)')
[ "$PY_OK" = "1" ] || fail "Python $PY_VER 太舊，需要 3.10 以上"
ok "Python $PY_VER（$PY）"

# ── 2. 安裝套件 ─────────────────────────────────────
echo ""
echo "[2/5] 安裝套件（在虛擬環境內，不污染系統 Python）..."
if [ ! -d ".venv" ]; then
    $PY -m venv .venv
    ok "虛擬環境已建立"
else
    ok "沿用既有虛擬環境"
fi

# shellcheck disable=SC1091
source .venv/bin/activate 2>/dev/null || source .venv/Scripts/activate

if pip install --quiet --upgrade pip 2>/dev/null && \
   pip install --quiet -r requirements.txt 2>/dev/null; then
    ok "套件安裝完成"
else
    # 沒網路或安裝失敗時，檢查系統 Python 是否已有這些套件
    deactivate 2>/dev/null || true
    if $PY -c "import requests, pandas, jinja2, yaml, yfinance, lxml" 2>/dev/null; then
        warn "無法連網安裝，但系統已具備所需套件，改用系統 Python 繼續"
    else
        fail "套件安裝失敗，請檢查網路連線後重跑：pip install -r requirements.txt"
    fi
fi

# ── 3. 環境變數檔 ───────────────────────────────────
echo ""
echo "[3/5] 準備環境變數檔..."
if [ ! -f ".env" ]; then
    cp .env.example .env
    ok ".env 已建立（等你填入 API key）"
else
    ok ".env 已存在，保留不動"
fi

# ── 4. 跑測試 ───────────────────────────────────────
echo ""
echo "[4/5] 用假資料跑一次完整流程..."
DRY_RUN=1 python -m src.main evening > /tmp/report_test.log 2>&1 \
    || { cat /tmp/report_test.log; fail "測試失敗，log 如上"; }
DRY_RUN=1 python -m src.main morning >> /tmp/report_test.log 2>&1 \
    || { cat /tmp/report_test.log; fail "早報測試失敗"; }

REPORTS=$(find docs/reports -name "*.html" 2>/dev/null | wc -l | tr -d ' ')
ok "測試通過，已產出 $REPORTS 份報告"

# ── 5. git ─────────────────────────────────────────
echo ""
echo "[5/5] 初始化 git..."
if [ ! -d ".git" ]; then
    git init --quiet

    # 沒設過 git 身分的機器會在 commit 時卡住，先檢查
    if ! git config user.email >/dev/null 2>&1 && ! git config --global user.email >/dev/null 2>&1; then
        warn "尚未設定 git 身分，跳過首次 commit"
        echo ""
        echo "    請先執行（換成你自己的資料）："
        echo "      git config --global user.name  \"你的名字\""
        echo "      git config --global user.email \"你的信箱\""
        echo ""
        echo "    然後手動 commit："
        echo "      git add . && git commit -m \"初始版本\""
    else
        git add .
        git commit --quiet -m "初始版本：盤後快訊自動分析系統"
        ok "git 已初始化並完成首次 commit"
    fi
else
    ok "git 已存在，略過"
fi

# ── 完成 ───────────────────────────────────────────
echo ""
echo "═══════════════════════════════════════"
echo -e "  ${GREEN}本機設定完成${NC}"
echo "═══════════════════════════════════════"
echo ""
echo "現在可以打開產出的網站看看："
echo ""
if [[ "$OSTYPE" == "darwin"* ]]; then
    echo "    open docs/index.html"
elif [[ "$OSTYPE" == "msys" || "$OSTYPE" == "cygwin" ]]; then
    echo "    start docs/index.html"
else
    echo "    xdg-open docs/index.html"
fi
echo ""
echo "接下來要自己做的三件事（涉及帳號憑證，不該由腳本代勞）："
echo ""
echo "  1. 申請 Anthropic API key"
echo "     https://console.anthropic.com → API Keys → Create Key"
echo "     拿到後填進 .env 的 ANTHROPIC_API_KEY"
echo ""
echo "  2. 建立 Telegram Bot"
echo "     Telegram 搜尋 @BotFather → /newbot"
echo "     token 和 chat id 填進 .env"
echo ""
echo "  3. 推上 GitHub 並開啟 Pages"
echo "     詳細步驟見 DEPLOY.md"
echo ""
echo "填完 .env 後，跑真實資料測試："
echo ""
echo "    source .venv/bin/activate"
echo "    export \$(grep -v '^#' .env | xargs)"
echo "    DRY_RUN=0 python -m src.main evening"
echo ""
