# OCRServiceApi

基于 FastAPI 和 PaddleOCR 的异步文档识别服务。服务支持图片、PDF、`.doc` 和 `.docx` 文件，通过后台任务队列执行识别，并使用 SQLite 保存任务状态和识别结果。

## 功能特性

- 支持 JPEG、PNG、TIFF、BMP、WebP 图片。
- 支持多页 PDF，逐页在内存中渲染和识别。
- 支持 `.docx` 和 `.doc` Word 文档。
- Word 能直接读取全文时不执行 OCR；无法读取有效文字时执行 OCR，并使用与 PDF、图片一致的结果字段。
- PaddleOCR 模型启动预热并在进程内复用。
- 异步任务提交、状态查询和结果查询。
- SQLite 任务持久化，服务重启后恢复未完成任务。
- 可选任务完成回调。
- 上传大小、PDF 页数、队列长度和内存限制。
- 任务完成后自动清理上传的临时文件。

## Word 处理规则

Word、PDF 和图片都是本服务支持的输入类型。Word 结果不要求分页，因此服务会先直接读取文档全文。

本服务按以下顺序处理 Word：

1. 使用 `aspose-words-foss` Python 包读取 `.doc` 或 `.docx` 全文。
2. 如果读取到有效文字，直接返回 `text`，完全不执行 OCR。
3. 如果没有读取到有效文字，在内部转换并执行 OCR。
4. Word 不做真实分页，所有内容合并为一个逻辑页 `page: 1`，字段与 PDF、图片一致。

因此，可编辑 Word 可以避免 OCR 带来的误识别和额外耗时；无法直接取得文字的扫描型 Word 才进入 OCR 流程。

> Word 处理不依赖 LibreOffice 或 Microsoft Office，但需要 `aspose-words-foss`。该包当前要求 Python 3.10–3.12，建议现场统一使用 Python 3.12。

## 项目结构

```text
OCRServiceApi/
├── main.py                         # FastAPI 应用及接口
├── core/settings.py                # 服务配置
├── schemas/ResponseModel.py        # 响应数据模型
├── utils/
│   ├── OCRDetector.py              # PaddleOCR 引擎与图像预处理
│   ├── WordDocumentProcessor.py    # Word 内容读取及 OCR 兜底
│   ├── TaskQueueManager.py         # 异步任务队列和识别流程
│   ├── TaskStore.py                # SQLite 持久化和任务恢复
│   ├── MemoryGuard.py              # 内存监控与限制
│   ├── LoggerDetector.py           # 日志配置
│   └── ResponseUtil.py             # 统一 HTTP 响应
├── models/                         # 本地 PaddleOCR 模型
├── task_db/                        # SQLite 任务数据库
├── uploads/                        # 上传临时文件
├── output/                         # 临时输出目录
├── logs/                           # 服务日志
├── requirements.txt
├── start.sh                        # 后台启动脚本
└── test_start.sh                   # 前台启动脚本
```

## 环境要求

- Python 3.10–3.12（推荐 Python 3.12）
- PaddlePaddle 3.x
- PaddleOCR 3.x
- FastAPI
- PyMuPDF
- OpenCV
- `aspose-words-foss`（Python Word 读取与转换）

本项目默认使用以下离线模型目录：

```text
models/PP-OCRv5_mobile_det
models/PP-OCRv5_mobile_rec
```

## 安装

创建虚拟环境并安装依赖：

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

无网络现场应在一台与现场操作系统、CPU 架构和 Python 版本一致的联网机器上提前下载 wheel：

```bash
python3.12 -m pip download -r requirements.txt -d wheels
```

将项目和 `wheels/` 一起带到现场后离线安装：

```bash
python3.12 -m pip install --no-index --find-links=./wheels -r requirements.txt
```

确认 OCR 模型已放置到 `models/` 下，然后启动服务。

## 启动服务

前台启动：

```bash
./test_start.sh
```

后台启动：

```bash
./start.sh
```

也可以直接使用 Uvicorn：

```bash
uvicorn main:app --host 0.0.0.0 --port 8000
```

启动时服务会加载并预热 PaddleOCR 模型。模型加载完成后才能正常接收请求。

接口文档地址：

- Swagger UI：`http://127.0.0.1:8000/docs`
- OpenAPI JSON：`http://127.0.0.1:8000/openapi.json`

## 配置

配置定义在 `core/settings.py`，也可以通过 `.env` 或环境变量覆盖。

| 配置项 | 默认值 | 说明 |
| --- | ---: | --- |
| `SERVER_HOST` | `127.0.0.1` | 服务监听地址配置 |
| `SERVER_PORT` | `8000` | 服务端口配置 |
| `MAX_UPLOAD_SIZE_MB` | `100` | 单个上传文件最大容量 |
| `MAX_PDF_PAGES` | `200` | PDF 最大页数 |
| `MAX_QUEUE_SIZE` | `100` | 等待队列最大任务数 |
| `TEMP_FILE_RETENTION_SECONDS` | `86400` | 孤儿临时文件保留时间，默认 24 小时 |
| `TEMP_CLEANUP_INTERVAL_SECONDS` | `3600` | 临时文件定时清理间隔，默认 1 小时 |
| `MAX_WORKERS` | `1` | 后台任务 worker 数量 |
| `MEMORY_SOFT_LIMIT_MB` | `4096` | 内存软限制 |
| `MEMORY_HARD_LIMIT_MB` | `5120` | 进程虚拟内存硬限制 |
| `TEMP_UPLOAD_PATH` | `uploads` | 上传临时目录 |
| `TASK_DB_PATH` | `task_db/tasks.db` | SQLite 数据库路径 |

示例 `.env`：

```dotenv
SERVER_HOST=127.0.0.1
SERVER_PORT=8000
MAX_UPLOAD_SIZE_MB=100
MAX_PDF_PAGES=200
MAX_QUEUE_SIZE=100
MAX_WORKERS=1
TEMP_FILE_RETENTION_SECONDS=86400
TEMP_CLEANUP_INTERVAL_SECONDS=3600
```

## API 使用

### 健康检查

```http
GET /api/v1
```

### 提交识别任务

```http
POST /api/v1/ocr
Content-Type: multipart/form-data
```

表单参数：

| 参数 | 必填 | 说明 |
| --- | --- | --- |
| `file` | 是 | 图片、PDF、DOC 或 DOCX 文件 |
| `callback_url` | 否 | 任务完成后的 POST 回调地址 |

使用 curl 提交：

```bash
curl -X POST http://127.0.0.1:8000/api/v1/ocr \
  -F "file=@./document.docx"
```

带回调地址：

```bash
curl -X POST http://127.0.0.1:8000/api/v1/ocr \
  -F "file=@./document.pdf" \
  -F "callback_url=https://example.com/ocr/callback"
```

成功提交返回 HTTP `202 Accepted`：

```json
{
  "code": 202,
  "message": "任务已提交",
  "data": {
    "message": "任务已提交，正在排队处理",
    "status": "Queued",
    "task_id": "a5cbbc5c-6c8f-47c5-81e6-45ccbbdfc012",
    "status_url": "/api/v1/ocr/status/a5cbbc5c-6c8f-47c5-81e6-45ccbbdfc012",
    "result_url": "/api/v1/ocr/result/a5cbbc5c-6c8f-47c5-81e6-45ccbbdfc012"
  }
}
```

### 查询任务状态

```http
GET /api/v1/ocr/status/{task_id}
```

任务状态包括：

- `pending`：等待处理
- `processing`：正在处理
- `success`：识别成功
- `failed`：识别失败

### 获取任务结果

```http
GET /api/v1/ocr/result/{task_id}
```

任务尚未完成时返回 HTTP `202`；任务不存在时返回 `404`；任务失败时返回 `500`。

### 查询最近任务

```http
GET /api/v1/ocr/tasks?limit=20
```

`limit` 允许范围为 1～100。

## 图片和 PDF 结果结构

```json
{
  "file_type": "pdf",
  "pages": [
    {
      "page": 1,
      "rec_texts": ["识别文字"],
      "rec_scores": [0.98],
      "rec_polys": [[[10, 10], [100, 10], [100, 40], [10, 40]]],
      "dt_polys": [[[10, 10], [100, 10], [100, 40], [10, 40]]]
    }
  ]
}
```

## Word 结果结构

```json
{
  "file_type": "word",
  "processing_method": "direct_read",
  "pages": [
    {
      "page": 1,
      "rec_texts": ["Word 文档中的全部正文内容"],
      "rec_scores": [1.0],
      "rec_polys": [],
      "dt_polys": []
    }
  ]
}
```

以上结果表示成功直接读取 Word 全文，处理过程中没有调用 OCR。

无法直接读取文字时返回 OCR 结果：

```json
{
  "file_type": "word",
  "processing_method": "ocr_fallback",
  "pages": [
    {
      "page": 1,
      "rec_texts": ["扫描页面识别出的全部文字"],
      "rec_scores": [0.97],
      "rec_polys": [],
      "dt_polys": []
    }
  ]
}
```

`.doc` 转换成功时还会包含：

```json
{
  "converted_from": "doc"
}
```

## 回调格式

配置 `callback_url` 后，任务完成或失败时服务会发送 POST JSON：

```json
{
  "task_id": "a5cbbc5c-6c8f-47c5-81e6-45ccbbdfc012",
  "status": "success",
  "message": "OCR 识别成功",
  "cost_s": 2.35
}
```

回调超时时间为 10 秒。回调失败只记录日志，不改变已经完成的 OCR 任务状态。

## 运行数据和清理

- 原始上传文件在任务成功或失败后自动删除。
- 上传写入失败或任务入队失败时，会立即删除已经生成的残缺文件。
- PDF 页面直接在内存中处理，不会为整本文档生成临时 PNG。
- Word 转换文件位于系统临时目录，处理结束或发生异常时自动删除。
- 后台任务定时扫描 `uploads/` 和 `output/images/`，删除超过保留时间的孤儿文件。
- 定时清理会排除数据库中 `pending` 和 `processing` 任务使用的源文件。
- 已完成和失败的任务记录默认保留 7 天。
- SQLite 最多保留约 100,000 条记录。
- 服务重启后会恢复数据库中的 `pending` 和 `processing` 任务。
- 如果恢复任务对应的上传文件已不存在，该任务会被标记为失败。

## 部署注意事项

- PaddleOCR 推理使用全局模型锁，单进程中增加 worker 不等于增加模型推理并发。
- 如果需要并行推理，建议使用多个服务进程或容器，并根据内存容量规划模型实例数。
- `RLIMIT_AS` 限制虚拟地址空间，容器部署时建议同时使用 cgroup 内存限制。
- 生产环境应限制 CORS 来源，并为管理类接口增加认证。
- `callback_url` 会由服务端主动访问。对外开放服务前，应增加回调域名白名单和内网地址限制，避免 SSRF 风险。
- `models/`、`uploads/`、`output/`、`logs/` 和 `task_db/` 的持久化策略应根据部署环境配置。

## 常见问题

### `.doc` 处理失败

Word 处理需要安装项目中的 Python 依赖：

```bash
python -c "import aspose.words_foss; print('aspose-words-foss ready')"
```

如果导入失败，请确认使用 Python 3.10–3.12，并从离线 wheel 目录安装依赖。

### 服务启动较慢

服务启动时会加载并预热 PaddleOCR 检测和识别模型，首次启动耗时属于正常现象。

### 任务一直处于 pending

检查：

- OCR 模型是否成功加载；
- 日志中是否存在内存软限制等待；
- `uploads/` 中对应源文件是否仍然存在；
- 队列是否达到 `MAX_QUEUE_SIZE`。

### 查看日志

```bash
tail -f logs/app.log
```
