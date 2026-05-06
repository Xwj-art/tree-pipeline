# tree-pipeline

`tree-pipeline` 是一个用于“树形并行开发流水线”的轻量实现：以冻结契约（contract-first）为核心，用依赖图（DAG）分批并行推进模块开发，并以 `tasks.jsonl` + `dashboard.md` 实现可恢复、可审计的进度管理。

## 目标流程（三层架构）

需求输入 → **主编排器**（Spec + DAG + 契约冻结） → **模块 Agent** 并行 → **dispatch** → **Worker Agent × N** 文件级并行 → 验证 + 看板

## 关键策略

- 三层 Agent：主编排器 → 模块 Agent → Worker Agent（文件级并行）
- 微批次测试：2-5 个函数一组
- 模块内 DAG：文件间可声明依赖，Worker 按就绪顺序分发
- 风险驱动覆盖率：80-90% 行覆盖 + 场景清单 + CDC（不追求 100% 分支）
- 重构封顶：最多 1 轮；仍不通过则升级人工
- Selective Feature Flags：高风险路径用 Flag 包裹，支持快速回滚
- Mutation Testing：延期到夜间 CI（此仓库仅预留钩子）
- 契约版本化：`contract.yaml` 包含 `version`
- 上下文包：仅分发“最小上下文”（硬 Token 预算）
- 回滚策略：关 Flag → revert 合并 → 修复 PR → 冻结下游
- 进度看板：`tasks.jsonl` + `dashboard.md`，标准状态机

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

## 快速开始

```bash
# 1. 创建工作目录
mkdir -p /tmp/tp-run

# 2. 启动流水线（3 个模块，UI→API→Core 依赖链）
python3 -m pipeline.orchestrator start \
  --project-root . \
  --run-dir /tmp/tp-run \
  --module api --module core --module ui \
  --edge ui:api --edge api:core

# 3. 模块 Agent 拆分文件并分发 Worker
echo '{"files":[{"id":"src/models.py","title":"Data models","depends_on":[]},{"id":"src/service.py","title":"Business logic","depends_on":["src/models.py"]}]}' > /tmp/plan.json
python3 -m pipeline.orchestrator dispatch --run-dir /tmp/tp-run --module api --plan /tmp/plan.json

# 4. 查看 dashboard（含 File Workers）
cat /tmp/tp-run/dashboard.md

# 5. 执行契约校验
python3 -m pipeline.orchestrator validate \
  --run-dir /tmp/tp-run --project-root .

# 6. 中断后恢复
python3 -m pipeline.orchestrator resume --run-dir /tmp/tp-run
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

# 验证契约校验
python3 -m pipeline.orchestrator validate --run-dir "$TMPDIR" --project-root . \
  && echo "PASS: contract validate"

# 验证状态
python3 -m pipeline.orchestrator status --run-dir "$TMPDIR" \
  --module mod_a --module mod_b --edge mod_b:mod_a \
  && echo "PASS: status"

rm -rf "$TMPDIR"
echo "All smoke tests passed."
```

## 配置

- 默认配置文件：`pipeline.config.yaml`
- 可用 `--config` 覆盖默认配置

## 职责边界

tree-pipeline 是总统筹层，不替代模块 Agent。详见 [SKILL.md](SKILL.md#职责边界做什么--不做什么)。

## 目录结构

```
tree-pipeline/
  SKILL.md            # Claude Code Skill 定义
  README.md
  pyproject.toml
  pipeline.config.yaml
  templates/          # Spec/Contract/Conventions 模板
  pipeline/           # 编排器核心代码
  artifacts/          # 预留产物目录
```

## 免责声明

该实现不绑定某个具体语言/框架的构建系统；测试、CDC、集成脚本均以“可配置命令钩子”形式提供。
