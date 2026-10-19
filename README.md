# 纸间 · PDF 译制台

一个本地优先的 PDF 翻译工具，提供 Mac App 和保留的网页开发入口，用于将复杂 PDF 译为简体中文，同时尽量保留原文件的版式、公式、图表、脚注和双栏结构。

## Mac App：组内轻量分发

- Apple 芯片（arm64），macOS 14 或以上；原生 AppKit / WebKit 窗口，生产界面静态打包，不携带 Node、Electron、Cloudflare 服务或开发依赖。
- 自带裁剪后的 Python / PDF 引擎，组员无需安装开发工具。首次使用会下载约 336 MB 的模型、字体和字符映射，使用固定哈希校验、原子写入，并复用已完成文件。
- 每人配置自己的 OpenAI-compatible API 地址；支持自定义基础路径，自动读取 `/models` 并选择模型，也可手动输入。不支持原生 Anthropic/Gemini 等不同请求协议，需使用兼容端点。
- 在界面勾选“安全记住 API 密钥”并保存后，密钥由原生窗口写入 macOS 登录钥匙串；普通偏好设置只含地址、模型和并发数。未记住时仅在 App 会话内保留。更改地址不会沿用原地址的密钥。
- 桌面版完全忽略开发目录里的 `api_config.txt` 和开发者的 API 环境变量。分发采用源码白名单，不包含密钥文件、历史 PDF、任务目录或模型缓存。
- 每次启动使用随机回环端口和内存会话凭据；仅当前窗口可以访问接口、进度流和下载。默认自动保存到 `~/Downloads/纸间译文/`，可选择统一目录；每个任务独立文件夹，已有结果不覆盖。单文件另存仍使用 macOS 保存窗口。退出会清理本次启动的服务及翻译子进程，运行任务时先询问是否取消。
- App 数据位于 `~/Library/Application Support/PaperStudio/`。保留单文件翻译、取消、长文分段、纯译文和可选对照版；暂不增加批量或断点续传。桌面版初始并发为 8，可按供应商限流调整。
- 0.3.0 新增持久历史、搜索、模型/接口/阶段耗时记录，重启后可下载和重新保存；桌面任务不再自动过期。0.2.0 已有的可读本地结果会导入历史，不推断旧任务的模型或耗时。
- 可同时导出原文和译文 UTF-8 TXT，也可仅提取原文 TXT，无需 API。TXT 保留页码和换行，普通双栏按栏读取，来自 PDF 文本层；不做 OCR 或研究语料清洗，复杂版式需核对。
- 完全相同的 PDF、接口、模型与翻译设置可复用已验证完成结果，使用文件哈希检查完整性，不调用模型；高级设置可关闭复用。旧的段落缓存继续跳过，避免历史污染。首次翻译仍受模型延迟、并发和本地排版影响。

构建（Apple 芯片 Mac，已准备本项目依赖后）：

```bash
npm ci
.venv/bin/python -m pip install -r desktop/requirements-build.lock
.venv/bin/python -m pip install scikit-build==0.18.1 cmake==4.2.3 ninja==1.13.0 wheel==0.45.1
.venv/bin/python scripts/build_slim_opencv.py
.venv/bin/python -m pip install --force-reinstall --no-deps .runtime/slim-wheels/opencv_python_headless-*.whl
npm run app:build
```

全新构建机应先用 Python 3.11 创建 `.venv`。构建脚本会运行后端回归、桌面 TypeScript 和 ESLint 检查，生成 App、ZIP、对应源码、第三方授权及 SHA-256。产物写入 `release/`，0.3.0 App 工作副本在 `build/package-0.3.0-macos-arm64/纸间.app`。

当前采用本地临时签名，未经过 Apple 公证。首次打开的操作与完整使用说明见 `desktop/使用说明.txt`。可用以下命令对冻结后的引擎做隔离验证；模拟供应商仅验证协议与处理链，不能据此推断真实模型译文质量：

```bash
.venv/bin/python scripts/smoke_packaged.py --seed-cache
```

这条完整处理验证需要项目现有的引擎资源缓存；不加 `--seed-cache` 则只检查冻结服务启动、会话隔离与模型列表协议。组员的首次下载不依赖开发目录。

Slim 构建使用同版本 OpenCV 5.0.0.93 的 `core,imgproc,imgcodecs,python3` 模块，关闭视频、GUI 等本 App 未使用的功能；源码仍来自官方 PyPI 发布（https://pypi.org/project/opencv-python-headless/5.0.0.93/），完整 CMake 参数在 `scripts/build_slim_opencv.py`。同时排除未链接的 ONNX Runtime 独立 C API 动态库。保留 PDF 引擎、CoreML 推理、图像处理、SciPy/聚类和字体组件；不依赖组员电脑另装 Python。

## 已实现的工作流

- 上传一份 PDF；源语言不在界面中设置。
- 翻译提示词要求模型逐段识别混合语言，再统一输出简体中文。
- 在页面填写任意 OpenAI-compatible API 的基础地址、模型名和密钥。
- 也可以在 `backend/api_config.txt` 中预设 base_url、api_key、model（每行一项 key=value），以及可选的 `model_reasoning_effort=minimal|low|medium|high`；网页会自动读取，base_url 结尾不需要加 /v1。更安全的无落盘方式是设置 `PDF_TRANSLATION_API_BASE_URL`、`PDF_TRANSLATION_API_KEY`、`PDF_TRANSLATION_MODEL`、`PDF_TRANSLATION_MODEL_REASONING_EFFORT`（以及可选的 `PDF_TRANSLATION_QPS`），环境变量优先于文件。
- 配置文件路径可通过环境变量 `API_CONFIG_PATH` 自定义，例如 `API_CONFIG_PATH=~/my-keys.txt`。
- 81 页及以上的文档会自动按每部分 50 页（可调）交给 PDFMathTranslate-next 处理；其后端负责按页序合并。
- 浏览器通过 Server-Sent Events 显示解析、翻译、回填、合并的总体进度与当前分段。
- 默认并发 qps=32（可在界面或 `api_config.txt` 中调为 1-64）。上游 BabelDOC 会自行把小段组装成 JSON 批次，并通过线程池并行请求；本工具不再做第二层文本批量代理，避免破坏上游协议。
- 默认交付纯译文版（原位替换文字）；在高级设置中按需生成左右对照版（左半页中文译文，右半页原文），避免每次任务都重复做第二次版式渲染。
- 翻译完成后会把原 PDF 的超链接、内部跳转、目录和元数据恢复到实际生成的输出版本中；恢复过程在统一的视觉坐标、PDF 原始坐标和目标插入坐标之间转换，覆盖旋转、缩放及非默认 CropBox，并且重复执行不会增加重复链接。
- 网页表单中的 API 密钥不会写入浏览器存储、日志或任务目录，只会随着当前任务留在本地服务内存中。若使用 `backend/api_config.txt`，该文件本身会保存密钥，服务会尽力将其权限收紧为当前用户可读写；共享机器上应改用环境变量或删除该文件。
- 翻译模型连接有 120 秒超时；如果模型端点不可达，任务会报告连接错误，不会默默停在“解析原稿”十分钟。
- 默认跳过独立的自动术语提取阶段，直接进入正文翻译；该阶段会为几乎每个段落额外调用模型，论文任务通常不值得付出这部分等待时间。
- 当前安装的 BabelDOC 0.6.2 已弃用并忽略旧的表格文字 OCR 接口，因此界面不会发送这个无效开关；表格的几何结构与位置仍交给版式引擎保留，表格单元格文字翻译需等上游支持或另行实现。
- 为避免旧版本批量代理留下的错误译文被缓存复用，新任务默认跳过 PDFMathTranslate-next 的翻译缓存。确认缓存内容干净后，可设置 `PDF_TRANSLATION_USE_CACHE=1` 恢复缓存。
- BabelDOC 的模型、工作文件和缓存默认放在项目内 `backend/data/engine-cache/`，避免依赖用户目录权限；可用 `PDF_TRANSLATION_CACHE_DIR` 指定其他本地目录。
- 输出在交付前会扫描整份 PDF，进行 Unicode 控制分隔符归一化，并检查页数、文件完整性和多条高置信度 prompt 泄漏特征；校验失败会把任务标记为失败，不会提供“看似完成”的 PDF。
- 超链接与内部跳转按“尽力恢复”处理：少量链接未能保留时会显示警告，但正文 PDF 仍标记完成并允许下载；页数不一致、文件损坏或提示词泄漏等正文完整性问题仍会阻止交付。
- 当前本地回归已验证兼容配置到 OpenAI 客户端的转换和标准请求协议；首次接入某个供应商时，仍应先用 1-2 页代表性 PDF 检查该端点的实际响应、限流和译文质量。

## 架构

```text
浏览器界面 (React / Vinext)
        │ 上传 PDF + 本次 API 设置
        ▼
本地 Python 服务 (127.0.0.1:8789)
        │ 事件流（SSE）
        ▼
PDFMathTranslate-next / BabelDOC
        │ OpenAI-compatible requests
        ▼
你的 OpenAI 或兼容模型端点
```

网页开发入口的 PDF 及处理产物保存在本机的 `backend/data/jobs/`，在任务完成 24 小时后清理；桌面版数据在用户的 Application Support 中，历史与原稿持续保留，结果可自动保存到统一目录。只有为翻译所需的文本片段会发送到你配置的模型端点；原文 TXT 提取完全在本机进行。

为避免 API 密钥被误送到未知服务器，网页界面只接受 `localhost`、`127.0.0.1` 或 `::1` 作为译制服务地址；模型 API 地址仍可按需填写官方或兼容端点。若将来需要远程部署，应另行加入认证、TLS 与服务器端密钥管理，而不是直接放开这个限制。

如果首次运行需要下载 BabelDOC 的版式模型，服务会在第一次任务前完成下载。你的环境若设置了 `NO_PROXY`，不要写裸的 `::1`；本服务会自动移除这个会被 `httpx` 误解析为非法端口的条目。

## 本地启动

### 日常使用：一键启动

在 Finder 中双击项目根目录里的 `启动译制台.command`。脚本会自动定位本项目，检查前后端依赖，启动固定端口，并打开 `http://localhost:3000/`。终端窗口需要保持打开；结束使用时在该窗口按 `Control-C`，脚本只会停止本次由它启动的服务。

也可以在任意终端中执行：

```bash
cd /Users/leowu/Downloads/pdf-translate-studio
npm run studio
```

正常情况下无需每天重新执行 `npm ci`。一键脚本只会在 `node_modules` 或 `.venv` 缺失时自动安装依赖。启动日志保存在项目的 `.runtime/` 目录；若端口被其他软件占用，脚本会停止并指出冲突端口，不会悄悄改连旧服务。

### 手动启动（仅用于排错）

前端：

```bash
npm ci
npm run dev
```

另开一个终端启动本地译制服务：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r backend/requirements.txt
.venv/bin/python -m uvicorn app:app --app-dir backend --host 127.0.0.1 --port 8789
```

随后在浏览器打开前端显示的地址。当前修复版默认服务地址是 `http://127.0.0.1:8789`，不要再使用旧端口 `8788`。后端回归测试可用 `.venv/bin/python -m unittest discover -s tests -p 'test_backend.py'` 运行。
如果 8789 已被占用，可改用其他本机端口，并在“本地服务与长文策略”中同步修改地址；端口不应指向旧版本服务。

## 重要边界

- PDFMathTranslate-next 的版式重建能力不是“像素级绝对保证”。扫描件、密码保护文件、复杂跨页表格、嵌入式图像文字与异形文字框仍应先以代表性页面试译。
- 上游引擎目前仍需要一个名义源语言设置；本服务在后台使用英文作为拉丁文字的排版基线，并以逐段自动识别提示词处理混合语言。德英混排的实际效果必须以试译结果判断。
- 本项目使用的 PDFMathTranslate-next 上游项目为 AGPL-3.0；如要把该工具对外提供为服务，请先自行评估相应许可证义务。

## 当前判断与优化计划

### 已处理的高优先级问题

1. **翻译污染与速度**：旧版 `BatchCollector` 把 BabelDOC 已经生成的 JSON 数组再次包装成编号文本，解析失败时还会把完整 prompt 当作译文返回。现已移除该代理，直接使用上游原生 JSON batching、重试和线程池。
2. **任务隔离**：旧版全局 collector 会在并行任务之间串混请求；现有实现每个任务直接建立自己的上游翻译进程，并限制同时运行的任务数（可用 `PDF_TRANSLATION_MAX_ACTIVE_JOBS` 调整）。
3. **超链接**：旧版使用 `saveIncr()` 且不检查重复；现已改为语义去重、旋转/缩放坐标映射、目录/元数据恢复和原子写入，并把恢复数量写入完成事件。

### 下一阶段（需要真实样本与模型基准）

1. 建立同一 PDF、同一模型、冷/热缓存、mono/dual、OCR/表格开关的耗时基准，再按端点限流设置 qps；不要用单次体感比较不同配置。
2. 增加用户可维护的术语表与专名策略（首次出现“中文（原文）”、后续统一），并用人工抽样评估一致性；仅靠提示词不能保证每个专名都正确。
3. 对扫描件中的图像文字、跨页表格和 RTL/竖排语言分别建立 fixture；当前引擎仍以“尽量保留”为目标，不能宣称任意语言和任意版式都像素级一致。
4. 补充后端接口测试、PDF 结构回归（页数/目录/链接/图像/字体）和渲染截图对比；生产使用前还应加入磁盘配额、密钥权限检查和失败任务清理。

验收底线：任何输出都不得包含翻译指令文本；链接数量与目标应与源文件一致，重复恢复不增加数量；页数、目录、图像对象和主要元数据应通过结构检查，失败段落不能静默退化为原始 prompt。
