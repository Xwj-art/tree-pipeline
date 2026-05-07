# MODULE_DIVISION_ANALYSIS

## 1. 现状分析 (Current State)

从当前代码看，tree-pipeline 已经具备“模块一旦确定，就可以按依赖图分批、按模块并行、按文件继续细分”的完整后半段流水线，但它并不负责“从一句需求里推导模块”。`pipeline/orchestrator.py` 的 `start()` 接收的 `modules` 与 `edges` 都是外部已经准备好的输入，然后立刻进入 `build_topological_batches()`、`create_tasks()`、`_write_context_packets()`、`write_run_manifest()` 这些后续动作，不包含任何模块候选生成逻辑，也没有对需求文本做语义解析的入口（见 [pipeline/orchestrator.py](/Users/xiaowenjie/tree-pipeline/pipeline/orchestrator.py:183)-[240](/Users/xiaowenjie/tree-pipeline/pipeline/orchestrator.py:240)）。CLI 层面上，`start` 仍然是通过 `--module` / `--edge` 传入，或者回落到 `pipeline.config.yaml` 里的 `modules.items` 和 `dependencies.edges`；如果 CLI 没给模块，它只会从配置中读取，并不会做自动拆分（见 [pipeline/orchestrator.py](/Users/xiaowenjie/tree-pipeline/pipeline/orchestrator.py:875)-[890](/Users/xiaowenjie/tree-pipeline/pipeline/orchestrator.py:890) 以及模块读取逻辑 [807](/Users/xiaowenjie/tree-pipeline/pipeline/orchestrator.py:807)-[848](/Users/xiaowenjie/tree-pipeline/pipeline/orchestrator.py:848)）。

配置模板也印证了这个边界：`pipeline.config.yaml` 只提供一个空的 `modules.items` 列表，以及一个空的 `dependencies.edges` 列表，示例也是假设用户已经知道 `core/api/ui` 这样的模块名（见 [pipeline.config.yaml](/Users/xiaowenjie/tree-pipeline/pipeline.config.yaml:43)-[59](/Users/xiaowenjie/tree-pipeline/pipeline.config.yaml:59)）。换句话说，tree-pipeline 的默认假设是“模块边界由人先决定”，系统只负责消费这个决定。

`dispatch_files()` 进一步说明：它处理的是模块内文件级计划，而不是模块级规划。它要求输入一个已经写好的 `plan.json`，格式是 `{files: [{id, title, depends_on}]}`，然后创建文件级子任务、生成文件级 context packet，并把模块状态从 `planned/spec_ready` 推进到 `coding`（见 [pipeline/orchestrator.py](/Users/xiaowenjie/tree-pipeline/pipeline/orchestrator.py:456)-[533](/Users/xiaowenjie/tree-pipeline/pipeline/orchestrator.py:533)）。这里的能力是“模块内拆分”，不是“模块间拆分”。因此，如果上游模块划分本身有问题，`dispatch` 只会把错误更细粒度地放大。

`TaskLedger` 也没有弥补这个空缺。它对模块任务的建模是：`create_tasks(modules, dependencies)` 直接把每个模块注册成一个 module-level task，并把依赖边写入 `depends_on`；后续状态机围绕 `planned -> spec_ready -> coding -> ... -> done` 展开（见 [pipeline/task_ledger.py](/Users/xiaowenjie/tree-pipeline/pipeline/task_ledger.py:213)-[295](/Users/xiaowenjie/tree-pipeline/pipeline/task_ledger.py:295)）。它提供了很强的“模块既定之后的可追踪性”，但完全不提供“模块是否划得对”的判断机制。`SKILL.md` 的使用说明同样要求用户“准备模块清单 + 依赖边”，并把这一步放在运行前置条件中（见 [SKILL.md](/Users/xiaowenjie/tree-pipeline/SKILL.md:29)-[45](/Users/xiaowenjie/tree-pipeline/SKILL.md:45)）。因此，当前最大缺口不是并行执行，而是缺少一个“从自然语言需求到模块候选、依赖方向、初始契约”的辅助决策层。

## 2. 多角色视角分析 (Multi-Role Analysis)

### 2.1 产品经理视角 (Product Manager)

从产品经理视角看，用户输入的一句话需求通常不是技术分层描述，而是“业务能力 + 交互目标”的混合表达，例如“开发一个博客网站”“做一个电商后台管理系统”“做一个团队知识库带权限和搜索”。这类输入的天然结构更接近用户故事地图，而不是模块图。Jeff Patton 在《User Story Mapping》中强调应先按用户活动与业务流程识别骨架，再决定切片顺序；这对 tree-pipeline 很重要，因为如果系统直接把“博客网站”粗暴拆成 `frontend/backend/database`，虽然工程上常见，但并不一定对应用户真正感知到的能力点，如“文章发布”“评论”“搜索”“权限”“运营配置”。这会导致模块名技术味很重，却不能稳定映射验收标准。

结合当前实现，tree-pipeline 最适合的前置增强不是“自动拍脑袋给出 3 个模块”，而是先从一句话需求中抽取用户可见能力点，再把能力点聚类成模块候选。因为现有 `spec.md` 模板本身就包含 `module_breakdown`、`dependencies`、`acceptance_criteria` 等栏目（通过 `spec.md.tpl` 渲染，调用链见 [pipeline/orchestrator.py](/Users/xiaowenjie/tree-pipeline/pipeline/orchestrator.py:201)-[208](/Users/xiaowenjie/tree-pipeline/pipeline/orchestrator.py:208) 与 [738](/Users/xiaowenjie/tree-pipeline/pipeline/orchestrator.py:738)-[759](/Users/xiaowenjie/tree-pipeline/pipeline/orchestrator.py:759)），说明系统已经有一个承接模块说明的文档载体。缺的是“候选生成器”，而不是“结果存放处”。

产品上还必须考虑可干预性。GitHub Projects、Jira Advanced Roadmaps、Linear 这类产品都不会把需求自动拆分结果当成最终真理，而是把它作为可编辑建议。原因很简单：一句“做一个电商后台”在不同团队语境下，模块边界可能按“商品/订单/用户/营销”划，也可能按“前台 API / 管理后台 / 报表 / 权限平台”划。对于 tree-pipeline 来说，推荐做法应是“双层输出”：先给出 2 到 4 种模块划分候选，再显示每种方案的用户功能覆盖、预估依赖数、潜在共享模型冲突点，让用户确认或修改。否则自动划分一旦错，后面的 DAG、contract、并行分支都会基于错误前提高速推进，返工成本很高。

因此，产品经理视角下最关键的结论是：模块划分必须与用户可感知功能对齐，并且必须让用户看见、审查、修改结果。tree-pipeline 当前要求用户手工填 `--module` 或 `modules.items`，这虽然原始，但至少保留了人类裁决权；未来若要自动化，不能跳过“人看一眼”的步骤。更合理的产品形态是新增一个 `discover-modules` 或 `suggest-modules` 阶段，输入一句需求，输出模块候选、功能覆盖映射和依赖草图，再由用户确认进入 `start`。

### 2.2 架构师视角 (Architect)

从架构师视角看，模块划分的首要目标不是“数量好看”，而是把依赖方向定清楚，并尽量避免循环依赖。当前 `pipeline/graph.py` 已经把依赖语义定义得很明确：边的含义是 `consumer -> provider`，即消费者依赖提供者，拓扑分批以 provider 先、consumer 后为准；如果出现未知模块或环，就直接报错（见 [pipeline/graph.py](/Users/xiaowenjie/tree-pipeline/pipeline/graph.py:4)-[84](/Users/xiaowenjie/tree-pipeline/pipeline/graph.py:84)）。这说明 tree-pipeline 的执行层是“强依赖有向无环图”的。但现实瓶颈在于：系统目前没有帮助用户判定哪些依赖应该存在，更没有指导用户识别“伪依赖”和“共享模型导致的双向耦合”。

这类判断应借鉴 Eric Evans《Domain-Driven Design》中的 Bounded Context 概念。DDD 的重点不是把目录切得整齐，而是让语言、规则、数据含义在边界内自洽。对于 tree-pipeline，一句需求应该先被映射为几个候选上下文，例如博客系统可分为“内容创作”“内容分发”“用户互动”“后台运营”，然后再判断哪些上下文暴露契约、哪些只消费契约。Sam Newman 在《Building Microservices》中反复强调“服务边界围绕业务能力，而不是围绕部署幻想”，这同样适用于这里。否则用户很容易按技术栈切出 `frontend` / `backend` / `db` 三个模块，结果 contract 全都堆在一个共享 schema 上，DAG 形式上无环，语义上却高度耦合。

在契约定义上，我更倾向于“接口优先，数据模型次之”的策略。原因是 tree-pipeline 当前会在 `start()` 里生成一个极简 `contract.yaml`，每个模块只有 `provides` / `requires` 骨架，而且默认函数与 HTTP endpoint 都是空列表（见 [pipeline/orchestrator.py](/Users/xiaowenjie/tree-pipeline/pipeline/orchestrator.py:761)-[786](/Users/xiaowenjie/tree-pipeline/pipeline/orchestrator.py:786)）。这意味着当前 contract 更像占位符，而不是自动推导出的真实边界。如果模块划分阶段改成数据模型优先，团队通常会先共享一套核心 schema，再让多个模块围着它转，最后很容易形成“所有模块都能碰核心对象”的局面。相比之下，接口优先更容易把依赖方向固定为“谁提供能力、谁消费能力”，再由接口所需的数据子集反向收敛模型。

粒度标准上，不建议把 tree-pipeline 的“模块”直接等同于微服务，也不建议退化到目录级别。更合适的标准是“介于 Bounded Context 与可独立并行交付的组件之间”。Component-Based Software Engineering 的经验说明，组件边界应当同时考虑接口稳定性、变更频率和可替换性，而不是单纯看文件数量。对当前系统而言，模块是 DAG 节点、Git 分支单位、context packet 单位和 task ledger 主任务单位，所以它必须比单个目录粗，但又不能粗到包含大量互相独立的业务能力。架构师视角的结论是：tree-pipeline 需要一个专门的模块划分准则，把“业务能力边界 + 单向契约 + 无环依赖”固化为自动建议规则，否则现有拓扑调度再正确，也只是对人工输入的错误边界做精确执行。

### 2.3 开发工程师视角 (Developer)

从开发工程师视角看，模块划分失败最直接的表现不是概念不优雅，而是每天都在处理跨模块引用、共享文件冲突和计划文件失真。当前 tree-pipeline 的开发流程是：先有模块，再在模块内部通过 `dispatch_files()` 读取 `plan.json` 生成文件级子任务，每个文件子任务进入 `ready/planned/coding/testing/done` 状态机（见 [pipeline/orchestrator.py](/Users/xiaowenjie/tree-pipeline/pipeline/orchestrator.py:456)-[533](/Users/xiaowenjie/tree-pipeline/pipeline/orchestrator.py:533)，[pipeline/task_ledger.py](/Users/xiaowenjie/tree-pipeline/pipeline/task_ledger.py:368)-[476](/Users/xiaowenjie/tree-pipeline/pipeline/task_ledger.py:476)）。这个机制本身很适合“模块内再并行”，但前提是模块内部有高内聚，而模块之间只通过少量契约交互。如果模块切得过细，比如把“用户服务”再拆成 profile、avatar、settings、auth-token 四个模块，结果通常是每个文件任务都在跨分支 import 对方、同时改 shared schema，最后文件级并行带来的收益会被接口协调成本吃掉。

反过来，如果模块切得过粗，比如整个后台系统只做成一个 `admin` 模块，那么 `dispatch` 虽然还能拆文件，但此时模块分支太大，`plan.json` 会非常长，文件依赖会密集，`TaskLedger.create_sub_tasks()` 只靠 `depends_on` 串联文件级依赖就很快变得脆弱。Nx、Bazel、Turborepo 这类工程化工具的经验都说明，真正高效的并行不是“无限细分”，而是先找到稳定的包/项目边界，再在包内局部并行。tree-pipeline 当前已经拥有第二层能力，却缺第一层辅助，所以开发体验瓶颈不在 Worker，而在模块候选的前置决策。

还有一个现实问题是冲突热点。当前 `start()` 会生成统一的 `contract.yaml`，而 `_write_context_packets()` 会按模块抽取对应片段发给模块 Agent（见 [pipeline/orchestrator.py](/Users/xiaowenjie/tree-pipeline/pipeline/orchestrator.py:788)-[836](/Users/xiaowenjie/tree-pipeline/pipeline/orchestrator.py:836)）。这说明 contract 目前仍然是集中式文件。一旦模块划分不稳，多个模块很可能都想修改同一个 contract 区块、同一个基础模型、同一个 conventions 约束，Git 冲突会集中爆发在这些“共享上游”文件，而不是业务代码本身。Linux kernel 的 subsystem maintainership 和 Kubernetes 的 SIG/OWNERS 机制都说明，并行开发要想扩展，必须先把高争用的共享接口最小化，而不是只靠更快的合并。

开发者视角下还有一个关键建议：模块划分分析必须向下连到 `plan.json` 自动生成。现在 `dispatch` 假设模块 Agent 自己写 `plan.json`，这意味着模块划分正确与否、文件边界清晰与否，都会转化为 plan 的质量问题。未来应该新增“模块 -> 文件计划”的模板化生成器：根据模块候选类型自动产出常见文件骨架，如 `api/routes.py`、`service.py`、`repository.py`、`tests/test_*.py` 等，再允许模块 Agent 细修。这能把模块划分的收益传导到文件级并行，而不是停留在文档层。总之，开发工程师最关心的不是系统会不会说“这里有 4 个模块”，而是这 4 个模块能否自然映射为低冲突分支、低耦合 plan、低争用 contract。

### 2.4 测试/QA 视角 (QA)

从测试与 QA 视角看，模块划分决定的不是目录结构，而是测试边界、契约边界和缺陷归因边界。当前 `TaskLedger` 的主状态机把 `unit_tests`、`module_review`、`integrated`、`integration_tests` 分成独立阶段，已经暗示 tree-pipeline 采用的是“先模块内验证，再跨模块验证”的策略（见 [pipeline/task_ledger.py](/Users/xiaowenjie/tree-pipeline/pipeline/task_ledger.py:18)-[89](/Users/xiaowenjie/tree-pipeline/pipeline/task_ledger.py:89)）。但这个阶段模型只有在模块边界清晰时才成立：如果模块之间共享大量内部对象，那么所谓 unit test 很可能测的是半个系统；所谓 integration test 也无法准确知道应该覆盖哪条链路。

在实践中，最适合 tree-pipeline 参考的是 Consumer-Driven Contract Testing，尤其是 Pact 这一开源项目背后的方法论。Pact 的核心思想是：消费者先声明期望，提供者验证兼容性。对 tree-pipeline 而言，如果模块划分阶段能同时产出“模块依赖边 + 初始 provides/requires 契约”，那么 QA 就能据此自动生成第一版 contract test 框架，至少知道哪些模块对之间需要 consumer/provider 用例。否则当前 `contract.yaml` 只是空壳，占位有了，但测试信号并没有形成。

另外，Google Testing Blog 长期倡导的 Testing Pyramid 与后来的 Test Honeycomb 也提醒我们：测试层次必须与系统边界一致。对于 tree-pipeline，正确的模块划分应当让三层测试自然落位。第一层是模块内 unit tests，对应模块分支自测；第二层是 module-level integration tests，对应一个模块内部多个文件子任务收敛后的验证；第三层是跨模块 integration / CDC / end-to-end，对应 `dependencies.edges` 所定义的真实交互链路。当前配置模板里已经预留了 `integration_tests`、`cdc_tests` 等命令钩子（见 [pipeline.config.yaml](/Users/xiaowenjie/tree-pipeline/pipeline.config.yaml:88)-[99](/Users/xiaowenjie/tree-pipeline/pipeline.config.yaml:99)），说明系统在执行层为 QA 留了挂点，但没有给出“哪些模块对需要什么测试”的自动识别机制。

因此，QA 视角下最重要的增强点不是再多一个测试命令，而是把模块划分结果转成“测试拓扑”。具体做法可以是：模块建议阶段输出一个矩阵，列出每个模块的入口能力、依赖模块、关键共享实体、风险标签；然后根据边关系自动识别高价值跨模块测试，如 `订单 -> 库存 -> 支付`、`内容发布 -> 搜索索引`。这类自动识别比完全自动生成用例更重要，因为它先告诉 QA 哪里必须写集成测试。换言之，tree-pipeline 当前 QA 层的短板并不在测试执行器，而在上游没有把模块边界和依赖语义转化成可消费的测试范围。

## 3. 候选方案对比 (Candidate Approaches)

| 策略 | 核心做法 | 自动化程度 | 并行开发适配度 | 契约清晰度 | 依赖识别能力 | LLM 适配性 | 主要风险 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 方案 A：纯人工模块清单 | 用户手写 `--module` / `modules.items` 与 `edges` | 低 | 中，取决于用户经验 | 中，完全靠人补 contract | 低，系统只校验 DAG 合法性 | 高，最容易解释 | 高度依赖用户架构能力；不同人风格差异大 |
| 方案 B：LLM 基于一句话需求直接给模块 + 边 | 输入需求文本，直接生成模块候选、依赖边、模块说明 | 高 | 中高，速度快 | 中，若不要求结构化理由容易漂移 | 中，能猜测但不稳定 | 很高 | 容易幻觉式拆分；对复杂领域会给出貌似合理但不稳定的边界 |
| 方案 C：能力点抽取 + 规则聚类 + 人工确认 | 先抽功能点，再按业务能力/数据拥有权聚类，再生成模块草案 | 中高 | 高 | 高，便于同步生成 provides/requires | 高，因有显式聚类和依赖规则 | 高 | 初版规则设计成本高；需要交互确认界面 |
| 方案 D：代码脚手架模板驱动 | 根据常见系统类型（博客、电商、CMS）套用预置模块模板 | 中 | 高，成熟模板下效果好 | 中高，模板可预置 contract | 中 | 中高 | 模板外项目适配差；容易把陌生领域硬套成熟模板 |

对 tree-pipeline 最合适的不是 A，也不是“完全自由生成”的 B，而是 C。原因是当前系统后半段已经非常结构化：DAG、ledger、dispatch、contract、context packet 都要求输入稳定、可追踪、可复现。纯人工方案扩展性太差；纯 LLM 直出方案虽然看起来最自动，但会把最关键的边界决策建立在不可解释的文本生成上。能力点抽取 + 规则聚类 + 人工确认更适合 tree-pipeline，因为它既能把“一句话需求”转成结构化中间层，又能把最终裁决权留给用户。这也最容易向下衔接 `spec.md`、`contract.yaml`、`plan.json` 三个现有产物。

## 4. 薄弱环节排序 (Weakest Links Priority)

1. **模块候选生成缺失**  
   这是首要瓶颈。当前系统完全假设用户已知道模块，而真实世界里恰恰最难的是从需求提炼模块边界。没有这一步，后面的 DAG、分支并行和自动合并都会建立在人工拍板上。

2. **依赖边判定缺失**  
   `pipeline/graph.py` 只负责校验和拓扑排序，不负责推导依赖边。模块名即使勉强定了，边一旦画错，Batch 顺序、并行度、契约校验顺序都会失真。

3. **初始契约空壳化**  
   `_write_initial_contract()` 只生成空的 `provides/requires` 列表。这样虽然有 contract 文件，但没有真实契约信息，导致架构、开发、测试都只能靠后补。

4. **模块到文件计划的桥接不足**  
   `dispatch_files()` 要求已有 `plan.json`，说明模块层决策不能自然下传到文件层。结果是模块划分和文件并行之间有一段人工断层。

5. **测试拓扑未从模块拓扑推导**  
   配置里有 `integration_tests` / `cdc_tests` 钩子，但系统不根据模块边自动建议测试范围。QA 仍需要手工理解模块图。

6. **共享热点识别缺失**  
   当前没有机制提前识别 `contract.yaml`、基础模型、公共库这类高冲突文件，导致模块划分即便大体正确，也可能在共享上游处产生分支冲突。

## 5. 实施建议 (Implementation Recommendations)

1. **新增 `suggest-modules` 前置命令**  
   输入一句需求文本，输出结构化 JSON：`capabilities[]`、`module_candidates[]`、`dependency_candidates[]`、`open_questions[]`。这一步不要直接启动 run-dir，只负责建议。实现上可新增 `pipeline/module_discovery.py`，并让 CLI 先产生草案，再进入 `start`。

2. **采用“两阶段划分”而不是一步到位**  
   阶段一抽取用户可见能力点，如内容发布、评论、搜索、权限。阶段二按“业务能力拥有权 + 数据主责 + 变更频率”聚类成模块。这样可以把产品语言转成工程边界，减少 LLM 直接猜模块的漂移。

3. **为模块建议增加显式评分规则**  
   每个候选模块都输出评分项：边界清晰度、预估依赖数、共享模型风险、测试独立性、并行收益。这样用户看到的不是一份神秘答案，而是一份可辩论的拆分报告。

4. **在模块建议阶段同步生成初始 contract 草案**  
   不要等 `start()` 再写空 `provides/requires`。建议直接输出“模块提供什么能力、依赖什么能力”的第一版草案，再由 `start` 消费。这会把 contract 从占位符升级为真正的架构输入。

5. **让依赖边推导规则显式化**  
   定义基础规则：UI/应用层依赖领域服务；消费者依赖提供者；共享数据所有权只能属于一个模块；禁止双向边。把这些规则固化到建议器里，再交给 `build_topological_batches()` 做最终校验。

6. **把模块建议结果落到 `spec.md` 的中间稿**  
   在正式 `start` 之前先生成一个 `module_division.md` 或 `spec.draft.md`，列出模块说明、边关系、理由和待确认问题。用户确认后再写正式 `spec.md`、`contract.yaml`、`tasks.jsonl`。

7. **新增“模块到 plan.json”的骨架生成器**  
   当模块确定后，依据模块类型自动产出文件清单骨架，例如 service/repository/api/tests 组合，减少模块 Agent 手写 `plan.json` 的负担，并把模块质量直接传导到文件并行质量。

8. **基于依赖边自动建议 QA 范围**  
   从 `dependency_candidates` 自动生成 CDC/集成测试清单，如 `A -> B` 至少对应一个 consumer-provider contract test。这样模块划分完成时，测试边界也同步出现。

9. **识别共享热点并前置警告**  
   对每份候选方案额外输出“冲突热点预测”，例如公共 schema、contract 根文件、base model、shared utils。如果某方案热点过多，即便功能划分合理，也应降低推荐级别。

10. **最终采用“建议 + 人工确认 + 启动”的闭环**  
    不建议直接把一句话需求自动推进到 `start`。更稳妥的产品化路径是：`suggest-modules` 生成候选 -> 用户确认/修改 -> `start` 固化 DAG 和 ledger -> `dispatch` 进入文件级并行。这样既保留 tree-pipeline 的自动化优势，也把最高风险的决策留在最可控的环节。
