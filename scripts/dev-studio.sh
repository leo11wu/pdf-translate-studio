#!/usr/bin/env bash

set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
PROJECT_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd -P)"
FRONTEND_HOST="${PDF_STUDIO_FRONTEND_HOST:-127.0.0.1}"
FRONTEND_PORT="${PDF_STUDIO_FRONTEND_PORT:-3000}"
BACKEND_HOST="${PDF_STUDIO_BACKEND_HOST:-127.0.0.1}"
BACKEND_PORT="${PDF_STUDIO_BACKEND_PORT:-8789}"
FRONTEND_URL="http://localhost:${FRONTEND_PORT}/"
BACKEND_URL="http://${BACKEND_HOST}:${BACKEND_PORT}"
RUNTIME_DIR="$PROJECT_DIR/.runtime"
FRONTEND_LOG="$RUNTIME_DIR/frontend.log"
BACKEND_LOG="$RUNTIME_DIR/backend.log"

frontend_pid=""
backend_pid=""

say() {
  printf '%s\n' "$*"
}

fail() {
  say ""
  say "启动失败：$*"
  say "前端日志：$FRONTEND_LOG"
  say "后端日志：$BACKEND_LOG"
  exit 1
}

port_is_listening() {
  lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1
}

backend_is_ours() {
  local response
  response="$(curl --noproxy '*' -fsS --max-time 3 "$BACKEND_URL/api/health" 2>/dev/null || true)"
  case "$response" in
    *'"engine":"pdf2zh-next"'*|*'"engine": "pdf2zh-next"'*) return 0 ;;
    *) return 1 ;;
  esac
}

frontend_is_ours() {
  local response
  response="$(curl --noproxy '*' -fsS --max-time 3 "$FRONTEND_URL" 2>/dev/null || true)"
  case "$response" in
    *'纸间'*|*'PDF 译制台'*) return 0 ;;
    *) return 1 ;;
  esac
}

show_log_tail() {
  local log_file="$1"
  if [[ -s "$log_file" ]]; then
    say ""
    tail -n 24 "$log_file"
  fi
}

wait_for_backend() {
  local attempt
  for attempt in $(seq 1 90); do
    if backend_is_ours; then
      return 0
    fi
    if [[ -n "$backend_pid" ]] && ! kill -0 "$backend_pid" 2>/dev/null; then
      show_log_tail "$BACKEND_LOG"
      return 1
    fi
    sleep 0.5
  done
  show_log_tail "$BACKEND_LOG"
  return 1
}

wait_for_frontend() {
  local attempt
  for attempt in $(seq 1 120); do
    if frontend_is_ours; then
      return 0
    fi
    if [[ -n "$frontend_pid" ]] && ! kill -0 "$frontend_pid" 2>/dev/null; then
      show_log_tail "$FRONTEND_LOG"
      return 1
    fi
    sleep 0.5
  done
  show_log_tail "$FRONTEND_LOG"
  return 1
}

stop_owned_process() {
  local pid="$1"
  if [[ -z "$pid" ]] || ! kill -0 "$pid" 2>/dev/null; then
    return
  fi
  kill "$pid" 2>/dev/null || true
  local attempt
  for attempt in $(seq 1 20); do
    if ! kill -0 "$pid" 2>/dev/null; then
      wait "$pid" 2>/dev/null || true
      return
    fi
    sleep 0.1
  done
  kill -KILL "$pid" 2>/dev/null || true
  wait "$pid" 2>/dev/null || true
}

cleanup() {
  local exit_code=$?
  trap - EXIT INT TERM HUP
  if [[ -n "$frontend_pid" || -n "$backend_pid" ]]; then
    say ""
    say "正在停止本次启动的服务……"
  fi
  stop_owned_process "$frontend_pid"
  stop_owned_process "$backend_pid"
  if [[ -n "$frontend_pid" || -n "$backend_pid" ]]; then
    say "已停止。"
  fi
  exit "$exit_code"
}

trap cleanup EXIT INT TERM HUP

mkdir -p "$RUNTIME_DIR"
cd "$PROJECT_DIR"

say "纸间 · PDF 译制台"
say "项目目录：$PROJECT_DIR"
say "前端地址：$FRONTEND_URL"
say "后端地址：$BACKEND_URL"
say ""

if [[ "$BACKEND_PORT" == "8789" ]] && port_is_listening "8788"; then
  say "提示：检测到旧版后端仍在 8788 端口运行；本次不会使用它。"
  say "若不再需要，可回到原来的后端终端按 Control-C。"
  say ""
fi

command -v npm >/dev/null 2>&1 || fail "找不到 npm，请先安装 Node.js 22 或更高版本。"
command -v python3 >/dev/null 2>&1 || fail "找不到 python3。"
command -v curl >/dev/null 2>&1 || fail "找不到 curl。"
command -v lsof >/dev/null 2>&1 || fail "找不到 lsof。"

if [[ ! -x "$PROJECT_DIR/node_modules/.bin/vinext" ]]; then
  say "首次运行：正在安装前端依赖……"
  npm ci || fail "前端依赖安装失败。"
fi

if [[ ! -x "$PROJECT_DIR/.venv/bin/python" ]]; then
  say "首次运行：正在创建 Python 环境……"
  python3 -m venv "$PROJECT_DIR/.venv" || fail "Python 环境创建失败。"
fi

if ! "$PROJECT_DIR/.venv/bin/python" -c "import importlib.util, sys; sys.exit(any(importlib.util.find_spec(name) is None for name in ('fastapi', 'uvicorn', 'pdf2zh_next')))" >/dev/null 2>&1; then
  say "首次运行：正在安装后端依赖……"
  "$PROJECT_DIR/.venv/bin/python" -m pip install -r "$PROJECT_DIR/backend/requirements.txt" || fail "后端依赖安装失败。"
fi

if port_is_listening "$BACKEND_PORT"; then
  if backend_is_ours; then
    say "后端已经运行，直接复用。"
  else
    fail "端口 $BACKEND_PORT 已被其他程序占用。请先关闭占用者，再重新双击启动。"
  fi
else
  : >"$BACKEND_LOG"
  say "正在启动后端……"
  "$PROJECT_DIR/.venv/bin/python" -m uvicorn app:app \
    --app-dir "$PROJECT_DIR/backend" \
    --host "$BACKEND_HOST" \
    --port "$BACKEND_PORT" \
    >"$BACKEND_LOG" 2>&1 &
  backend_pid=$!
  wait_for_backend || fail "后端未能在 ${BACKEND_PORT} 端口就绪。"
fi

if port_is_listening "$FRONTEND_PORT"; then
  if frontend_is_ours; then
    say "前端已经运行，直接复用。"
  else
    fail "端口 $FRONTEND_PORT 已被其他程序占用。请先关闭占用者，再重新双击启动。"
  fi
else
  : >"$FRONTEND_LOG"
  say "正在启动前端……"
  "$PROJECT_DIR/node_modules/.bin/vinext" dev \
    --host "$FRONTEND_HOST" \
    --port "$FRONTEND_PORT" \
    --strictPort \
    >"$FRONTEND_LOG" 2>&1 &
  frontend_pid=$!
  wait_for_frontend || fail "前端未能在 ${FRONTEND_PORT} 端口就绪。"
fi

say ""
say "启动成功：$FRONTEND_URL"

if [[ "${PDF_STUDIO_NO_BROWSER:-0}" != "1" ]] && command -v open >/dev/null 2>&1; then
  open "$FRONTEND_URL" >/dev/null 2>&1 || true
fi

if [[ -z "$frontend_pid" && -z "$backend_pid" ]]; then
  say "前后端原本都已运行，浏览器已打开。"
  trap - EXIT INT TERM HUP
  exit 0
fi

say "请保持此窗口打开。结束使用时按 Control-C。"
say "日志目录：$RUNTIME_DIR"

while true; do
  if [[ -n "$frontend_pid" ]] && ! kill -0 "$frontend_pid" 2>/dev/null; then
    show_log_tail "$FRONTEND_LOG"
    fail "前端进程意外结束。"
  fi
  if [[ -n "$backend_pid" ]] && ! kill -0 "$backend_pid" 2>/dev/null; then
    show_log_tail "$BACKEND_LOG"
    fail "后端进程意外结束。"
  fi
  sleep 1
done
