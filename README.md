# tree-pipeline

`tree-pipeline` 是一个用于"树形并行开发流水线"的轻量实现：以冻结契约（contract-first）为核心，用依赖图（DAG）分批并行推进模块开发，并以 `tasks.jsonl` + `dashboard.md` 实现可恢复、可审计的进度管理。

## 目标流程（三层架构）

需求输入 → **主编排器**（Spec + DAG + 契约冻结） → **模块 Agent** 并行 → **dispatch** → **Worker Agent × N** 文件级并行 → 验证 + 看板

## 关键策略

- 三层 Agent：主编排器 → 模块 Agent → Worker Agent（文件级并行）
- 微批次测试：2-5 个函数一组
- 模块内 DAG：文件间可声明依赖，Worker 按就绪顺序分发
- 风险驱动覆盖率：80-90% 行覆盖 + 场景清单 + CDC
- 重构封顶：最多 1 轮；仍不通过则升级人工
- Selective Feature Flags：高风险路径用 Flag 包裹，支持快速回滚
- Mutation Testing：延期到夜间 CI（此仓库仅预留钩子）
- 契约版本化：`contract.yaml` 包含 `version`
- 上下文包：仅分发"最小上下文"（硬 Token 预算）
- 回滚策略：关 Flag → revert 合并 → 修复 PR → 冻结下游
- 进度看板：`tasks.jsonl` + `dashboard.md`，双状态机（模块级 8 态 + 文件级 6 态）

## 安装

```bash
# 克隆到 Claude Code 全局 skill 目录
git clone git@github.com:Xwj-art/tree-pipeline.git ~/.claude/skills/tree-pipeline

# 方式一：pip 可编辑安装（非 Homebrew Python）
pip install -e ~/.claude/skills/tree-pipeline

# 方式二：Homebrew Python（macOS 默认）— 使用 PYTHONPATH
PYTHONPATH=~/.claude/skills/tree-pipeline python3 -m pipeline.orchestrator --help

# 方式三：pipx（Homebrew 推荐）
brew install pipx && pipx install --editable ~/.claude/skills/tree-pipeline

# 验证
python3 -m pipeline.orchestrator --help
```

可选依赖：`pip install PyYAML`（解析 YAML 配置/契约文件；未安装时可用 JSON）。

## CLI 命令

| 命令              | 说明                                                                                         |
| ----------------- | -------------------------------------------------------------------------------------------- |
| `suggest-modules` | 从自然语言需求输出模块建议、依赖候选、共享热点                                               |
| `start`           | 启动新流水线：生成 Spec + Contract + 任务账本 + 上下文包，可选 `--git-auto` 自动创建模块分支 |
| `dispatch`        | 将模块拆分为文件级子任务，生成每个文件的 context_packet                                      |
| `file-start`      | Worker Agent 认领文件子任务（ready → coding）                                                |
| `file-done`       | 标记文件完成：自动解锁下游文件、触发模块单测、推进模块状态                                   |
| `module-ship`     | 发布模块：scoped commit + push + 自动 PR，限制模块路径所有权                                 |
| `gate-merge`      | main 守门合并：检查 CI → squash merge → 删除分支 → 刷新本地 main                             |
| `validate`        | 契约校验 + 单元测试 + 集成测试 + CDC 测试 + 质量门控                                         |
| `status`          | 渲染 dashboard.md 看板（模块 + 文件状态 + git 状态）                                         |
| `resume`          | 中断恢复：自动解除过期阻塞 + 刷新看板                                                        |
| `module-check`    | 诊断：按状态统计模块文件分布                                                                 |
| `next`            | 显示下一个就绪的模块和文件 Worker                                                            |

## 快速开始

```bash
# 1. 从需求生成模块建议
python3 -m pipeline.orchestrator suggest-modules \
  --requirement "实现一个带 CLI 入口、配置管理和诊断功能的工具" \
  --output /tmp/suggestions.json

# 2. 审查 suggestions.json，确认模块后启动流水线
python3 -m pipeline.orchestrator start \
  --project-root . \
  --run-dir /tmp/tp-run \
  --module core --module cli --module diagnostics \
  --edge cli:core --edge diagnostics:core \
  --git-auto

# 3. 模块 Agent 拆分文件并分发 Worker
echo '{"files":[{"id":"src/models.py","title":"Data models","depends_on":[]},{"id":"src/service.py","title":"Business logic","depends_on":["src/models.py"]}]}' > /tmp/plan.json
python3 -m pipeline.orchestrator dispatch --run-dir /tmp/tp-run --module core --plan /tmp/plan.json

# 4. Worker Agent 认领并完成文件
python3 -m pipeline.orchestrator file-start --run-dir /tmp/tp-run --task-id "core::src/models.py"
python3 -m pipeline.orchestrator file-done --run-dir /tmp/tp-run --task-id "core::src/models.py"

# 5. 发布模块（commit + push + PR）
python3 -m pipeline.orchestrator module-ship --run-dir /tmp/tp-run --project-root . --module core

# 6. main 守门合并
python3 -m pipeline.orchestrator gate-merge --run-dir /tmp/tp-run --project-root . --module core

# 7. 查看看板（含 git 状态）
python3 -m pipeline.orchestrator status --run-dir /tmp/tp-run --project-root .

# 8. 中断后恢复
python3 -m pipeline.orchestrator resume --run-dir /tmp/tp-run

# 9. 查看就绪队列
python3 -m pipeline.orchestrator next --run-dir /tmp/tp-run

# 10. 查看模块文件状态
python3 -m pipeline.orchestrator module-check --run-dir /tmp/tp-run --module core
```

## 使用 suggestions 接力的完整流程

`suggest-modules` 输出的 JSON 可直接消费进 `start`，减少人工转录：

```bash
# 1. 生成建议
python3 -m pipeline.orchestrator suggest-modules \
  --requirement "build a CLI tool with configuration and diagnostics" \
  --output suggestions.json

# 2. 审查 suggestions.json，将 approved 设为 true，然后启动
python3 -m pipeline.orchestrator start \
  --project-root . --run-dir /tmp/tp-run \
  --suggestions suggestions.json --git-auto
```

## 冒烟测试

```bash
TMPDIR=$(mktemp -d)
python3 -m pipeline.orchestrator start \
  --project-root . --run-dir "$TMPDIR" \
  --module mod_a --module mod_b \
  --edge mod_b:mod_a

# 验证输出物
test -f "$TMPDIR/contract.yaml" && echo "PASS: contract.yaml"
test -f "$TMPDIR/spec.md" && echo "PASS: spec.md"
test -f "$TMPDIR/conventions.md" && echo "PASS: conventions.md"
test -f "$TMPDIR/tasks.jsonl" && echo "PASS: tasks.jsonl"
test -f "$TMPDIR/dashboard.md" && echo "PASS: dashboard.md"
test -f "$TMPDIR/context_packets/mod_a.md" && echo "PASS: context_packet mod_a"
test -f "$TMPDIR/context_packets/mod_b.md" && echo "PASS: context_packet mod_b"

# 验证模块建议
python3 -m pipeline.orchestrator suggest-modules \
  --requirement "implement a CLI tool with config and diagnostics" \
  --output "$TMPDIR/suggestions.json" \
  && test -f "$TMPDIR/suggestions.json" && echo "PASS: suggest-modules"

# 验证看板
python3 -m pipeline.orchestrator status --run-dir "$TMPDIR" && echo "PASS: status"

# 验证就绪队列
python3 -m pipeline.orchestrator next --run-dir "$TMPDIR" && echo "PASS: next"

rm -rf "$TMPDIR"
echo "All smoke tests passed."
```

## 配置

- 默认配置文件：`pipeline.config.yaml`
- 可用 `--config` 覆盖默认配置
- 关键配置节点：
  - `orchestrator.token_budget` — 上下文包 token 预算
  - `orchestrator.micro_batch_tests` — 微批次测试设置
  - `orchestrator.quality_gates.coverage` — 覆盖率目标与质量门控
  - `orchestrator.feature_flags` — Feature Flag 策略
  - `modules` / `dependencies` — 模块与依赖声明（可通过 CLI 覆盖）
  - `commands` — 测试钩子（unit_tests / integration_tests / cdc_tests）
  - `git` — Git 自动化策略（auto_create_pr / merge_strategy / require_ci_pass）

## Git 自动化

内置 `GitManager` 提供幂等的 Git/GitHub 操作：

- `start --git-auto` — 自动为每个模块创建 `module/<name>` 分支并推送
- `module-ship` — scoped commit（限制模块路径所有权）+ push + 可选自动 PR
- `gate-merge` — 守门合并：CI 检查 → squash merge → 删除分支 → 刷新本地 main
- 所有操作先检查状态再变更，安全可重入

## 职责边界

tree-pipeline 是总统筹层，不替代模块 Agent。详见 [SKILL.md](SKILL.md#职责边界做什么--不做什么)。

## 目录结构

```
tree-pipeline/
  SKILL.md                  # Claude Code Skill 定义
  README.md
  ARCHITECTURE.md           # 多分支多会话开发工作流架构
  REFACTOR_PLAN.md          # 架构转型路线图（P0/P1/P2）
  EFFICIENCY_REVIEW.md      # 效率审阅与改进建议
  pyproject.toml
  pipeline.config.yaml
  templates/                # Spec/Contract/Conventions 模板
  pipeline/                 # 编排器核心代码
    orchestrator.py         # CLI 入口 + Orchestrator API
    task_ledger.py          # JSONL 任务账本 + 双状态机
    dashboard.py            # 看板生成器
    git_manager.py          # Git/GitHub 自动化层
    module_discovery.py     # 需求到模块建议引擎
    context_packet.py       # 上下文包构建与 token 预算
    graph.py                # DAG 拓扑分批
    runners/                # 测试运行器
    validators/             # 契约校验器
  artifacts/                # 预留产物目录
  tests/                    # 测试
```

## 免责声明

该实现不绑定某个具体语言/框架的构建系统；测试、CDC、集成脚本均以"可配置命令钩子"形式提供。
