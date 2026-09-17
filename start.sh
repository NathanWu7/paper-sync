#!/usr/bin/env bash
# PaperSync 一键启动：已配置则直接打开编辑器；未配置则打开设置向导。
# 用法：./start.sh
set -e
cd "$(dirname "$0")"

# 选择 Python 环境：系统 python3 已有依赖则直接用，否则建 .venv
PY=python3
if ! "$PY" -c "import fastapi, openai" >/dev/null 2>&1; then
  if [ ! -d .venv ]; then
    "$PY" -m venv .venv
  fi
  .venv/bin/pip -q install -r requirements.txt
  PY=.venv/bin/python
fi

# 端口（config.json 不存在时默认 8787）
PORT=$("$PY" -c "import json;print(json.load(open('config.json')).get('port',8787))" 2>/dev/null || echo 8787)

# 已在运行则跳过启动（避免端口占用）
if ! curl -sf -o /dev/null "http://127.0.0.1:$PORT/api/status"; then
  nohup "$PY" app.py >> server.log 2>&1 &
  for _ in $(seq 1 40); do
    curl -sf -o /dev/null "http://127.0.0.1:$PORT/api/status" && break
    sleep 0.5
  done
fi

# 打开浏览器
xdg-open "http://127.0.0.1:$PORT" 2>/dev/null || true
