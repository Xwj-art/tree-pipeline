# tree-pipeline（树形并行开发流水线）- Claude Code Skill

本 Skill 用于把”需求输入 → 规格冻结 → DAG 批次并行实现 → 契约验证/CDC/集成测试 → 看板汇总”的流程标准化，适合多模块/多子系统并行开发的工程任务。

## 触发方式

- 在对话中说：`tree-pipeline` 或 “树形并行流水线”
- 描述需求时提及：**多模块并行开发**、**按依赖图分批实现**、**先冻结契约再编码**、**需要断点续跑**、**模块间有契约依赖**
- 或者直接让 Claude 执行：`python3 -m pipeline.orchestrator start ...`

## 适用场景

- 需求较大，且可拆分为多个模块（有明确依赖关系）
- 希望先冻结契约（`contract.yaml`），再并行编码
- 希望用任务账本（`tasks.jsonl`）+ 看板（`dashboard.md`）做断点续跑与审计
- 需要”微批次测试（2-5 个函数一组）”与”风险驱动覆盖率（80-90% 行覆盖 + 场景清单 + CDC）”

## 三层架构（关键决策）

```
主编排器 → DAG 分批 → 模块 Agent（并行）
                         ↳ dispatch → Worker Agent × N（文件级并行）
```

- **主编排器**：`pipeline/orchestrator.py` — 生成 Spec + 构建 DAG 批次 + 冻结契约 + 汇总看板
- **模块 Agent**：Claude 读 context_packet，规划模块内文件拆分，调用 `dispatch` 分发到 Worker
- **Worker Agent × N**：每个 Worker 拿一个文件的 context_packet，独立实现并标记 done

## 使用步骤（推荐）

1. 如果你只有一句需求，先生成模块建议草案：
   - `python3 -m pipeline.orchestrator suggest-modules --requirement "你的需求描述" --output suggestions.json`
   - 输出是结构化 JSON，包含：
     - `capabilities`：从需求里抽出的能力点
     - `module_candidates`：候选模块（含说明、风险、contract draft、评分）
     - `dependency_candidates`：建议依赖边（`consumer -> provider`）
     - `shared_hotspots`：应由 `main` 分支集中管理的共享契约/基础模型/公共库热点
     - `open_questions`：需要人工确认的问题
2. 人工审查模块草案，再确认正式模块清单 + 依赖边：
   - 最简单：把确认后的模块写成 `--module` 与 `--edge`
   - 或者把模块与依赖写进 `pipeline.config.yaml` 并用 `--config` 指定
   - 关键原则：
     - 模块分支只负责各自代码
     - `contract.yaml`、基础公共模型、共享协议/公共库等共享热点由 `main` 分支集中管理
3. 启动流水线 + 自动创建模块分支：
   - `python -m pipeline.orchestrator start --project-root <你的项目根目录> --run-dir <本次运行目录> --git-auto`
   - `--git-auto` 会自动为每个模块创建 `module/<name>` 分支并推送到远程
4. 按 DAG 批次并行执行（Batch1/Batch2...）：
   - 编排器会为每个模块生成 `context_packet.md`（最小上下文包）
   - 模块 Agent 规划文件拆分，创建 plan.json，执行 `dispatch`：
     `python3 -m pipeline.orchestrator dispatch --run-dir <dir> --module <name> --plan plan.json`
   - Worker Agent 各自拿一个文件级 context_packet 并行实现
   - Worker 完成后标记 `done`，模块 Agent 收集结果 + 微批次单测
5. 模块完成后发布（提交 + 推送 + PR）：
   - `python3 -m pipeline.orchestrator module-ship --run-dir <dir> --project-root <repo> --module <name>`
   - 自动 add/commit/push，可选自动创建 PR（取决于 `git.auto_create_pr` 配置）
6. 完成后运行验证阶段：
   - `python -m pipeline.orchestrator validate --run-dir <本次运行目录>`
7. main 守门合并：
   - `python -m pipeline.orchestrator gate-merge --run-dir <dir> --project-root <repo> --module <name>`
   - 检查 CI 状态 → squash merge → 删除分支 → 刷新本地 main
8. 任何时候查看状态：
   - `python -m pipeline.orchestrator status --run-dir <本次运行目录>`
9. 中断后恢复：
   - `python -m pipeline.orchestrator resume --run-dir <本次运行目录>`

## 依赖工具

- Python 3.10+（建议 3.11）
- 可选：`PyYAML`（用于解析 `contract.yaml`；未安装会给出明确错误）
- 可选：测试工具（由 `pipeline.config.yaml` 配置）
  - `pytest`、`coverage`、`requests`、你的项目自带测试脚本等

## 职责边界（做什么 / 不做什么）

tree-pipeline 是**多模块并行开发的总统筹层**，不是单模块实现者：

| 负责                                       | 不负责            | 委托给                      |
| ------------------------------------------ | ----------------- | --------------------------- |
| Spec 生成 + 契约冻结 + DAG 分批            | 详细实现方案叙事  | `planner` agent             |
| JSONL 任务账本（含子任务） + 状态机        | 模块/文件代码实现 | 模块 Agent / Worker Agent   |
| Context Packet 构建与分发（模块+文件级）   | 代码审查          | `code-reviewer` agent       |
| `dispatch` 文件级任务分发 + 内部 DAG       | TDD 测试编写      | `tdd-guide` agent           |
| 契约校验（签名 + semver）                  | 代码实现          | Module Agent / Worker Agent |
| Git 分支/提交/PR/合并自动化                | -                 | 内置 `GitManager`           |
| dashboard.md 看板汇总（模块+File Workers） | CI/CD 部署        | 项目自身 CI 系统            |
| 中断恢复（resume）                         | Mutation Testing  | 夜间 CI（仅预留钩子）       |

## 安装

```bash
# 克隆到 Claude Code 全局 skill 目录
git clone git@github.com:Xwj-art/tree-pipeline.git ~/.claude/skills/tree-pipeline

# 方式一：pip 可编辑安装（推荐，非 Homebrew Python）
pip install -e ~/.claude/skills/tree-pipeline

# 方式二：Homebrew Python（macOS 默认）— 使用 PYTHONPATH
# 在 ~/.claude/settings.json 中添加：
#   "env": {"PYTHONPATH": "$HOME/.claude/skills/tree-pipeline"}
# 或每次执行时：
PYTHONPATH=~/.claude/skills/tree-pipeline python3 -m pipeline.orchestrator --help

# 方式三：pipx（Homebrew 推荐）
brew install pipx && pipx install --editable ~/.claude/skills/tree-pipeline

# 验证安装
python3 -m pipeline.orchestrator --help
```

## 输出物（run-dir 下）

- `contract.yaml`：版本化契约（带 `version` 字段）
- `spec.md`：冻结规格（含模块拆分、依赖、风险、验收标准）
- `conventions.md`：工程约定（命名/测试/回滚/Feature Flags）
- `tasks.jsonl`：任务账本（标准状态机，支持断点续跑）
- `dashboard.md`：人类可读看板（按状态汇总 + 下一步建议）
- `context_packets/<module>.md`：每个模块的最小上下文包
