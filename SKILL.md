# tree-pipeline（树形并行开发流水线）- Claude Code Skill

本 Skill 用于把“需求输入 → 规格冻结 → DAG 批次并行实现 → 契约验证/CDC/集成测试 → 看板汇总”的流程标准化，适合多模块/多子系统并行开发的工程任务。

## 触发方式

- 在对话中说：`tree-pipeline` 或 “树形并行流水线”
- 或者直接让 Claude 执行：`python -m pipeline.orchestrator start ...`

## 适用场景

- 需求较大，且可拆分为多个模块（有明确依赖关系）
- 希望先冻结契约（`contract.yaml`），再并行编码
- 希望用任务账本（`tasks.jsonl`）+ 看板（`dashboard.md`）做断点续跑与审计
- 需要“微批次测试（2-5 个函数一组）”与“风险驱动覆盖率（80-90% 行覆盖 + 场景清单 + CDC）”

## 二层架构（关键决策）

- 主编排器：`pipeline/orchestrator.py`（生成 Spec + 构建 DAG 批次 + 冻结契约 + 汇总看板）
- 模块 Agent：由主编排器输出“模块任务包”（context packet）与执行指令，Claude 作为模块 Agent 按包内上下文实现与自测

## 使用步骤（推荐）

1. 在你的项目工作区准备一个“模块清单 + 依赖边”：
   - 最简单：在命令行参数里传 `--module` 与 `--edge`
   - 或者把模块与依赖写进 `pipeline.config.yaml` 并用 `--config` 指定
2. 启动流水线（生成 `contract.yaml`/`spec.md`/`tasks.jsonl`/`dashboard.md`）：
   - `python -m pipeline.orchestrator start --project-root <你的项目根目录> --run-dir <本次运行目录>`
3. 按 DAG 批次并行执行（Batch1/Batch2...）：
   - 编排器会为每个模块生成 `context_packet.md`（最小上下文包）
   - 你可以把该包发给 Claude（模块 Agent），并让其按包内指令完成模块实现 + 微批次单测
4. 完成后运行验证阶段：
   - `python -m pipeline.orchestrator validate --run-dir <本次运行目录>`
5. 任何时候查看状态：
   - `python -m pipeline.orchestrator status --run-dir <本次运行目录>`
6. 中断后恢复：
   - `python -m pipeline.orchestrator resume --run-dir <本次运行目录>`

## 依赖工具

- Python 3.10+（建议 3.11）
- 可选：`PyYAML`（用于解析 `contract.yaml`；未安装会给出明确错误）
- 可选：测试工具（由 `pipeline.config.yaml` 配置）
  - `pytest`、`coverage`、`requests`、你的项目自带测试脚本等

## 职责边界（做什么 / 不做什么）

tree-pipeline 是**多模块并行开发的总统筹层**，不是单模块实现者：

| 负责                            | 不负责           | 委托给                      |
| ------------------------------- | ---------------- | --------------------------- |
| Spec 生成 + 契约冻结 + DAG 分批 | 详细实现方案叙事 | `planner` agent             |
| JSONL 任务账本 + 状态机管理     | 模块级代码实现   | 模块 Agent (Claude)         |
| Context Packet 构建与分发       | 代码审查         | `code-reviewer` agent       |
| 契约校验（签名 + semver）       | TDD 测试编写     | `tdd-guide` agent           |
| dashboard.md 看板汇总           | Git 提交/PR 流程 | `prp-commit` / git-workflow |
| 测试钩子触发                    | CI/CD 部署       | 项目自身 CI 系统            |
| 中断恢复（resume）              | Mutation Testing | 夜间 CI（仅预留钩子）       |

## 安装

```bash
# 克隆并安装为可全局调用的 Python 包
git clone git@github.com:Xwj-art/tree-pipeline.git ~/.claude/skills/tree-pipeline
pip install -e ~/.claude/skills/tree-pipeline

# 验证安装
python -m pipeline.orchestrator --help
```

## 输出物（run-dir 下）

- `contract.yaml`：版本化契约（带 `version` 字段）
- `spec.md`：冻结规格（含模块拆分、依赖、风险、验收标准）
- `conventions.md`：工程约定（命名/测试/回滚/Feature Flags）
- `tasks.jsonl`：任务账本（标准状态机，支持断点续跑）
- `dashboard.md`：人类可读看板（按状态汇总 + 下一步建议）
- `context_packets/<module>.md`：每个模块的最小上下文包
