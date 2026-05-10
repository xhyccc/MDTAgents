# MDT-Orchestrator

一个基于 OpenCode CLI 的多 Agent 医疗会诊（MDT）模拟平台。输入任意结构的病例文件夹，由 AI Agent 自主识别资料类型、自动组建专科团队、并行会诊、输出结构化 MDT 报告。

> **核心原则**：Python 只做"调度员"和"文件快递员"，所有医学判断、文件分类、专科分工全部交给 Agent。

---

## 项目结构

```
MDTAgents/
├── README.md
├── Makefile                     # 常用操作快捷命令
├── app.py                       # Streamlit Web UI 入口
├── requirements.txt             # 核心依赖
├── requirements-dev.txt         # 测试依赖（pytest）
├── requirements-ui.txt          # Web UI 依赖（streamlit）
├── config/
│   └── system.yaml              # 系统配置（模型、并发数、专科注册表）
├── prompts/
│   ├── coordinator_index.md     # Coordinator Round 1：文件分类与索引
│   ├── coordinator_dispatch.md  # Coordinator Round 2：专科团队调度
│   ├── coordinator_synthesis.md # Coordinator Round 3：汇总与最终报告
│   └── specialists/
│       ├── base.md              # 所有专科通用底座 Prompt
│       ├── 影像科.md  (radiologist.md)
│       ├── 病理科.md  (pathologist.md)
│       ├── 肿瘤内科.md (oncologist.md)
│       ├── 外科.md   (surgeon.md)
│       └── 内科.md   (internist.md)
├── scripts/
│   └── setup.sh                 # 一键安装脚本（含 opencode CLI）
├── src/
│   ├── __init__.py
│   ├── scanner.py               # 纯文件扫描，零业务逻辑
│   ├── cli_client.py            # OpenCode CLI 封装
│   ├── file_bus.py              # 文件系统消息总线
│   ├── coordinator.py           # 三轮协调器引擎
│   ├── specialist_pool.py       # 专科 Agent 并行池
│   └── main.py                  # 入口脚本
├── tests/
│   ├── unit/                    # 单元测试
│   └── integration/             # 集成测试
└── cases/
    └── demo_case/               # 示例病例
        ├── 入院记录.md
        ├── CT检查.md
        ├── 病理报告.md
        └── 血常规.md
```

---

## 数据流架构（文件总线模式）

```
cases/{case_id}/
├── [原始资料...任意文件名]          # 输入：用户放入的任何文件
└── .mdt_workspace/                  # 工作区（自动创建）
    ├── 00_manifest.json             # 文件清单+预览（scanner 生成）
    ├── 01_index.json                # Coordinator Round 1：文件分类
    ├── 02_dispatch.json             # Coordinator Round 2：任务分工
    ├── 03_opinions/
    │   ├── 影像科.md
    │   ├── 病理科.md
    │   └── ...                      # 各专科独立意见
    ├── 04_debate.json               # （可选）分歧点交叉讨论
    ├── 05_mdt_report.md             # 最终 MDT 报告
    └── errors/
        └── {agent_name}.log         # Agent 错误日志
```

Agent 间通信完全通过读写 `.mdt_workspace/` 下的文件完成，Python 不解析任何医学内容。

---

## 安装

### 前置条件

- Python 3.10+
- [OpenCode CLI](https://opencode.ai/) 已安装并可在 PATH 中访问（`opencode --version`）

### 一键安装（推荐）

```bash
# 安装核心依赖 + opencode CLI
bash scripts/setup.sh

# 同时安装 Streamlit Web UI 依赖
bash scripts/setup.sh --with-ui
```

### 手动安装

```bash
# 核心依赖
pip install -r requirements.txt

# 开发 / 测试依赖（pytest）
pip install -r requirements-dev.txt

# Web UI 依赖（Streamlit）
pip install -r requirements-ui.txt
```

`requirements.txt` 包含：

| 包 | 用途 |
|---|---|
| `pyyaml` | 读取 `config/system.yaml` |
| `pdfplumber` | PDF 文字层提取 |
| `python-docx` | `.docx` 文件解析 |
| `openpyxl` | `.xlsx` 文件转文本 |

---

## 快速开始

### 命令行

```bash
# 对示例病例运行 MDT
python -m src.main cases/demo_case

# 对自定义病例文件夹运行
python -m src.main /path/to/your/case_folder
```

运行完成后查看报告：

```bash
cat cases/demo_case/.mdt_workspace/05_mdt_report.md
```

### Make 快捷命令

```bash
make install        # 安装核心依赖
make install-dev    # 安装核心 + 测试依赖
make install-ui     # 安装核心 + Web UI 依赖
make setup          # 一键安装脚本（含 opencode）
make setup-ui       # 一键安装脚本（含 opencode 和 UI）

make run            # 对 cases/demo_case 运行 MDT
make run CASE=cases/my_case   # 对指定病例运行 MDT
make ui             # 启动 Streamlit Web UI

make test           # 运行全部测试（单元 + 集成）
make test-unit      # 仅运行单元测试
make test-int       # 仅运行集成测试
make lint           # Python 语法检查
```

### Web UI（Streamlit）

```bash
# 启动 Web 界面
streamlit run app.py
# 或
make ui
```

Web UI 提供三个功能页签：

- **🏥 Run MDT** — 选择或上传病例文件夹、执行会诊、下载最终报告
- **🔍 Debug** — 浏览工作区中间文件（manifest、index、dispatch、各专科意见）
- **⚙️ Admin** — 查看/编辑系统配置、管理专科注册表

---

## 工作流程

```
Round 0  扫描文件夹
         ↓ 00_manifest.json
Round 1  Coordinator 索引（文件分类 + 完整度评估）
         ↓ 01_index.json
Round 2  Coordinator 调度（选择专科团队 + 分配文件）
         ↓ 02_dispatch.json
Round 3  并行专科会诊（ThreadPoolExecutor）
         ↓ 03_opinions/{name}.md
Round 4  Coordinator 汇总（共识 + 分歧 + 最终结论）
         ↓ 05_mdt_report.md
```

---

## 配置

编辑 `config/system.yaml` 调整以下参数：

```yaml
opencode:
  default_model: "claude-sonnet-4"  # 默认模型
  timeout: 300                       # Agent 超时（秒）
  max_workers: 5                     # 最大并行专科数

specialists:
  - name: "影像科"
    model: "claude-sonnet-4"         # 可为每个专科指定不同模型
    file_categories: ["影像"]

workflow:
  enable_debate: false               # 是否开启 Round 4 交叉讨论
```

---

## 扩展指南

### 添加新专科

1. 在 `prompts/specialists/` 目录下新建 `{专科名}.md`（按中文命名）
2. 在 `config/system.yaml` 的 `specialists` 列表中注册
3. Coordinator 会在 Dispatch 阶段自动考虑是否派出该专科

### 支持新文件格式

在 `src/scanner.py` 的 `_SUFFIX_EXTRACTORS` 字典中添加新的扩展名与提取函数即可。无需修改任何 Prompt 或业务逻辑。

### 开启交叉讨论（Debate）

将 `config/system.yaml` 中的 `enable_debate` 设为 `true`，并实现 `src/debater.py`：

1. 读取 `03_opinions/` 中各专科意见，识别分歧点
2. 让相关专科 Agent 再读对方意见并回应
3. 将交叉讨论结果写入 `04_debate.json`
4. Coordinator Synthesis 基于 debate 结果生成最终报告

---

## 错误处理

- 每个 Agent 调用超时或失败时，错误信息写入 `.mdt_workspace/errors/{agent_name}.log`
- 专科会诊中某个专科失败不影响其他专科继续运行
- Coordinator 失败会抛出 `AgentError` 并终止流程

---

## 支持的文件格式

| 扩展名 | 处理方式 |
|--------|---------|
| `.md`, `.txt`, `.json`, `.csv` | 直接读取文本 |
| `.pdf` | pdfplumber 提取文字层 |
| `.docx` | python-docx 提取段落文本 |
| `.xlsx` | openpyxl 转换为文本表格 |
