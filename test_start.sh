#!/bin/bash

BASE_DIR=$(cd "$(dirname "$0")" && pwd)

source "$BASE_DIR/venv/bin/activate"

cd "$BASE_DIR"

uvicorn main:app --host 0.0.0.0 --port 8000

echo "++++++++++++++++++++++++++++++++++++++"
echo "服务已在后台启动, 运行端口: 8000          "
echo "停止命令: kill \$(lsof -t -i:8000)     "
echo "++++++++++++++++++++++++++++++++++++++"

