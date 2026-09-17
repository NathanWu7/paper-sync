# PaperSync · 双语校稿

中英双语 LaTeX 论文同步编辑工具。左栏改中文，自动译回英文并写回 `.tex`；每个章节自带对话面板，一句话让 LLM 帮你修改该章节。

```
一键启动 → 填写论文 GitHub 链接 + 模型 key → 开始校稿
```

## 功能

- **双语双栏编辑**：点击中文段落编辑（LaTeX 源码级），`Ctrl+Enter` 保存，英文自动翻译并写回 `.tex`
- **章节对话**：每个章节底部的对话框针对**当前章节**提出修改要求（如「把实验描述改得更严谨」），LLM 直接修订英文、中文反向同步；LaTeX 保护校验保证公式/引用/命令不被破坏，违规修改会被拦截
- **保护校验**：翻译与修改全程校验数学式、`\citep`/`\ref` 等键、LaTeX 命令，丢失即警告
- **git 同步**：一键拉取（`pull --ff-only`）/ 提交推送；英文被外部修改的段落自动标记「中文过时」
- **多模型后端**：Kimi / 通义千问 / DeepSeek（OpenAI 兼容协议），随时切换
- **公式渲染与图片**：KaTeX 渲染数学式，PDF 图自动转 PNG 预览

## 快速开始

```bash
./start.sh
```

浏览器自动打开 `http://127.0.0.1:8787`：

1. **首次使用**进入设置向导：
   - **论文仓库**：填 GitHub 链接（公开仓库直接填；私有仓库加填 Token，需 `repo` 权限），或填本地路径
   - **模型**：选择供应商并填入 API key
2. 提交后自动克隆仓库、解析段落，进入编辑器

配置只保存在本机 `config.json`（已 gitignore，含 API key，**永不提交**）。

### 手动启动

```bash
python3 app.py            # 默认端口 8787，python3 app.py 8888 可改端口
pip install -r requirements.txt   # fastapi / uvicorn / openai
```

PDF 转图依赖系统工具 `pdftoppm`（poppler-utils）。首次全量英→中翻译可跑 `python3 init_zh.py`（断点续跑）。

## 使用

| 操作 | 说明 |
|---|---|
| 点击中文段落 | 进入编辑，`Ctrl+Enter` 保存并翻译写回，`Esc` 取消 |
| 章节对话 | 输入修改要求，Enter 发送；修改只作用于当前章节 |
| 拉取 | `git pull --ff-only`，随后自动重解析对齐 |
| 提交并推送 | `git add <论文文件> + commit + push`，提交信息可改 |
| 更换论文 | 左侧底部「更换论文 / 设置」 |

## 工作原理

- **唯一真实源**：论文仓库里的英文 `.tex`
- **中文译稿**：只存本机 `state/<仓库名>/zh_map.json`，不进论文 git 仓库
- **段落对齐**：按 `en_hash`（英文段哈希）对齐；英文被外部修改 → 标记「英文已更新，中文过时」，点「英→中」重译
- **保护校验**：翻译/修改后对比新旧英文的数学段、引用键、命令计数，防止模型丢结构

## 目录

```
├── app.py            # FastAPI 后端（段落/翻译/设置/git 接口）
├── chapter_chat.py   # 章节对话修订（英文优先 + 保护校验 + 中文反译）
├── segmenter.py      # LaTeX 分段/回写（python3 segmenter.py <论文路径> 自检）
├── translator.py     # LLM 翻译 + 保护串校验
├── render.py         # LaTeX → 展示 HTML
├── init_zh.py        # 首次全量英→中翻译
├── start.sh          # 一键启动
├── config.example.json  # 配置模板（复制为 config.json 或走向导生成）
├── static/           # 前端（编辑器 / 设置向导）
├── state/            # 本地译文与对话记录（gitignore）
└── papers/           # 向导克隆的论文仓库（gitignore）
```

## 安全

- `config.json` 含 API key 与 GitHub Token，仅存本机，已被 gitignore
- GitHub Token 只在克隆时使用一次，之后从 remote URL 中清除，后续 pull/push 通过临时认证头注入
- 论文内容不进入本仓库

## License

MIT
