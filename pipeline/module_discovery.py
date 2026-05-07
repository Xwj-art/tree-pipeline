"""
Module discovery for natural-language requirements.

This module adds a domain-agnostic suggestion phase ahead of the existing
tree-pipeline execution flow. The discovery strategy is intentionally
two-stage:

1. Extract capability signals from the requirement text.
2. Cluster capabilities into module candidates by ownership, data shape,
   and change frequency.

The output is structured JSON-ready data suitable for user review before the
pipeline is started with concrete `--module` and `--edge` values.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Sequence, Tuple


@dataclass(frozen=True, slots=True)
class CapabilityTemplate:
    """A capability template used for rule-based discovery."""

    key: str
    name: str
    description: str
    ownership_hint: str
    data_shape: str
    change_frequency: str
    estimated_files: int
    keywords: Sequence[str]
    provides_tags: Sequence[str]
    requires_tags: Sequence[str]


@dataclass(frozen=True, slots=True)
class CapabilityRecord:
    """A normalized capability discovered from requirement text."""

    key: str
    name: str
    description: str
    ownership_hint: str
    data_shape: str
    change_frequency: str
    estimated_files: int
    source: str
    matched_keywords: Sequence[str]
    provides_tags: Sequence[str]
    requires_tags: Sequence[str]


@dataclass(frozen=True, slots=True)
class ModuleBundle:
    """Intermediate clustered module representation."""

    name: str
    description: str
    capabilities: Sequence[CapabilityRecord]
    primary_kind: str


CAPABILITY_TEMPLATES: Dict[str, CapabilityTemplate] = {
    "command_surface": CapabilityTemplate(
        key="command_surface",
        name="命令/工具入口",
        description="提供 CLI、脚本入口或开发者工具命令面。",
        ownership_hint="interface",
        data_shape="commands",
        change_frequency="high",
        estimated_files=6,
        keywords=("cli", "命令行", "tool", "工具", "shell", "terminal", "命令", "脚本"),
        provides_tags=("external_entry",),
        requires_tags=("domain_service", "config_store"),
    ),
    "api_surface": CapabilityTemplate(
        key="api_surface",
        name="外部接口入口",
        description="提供对外 API、RPC、协议入口或控制面。",
        ownership_hint="interface",
        data_shape="requests",
        change_frequency="high",
        estimated_files=8,
        keywords=("api", "rpc", "gateway", "网关", "接口", "服务端", "endpoint", "http"),
        provides_tags=("external_entry",),
        requires_tags=("domain_service", "auth_context", "config_store"),
    ),
    "auth_security": CapabilityTemplate(
        key="auth_security",
        name="认证与安全控制",
        description="处理鉴权、权限、身份、密钥或隔离策略。",
        ownership_hint="security",
        data_shape="security_state",
        change_frequency="medium",
        estimated_files=7,
        keywords=("auth", "认证", "鉴权", "权限", "security", "安全", "identity", "密钥", "token"),
        provides_tags=("auth_context",),
        requires_tags=("state_store",),
    ),
    "workflow_scheduling": CapabilityTemplate(
        key="workflow_scheduling",
        name="调度与编排",
        description="负责任务编排、批次推进、队列分发或流程控制。",
        ownership_hint="orchestration",
        data_shape="plans",
        change_frequency="high",
        estimated_files=9,
        keywords=("schedule", "scheduler", "调度", "编排", "workflow", "orchestr", "任务分发", "batch"),
        provides_tags=("workflow_orchestration",),
        requires_tags=("execution_slot", "state_store", "cluster_coordination"),
    ),
    "execution_runtime": CapabilityTemplate(
        key="execution_runtime",
        name="执行运行时",
        description="负责任务执行、作业运行、工作单元承载或运行时生命周期。",
        ownership_hint="execution",
        data_shape="runtime_state",
        change_frequency="high",
        estimated_files=10,
        keywords=("worker", "runtime", "executor", "执行", "运行时", "作业", "job", "engine"),
        provides_tags=("execution_slot",),
        requires_tags=("domain_service", "state_store", "config_store"),
    ),
    "cluster_coordination": CapabilityTemplate(
        key="cluster_coordination",
        name="集群协调",
        description="处理节点发现、领导选举、副本协调或分布式控制。",
        ownership_hint="platform",
        data_shape="cluster_state",
        change_frequency="medium",
        estimated_files=8,
        keywords=("distributed", "分布式", "cluster", "集群", "consensus", "raft", "leader", "replica"),
        provides_tags=("cluster_coordination",),
        requires_tags=("state_store",),
    ),
    "state_storage": CapabilityTemplate(
        key="state_storage",
        name="状态与持久化",
        description="负责状态存储、数据持久化、索引、缓存或模型注册。",
        ownership_hint="persistence",
        data_shape="state",
        change_frequency="medium",
        estimated_files=9,
        keywords=("storage", "store", "db", "database", "持久化", "存储", "状态", "缓存", "registry"),
        provides_tags=("state_store",),
        requires_tags=(),
    ),
    "integration_adapter": CapabilityTemplate(
        key="integration_adapter",
        name="外部集成适配",
        description="负责接入第三方系统、外设、外部协议或上下游平台。",
        ownership_hint="integration",
        data_shape="external_io",
        change_frequency="medium",
        estimated_files=8,
        keywords=("adapter", "connector", "integration", "集成", "第三方", "plugin", "驱动接入", "外部系统"),
        provides_tags=("external_adapter",),
        requires_tags=("domain_service", "config_store"),
    ),
    "observability": CapabilityTemplate(
        key="observability",
        name="可观测性",
        description="负责日志、指标、追踪、告警或运行诊断。",
        ownership_hint="observability",
        data_shape="telemetry",
        change_frequency="medium",
        estimated_files=6,
        keywords=("monitor", "监控", "metrics", "日志", "trace", "tracing", "telemetry", "观测", "告警"),
        provides_tags=("telemetry_sink",),
        requires_tags=(),
    ),
    "configuration": CapabilityTemplate(
        key="configuration",
        name="配置与策略",
        description="负责配置解析、策略装载、环境切换或 feature flag。",
        ownership_hint="platform",
        data_shape="configuration",
        change_frequency="medium",
        estimated_files=5,
        keywords=("config", "配置", "policy", "策略", "flag", "环境", "profile"),
        provides_tags=("config_store",),
        requires_tags=(),
    ),
    "domain_core": CapabilityTemplate(
        key="domain_core",
        name="领域核心逻辑",
        description="承载主要业务规则、核心算法或系统语义。",
        ownership_hint="domain",
        data_shape="domain_model",
        change_frequency="high",
        estimated_files=11,
        keywords=("core", "核心", "engine", "算法", "规则", "logic", "业务", "处理"),
        provides_tags=("domain_service", "domain_model"),
        requires_tags=("config_store",),
    ),
    "parser_frontend": CapabilityTemplate(
        key="parser_frontend",
        name="前端解析与语义分析",
        description="负责输入解析、语法树、语义检查或中间表示生成。",
        ownership_hint="compilation",
        data_shape="syntax_ir",
        change_frequency="high",
        estimated_files=10,
        keywords=("compiler", "编译器", "parser", "parse", "语法", "语义", "ast", "frontend"),
        provides_tags=("validated_ir",),
        requires_tags=("diagnostic_service", "config_store"),
    ),
    "code_generation": CapabilityTemplate(
        key="code_generation",
        name="后端生成与优化",
        description="负责优化、中间代码转换、代码生成或产物打包。",
        ownership_hint="compilation",
        data_shape="artifact_ir",
        change_frequency="high",
        estimated_files=10,
        keywords=("codegen", "backend", "生成", "优化", "optimiz", "link", "emit"),
        provides_tags=("compiled_artifact",),
        requires_tags=("validated_ir", "config_store"),
    ),
    "diagnostics": CapabilityTemplate(
        key="diagnostics",
        name="诊断与错误报告",
        description="负责错误定位、调试信息、故障诊断或健康检查。",
        ownership_hint="observability",
        data_shape="diagnostics",
        change_frequency="medium",
        estimated_files=5,
        keywords=("diagnostic", "debug", "错误", "诊断", "health", "lint", "check"),
        provides_tags=("diagnostic_service",),
        requires_tags=(),
    ),
    "device_control": CapabilityTemplate(
        key="device_control",
        name="设备控制与硬件抽象",
        description="负责寄存器访问、驱动控制、硬件抽象层或板级支持。",
        ownership_hint="hardware",
        data_shape="hardware_state",
        change_frequency="medium",
        estimated_files=9,
        keywords=("firmware", "embedded", "driver", "硬件", "固件", "设备", "寄存器", "bsp"),
        provides_tags=("hardware_control", "sensor_io"),
        requires_tags=("config_store",),
    ),
    "control_loop": CapabilityTemplate(
        key="control_loop",
        name="控制回路与实时决策",
        description="负责实时控制、闭环反馈、状态机或动作决策。",
        ownership_hint="execution",
        data_shape="control_state",
        change_frequency="high",
        estimated_files=8,
        keywords=("control", "实时", "feedback", "闭环", "状态机", "调节"),
        provides_tags=("control_decision",),
        requires_tags=("sensor_io", "state_store", "config_store"),
    ),
    "protocol_stack": CapabilityTemplate(
        key="protocol_stack",
        name="通信协议栈",
        description="负责消息协议、网络传输、总线通信或编解码。",
        ownership_hint="integration",
        data_shape="transport",
        change_frequency="medium",
        estimated_files=8,
        keywords=("protocol", "network", "bus", "通信", "消息", "socket", "串口", "can"),
        provides_tags=("transport_channel",),
        requires_tags=("hardware_control", "config_store"),
    ),
    "data_ingestion": CapabilityTemplate(
        key="data_ingestion",
        name="数据摄取",
        description="负责采集、抽取、抓取、接收或导入输入数据。",
        ownership_hint="integration",
        data_shape="raw_data",
        change_frequency="medium",
        estimated_files=7,
        keywords=("ingest", "etl", "extract", "stream", "采集", "摄取", "导入", "抓取"),
        provides_tags=("raw_dataset",),
        requires_tags=("external_adapter", "config_store"),
    ),
    "transform_processing": CapabilityTemplate(
        key="transform_processing",
        name="数据处理与转换",
        description="负责转换、清洗、聚合、特征构造或批流计算。",
        ownership_hint="domain",
        data_shape="processed_data",
        change_frequency="high",
        estimated_files=9,
        keywords=("transform", "processing", "pipeline", "clean", "aggregate", "转换", "清洗", "特征"),
        provides_tags=("processed_dataset",),
        requires_tags=("raw_dataset", "config_store"),
    ),
    "model_training": CapabilityTemplate(
        key="model_training",
        name="训练与模型构建",
        description="负责训练作业、实验流程、模型构建或参数优化。",
        ownership_hint="execution",
        data_shape="ml_model",
        change_frequency="high",
        estimated_files=10,
        keywords=("training", "train", "模型训练", "训练", "experiment", "fit", "学习"),
        provides_tags=("trained_model",),
        requires_tags=("processed_dataset", "execution_slot", "state_store"),
    ),
    "inference_serving": CapabilityTemplate(
        key="inference_serving",
        name="推理与在线服务",
        description="负责推理执行、在线预测、模型加载或结果返回。",
        ownership_hint="interface",
        data_shape="predictions",
        change_frequency="high",
        estimated_files=8,
        keywords=("inference", "serving", "推理", "预测", "在线服务", "model serving"),
        provides_tags=("inference_result",),
        requires_tags=("trained_model", "execution_slot", "auth_context"),
    ),
    "rendering": CapabilityTemplate(
        key="rendering",
        name="渲染输出",
        description="负责画面生成、渲染管线、视图合成或输出设备刷新。",
        ownership_hint="execution",
        data_shape="frames",
        change_frequency="high",
        estimated_files=10,
        keywords=("render", "renderer", "渲染", "frame", "graphics", "图形"),
        provides_tags=("frame_output",),
        requires_tags=("simulation_state", "asset_bundle", "config_store"),
    ),
    "simulation": CapabilityTemplate(
        key="simulation",
        name="模拟与世界状态",
        description="负责场景模拟、物理、规则推进或核心状态演化。",
        ownership_hint="domain",
        data_shape="world_state",
        change_frequency="high",
        estimated_files=10,
        keywords=("simulation", "physics", "gameplay", "模拟", "物理", "世界状态"),
        provides_tags=("simulation_state",),
        requires_tags=("config_store",),
    ),
    "asset_pipeline": CapabilityTemplate(
        key="asset_pipeline",
        name="资源与构建资产",
        description="负责资源导入、打包、转换、版本化或内容构建。",
        ownership_hint="tooling",
        data_shape="assets",
        change_frequency="medium",
        estimated_files=7,
        keywords=("asset", "资源", "打包", "import", "bundle", "content pipeline"),
        provides_tags=("asset_bundle",),
        requires_tags=("external_adapter", "config_store"),
    ),
}


BLUEPRINTS: Dict[str, Dict[str, Sequence[str]]] = {
    "distributed_system": {
        "keywords": ("distributed", "分布式", "cluster", "集群", "scheduler", "调度系统", "任务调度"),
        "capabilities": (
            "api_surface",
            "workflow_scheduling",
            "execution_runtime",
            "cluster_coordination",
            "state_storage",
            "observability",
            "configuration",
        ),
    },
    "compiler_toolchain": {
        "keywords": ("compiler", "编译器", "toolchain", "解析器", "语言处理"),
        "capabilities": (
            "command_surface",
            "parser_frontend",
            "code_generation",
            "diagnostics",
            "configuration",
        ),
    },
    "cli_tool": {
        "keywords": ("cli", "命令行", "tool", "工具", "terminal", "shell"),
        "capabilities": (
            "command_surface",
            "domain_core",
            "configuration",
            "diagnostics",
        ),
    },
    "library_sdk": {
        "keywords": ("library", "sdk", "库", "组件库", "framework", "框架"),
        "capabilities": (
            "domain_core",
            "configuration",
            "integration_adapter",
            "diagnostics",
        ),
    },
    "embedded_firmware": {
        "keywords": ("firmware", "embedded", "嵌入式", "固件", "driver", "驱动", "bsp"),
        "capabilities": (
            "device_control",
            "control_loop",
            "protocol_stack",
            "configuration",
            "diagnostics",
        ),
    },
    "data_pipeline": {
        "keywords": ("etl", "数据管道", "pipeline", "stream", "batch", "ingest", "数据处理"),
        "capabilities": (
            "data_ingestion",
            "transform_processing",
            "workflow_scheduling",
            "state_storage",
            "observability",
        ),
    },
    "ml_platform": {
        "keywords": ("machine learning", "ml", "模型训练", "训练", "推理", "inference", "serving"),
        "capabilities": (
            "data_ingestion",
            "transform_processing",
            "model_training",
            "inference_serving",
            "state_storage",
            "observability",
            "configuration",
        ),
    },
    "game_engine": {
        "keywords": ("game", "游戏", "engine", "渲染", "renderer", "physics"),
        "capabilities": (
            "simulation",
            "rendering",
            "asset_pipeline",
            "configuration",
            "diagnostics",
        ),
    },
}


CONNECTOR_PATTERN = re.compile(
    r"(?:,|，|/|;|；|\band\b|\bwith\b|\bplus\b|以及|并且|并|同时|支持|包含|负责)"
)


def discover_modules(requirement: str) -> Dict[str, Any]:
    """Discover module suggestions from a natural-language requirement."""

    normalized = _normalize_requirement(requirement)
    if not normalized:
        raise ValueError("Requirement text must not be empty")

    capabilities = _extract_capabilities(requirement=normalized)
    bundles = _cluster_capabilities(capabilities)
    dependency_candidates, open_questions = _derive_dependencies(bundles)
    shared_hotspots = _build_shared_hotspots(bundles)

    return {
        "capabilities": [_capability_to_dict(c) for c in capabilities],
        "module_candidates": [
            _module_to_dict(bundle, dependency_candidates) for bundle in bundles
        ],
        "dependency_candidates": dependency_candidates,
        "shared_hotspots": shared_hotspots,
        "open_questions": open_questions + _default_open_questions(requirement, bundles),
    }


def _normalize_requirement(requirement: str) -> str:
    """Normalize requirement text into a compact single line."""

    text = requirement.strip()
    text = re.sub(r"\s+", " ", text)
    return text


def _extract_capabilities(*, requirement: str) -> List[CapabilityRecord]:
    """Stage 1: extract capability signals from requirement text."""

    lowered = requirement.lower()
    discovered: Dict[str, CapabilityRecord] = {}

    def add_template(key: str, source: str, matched_keywords: Sequence[str]) -> None:
        tpl = CAPABILITY_TEMPLATES[key]
        existing = discovered.get(key)
        if existing is not None and existing.source == "explicit":
            return
        discovered[key] = CapabilityRecord(
            key=tpl.key,
            name=tpl.name,
            description=tpl.description,
            ownership_hint=tpl.ownership_hint,
            data_shape=tpl.data_shape,
            change_frequency=tpl.change_frequency,
            estimated_files=tpl.estimated_files,
            source=source,
            matched_keywords=tuple(sorted(set(matched_keywords))),
            provides_tags=tuple(tpl.provides_tags),
            requires_tags=tuple(tpl.requires_tags),
        )

    explicit_hits = 0
    for key, tpl in CAPABILITY_TEMPLATES.items():
        hits = [kw for kw in tpl.keywords if kw.lower() in lowered]
        if hits:
            explicit_hits += 1
            add_template(key, "explicit", hits)

    for blueprint in BLUEPRINTS.values():
        if any(kw.lower() in lowered for kw in blueprint["keywords"]):
            for cap_key in blueprint["capabilities"]:
                source = "explicit" if cap_key in discovered else "implied"
                add_template(cap_key, source, tuple())

    if not discovered:
        add_template("domain_core", "fallback", tuple())
        add_template("configuration", "fallback", tuple())
        add_template("diagnostics", "fallback", tuple())

    fragments = _extract_requirement_fragments(requirement)
    for fragment in fragments:
        if len(fragment) < 4:
            continue
        key = _capability_key_from_fragment(fragment)
        if key and key not in discovered:
            add_template(key, "implied", tuple())

    if "domain_core" not in discovered:
        add_template("domain_core", "implied", tuple())
    if "configuration" not in discovered:
        add_template("configuration", "implied", tuple())

    ordered = sorted(discovered.values(), key=lambda c: (_source_rank(c.source), c.name))

    # Keep discovery focused: avoid flooding with overly weak implied capabilities.
    if explicit_hits == 0 and len(ordered) > 6:
        ordered = ordered[:6]
    return ordered


def _extract_requirement_fragments(requirement: str) -> List[str]:
    """Extract coarse fragments from a one-sentence requirement."""

    cleaned = re.sub(r"^[^A-Za-z0-9\u4e00-\u9fff]+", "", requirement)
    parts = [p.strip() for p in CONNECTOR_PATTERN.split(cleaned) if p.strip()]
    return parts


def _capability_key_from_fragment(fragment: str) -> str | None:
    """Map a raw fragment to a generic capability when no exact keyword matched."""

    text = fragment.lower()
    generic_checks: Sequence[Tuple[str, str]] = (
        ("执行", "execution_runtime"),
        ("处理", "domain_core"),
        ("分析", "domain_core"),
        ("控制", "control_loop"),
        ("采集", "data_ingestion"),
        ("存储", "state_storage"),
        ("接口", "api_surface"),
        ("命令", "command_surface"),
        ("调度", "workflow_scheduling"),
        ("渲染", "rendering"),
        ("训练", "model_training"),
        ("推理", "inference_serving"),
        ("协议", "protocol_stack"),
        ("设备", "device_control"),
        ("集成", "integration_adapter"),
        ("安全", "auth_security"),
        ("监控", "observability"),
    )
    for signal, key in generic_checks:
        if signal in fragment or signal in text:
            return key
    return None


def _source_rank(source: str) -> int:
    """Rank capability confidence."""

    order = {"explicit": 0, "implied": 1, "fallback": 2}
    return order.get(source, 3)


def _cluster_capabilities(capabilities: Sequence[CapabilityRecord]) -> List[ModuleBundle]:
    """Stage 2: cluster capabilities by ownership, data, and change frequency."""

    keys = {cap.key for cap in capabilities}

    if {"parser_frontend", "code_generation"} & keys:
        return _cluster_compiler_like(capabilities)
    if {"model_training", "inference_serving"} & keys:
        return _cluster_ml_like(capabilities)
    if {"device_control", "control_loop"} & keys:
        return _cluster_embedded_like(capabilities)
    if {"simulation", "rendering"} & keys:
        return _cluster_game_like(capabilities)

    groups: Dict[str, List[CapabilityRecord]] = {}
    for cap in capabilities:
        groups.setdefault(cap.ownership_hint, []).append(cap)

    modules: List[ModuleBundle] = []
    for ownership, caps in groups.items():
        name, description, primary_kind = _module_identity_for_group(ownership, caps)
        modules.append(
            ModuleBundle(
                name=name,
                description=description,
                capabilities=tuple(sorted(caps, key=lambda c: c.name)),
                primary_kind=primary_kind,
            )
        )
    return sorted(modules, key=lambda m: (_module_kind_rank(m.primary_kind), m.name))


def _cluster_compiler_like(capabilities: Sequence[CapabilityRecord]) -> List[ModuleBundle]:
    """Cluster compiler/toolchain oriented capabilities."""

    frontend = [c for c in capabilities if c.key in {"parser_frontend", "diagnostics"}]
    backend = [c for c in capabilities if c.key in {"code_generation"}]
    cli = [c for c in capabilities if c.key in {"command_surface", "configuration"}]
    others = [c for c in capabilities if c not in frontend + backend + cli]
    bundles: List[ModuleBundle] = []
    if frontend:
        bundles.append(
            ModuleBundle(
                name="compiler-frontend",
                description="负责解析、语义检查与前端中间表示。",
                capabilities=tuple(frontend),
                primary_kind="compilation",
            )
        )
    if backend:
        bundles.append(
            ModuleBundle(
                name="compiler-backend",
                description="负责优化、后端生成与产物输出。",
                capabilities=tuple(backend),
                primary_kind="execution",
            )
        )
    if cli:
        bundles.append(
            ModuleBundle(
                name="tooling-interface",
                description="负责命令入口、配置装载与调用编排。",
                capabilities=tuple(cli),
                primary_kind="interface",
            )
        )
    if others:
        bundles.append(
            ModuleBundle(
                name="shared-domain-core",
                description="承载非前后端专属的核心规则。",
                capabilities=tuple(others),
                primary_kind="domain",
            )
        )
    return bundles


def _cluster_ml_like(capabilities: Sequence[CapabilityRecord]) -> List[ModuleBundle]:
    """Cluster ML/data oriented capabilities."""

    data = [c for c in capabilities if c.key in {"data_ingestion", "transform_processing"}]
    training = [c for c in capabilities if c.key in {"model_training"}]
    serving = [c for c in capabilities if c.key in {"inference_serving", "api_surface", "auth_security"}]
    platform = [c for c in capabilities if c.key in {"state_storage", "observability", "configuration"}]
    bundles: List[ModuleBundle] = []
    if data:
        bundles.append(
            ModuleBundle(
                name="data-preparation",
                description="负责数据接入、清洗与可复用数据集构造。",
                capabilities=tuple(data),
                primary_kind="integration",
            )
        )
    if training:
        bundles.append(
            ModuleBundle(
                name="training-runtime",
                description="负责训练流程、实验执行与模型产出。",
                capabilities=tuple(training),
                primary_kind="execution",
            )
        )
    if serving:
        bundles.append(
            ModuleBundle(
                name="inference-interface",
                description="负责推理入口、访问控制与结果交付。",
                capabilities=tuple(serving),
                primary_kind="interface",
            )
        )
    if platform:
        bundles.append(
            ModuleBundle(
                name="model-platform",
                description="负责模型状态、配置与运行观测。",
                capabilities=tuple(platform),
                primary_kind="persistence",
            )
        )
    return bundles


def _cluster_embedded_like(capabilities: Sequence[CapabilityRecord]) -> List[ModuleBundle]:
    """Cluster embedded/firmware capabilities."""

    hardware = [c for c in capabilities if c.key in {"device_control", "protocol_stack"}]
    control = [c for c in capabilities if c.key in {"control_loop"}]
    support = [c for c in capabilities if c.key in {"configuration", "diagnostics", "observability"}]
    others = [c for c in capabilities if c not in hardware + control + support]
    bundles: List[ModuleBundle] = []
    if hardware:
        bundles.append(
            ModuleBundle(
                name="hardware-abstraction",
                description="负责驱动、寄存器访问与通信协议接入。",
                capabilities=tuple(hardware),
                primary_kind="hardware",
            )
        )
    if control:
        bundles.append(
            ModuleBundle(
                name="control-runtime",
                description="负责控制回路、实时决策与执行编排。",
                capabilities=tuple(control),
                primary_kind="execution",
            )
        )
    if support:
        bundles.append(
            ModuleBundle(
                name="device-support",
                description="负责配置、诊断与运行可观测性。",
                capabilities=tuple(support),
                primary_kind="platform",
            )
        )
    if others:
        bundles.append(
            ModuleBundle(
                name="device-domain-core",
                description="负责设备业务规则与主语义。",
                capabilities=tuple(others),
                primary_kind="domain",
            )
        )
    return bundles


def _cluster_game_like(capabilities: Sequence[CapabilityRecord]) -> List[ModuleBundle]:
    """Cluster simulation/rendering capabilities."""

    simulation = [c for c in capabilities if c.key in {"simulation", "domain_core"}]
    rendering = [c for c in capabilities if c.key in {"rendering"}]
    assets = [c for c in capabilities if c.key in {"asset_pipeline"}]
    support = [c for c in capabilities if c.key in {"configuration", "diagnostics", "observability"}]
    bundles: List[ModuleBundle] = []
    if simulation:
        bundles.append(
            ModuleBundle(
                name="simulation-core",
                description="负责世界状态推进、规则演化与核心逻辑。",
                capabilities=tuple(simulation),
                primary_kind="domain",
            )
        )
    if rendering:
        bundles.append(
            ModuleBundle(
                name="rendering-runtime",
                description="负责渲染管线与输出呈现。",
                capabilities=tuple(rendering),
                primary_kind="execution",
            )
        )
    if assets:
        bundles.append(
            ModuleBundle(
                name="asset-build",
                description="负责资源导入、转换与打包。",
                capabilities=tuple(assets),
                primary_kind="tooling",
            )
        )
    if support:
        bundles.append(
            ModuleBundle(
                name="engine-support",
                description="负责配置、诊断与可观测性。",
                capabilities=tuple(support),
                primary_kind="platform",
            )
        )
    return bundles


def _module_identity_for_group(
    ownership: str, capabilities: Sequence[CapabilityRecord]
) -> Tuple[str, str, str]:
    """Choose module name/description for a generic ownership group."""

    mapping: Dict[str, Tuple[str, str, str]] = {
        "interface": ("external-interface", "负责对外入口、调用面与交互边界。", "interface"),
        "domain": ("domain-core", "负责主要领域规则、算法和核心语义。", "domain"),
        "execution": ("execution-runtime", "负责执行单元、运行时生命周期和工作负载承载。", "execution"),
        "orchestration": ("orchestration-control", "负责流程编排、调度和批次推进。", "orchestration"),
        "persistence": ("state-storage", "负责持久化状态、索引和数据所有权。", "persistence"),
        "integration": ("integration-adapters", "负责外部系统、协议或环境适配。", "integration"),
        "security": ("auth-security", "负责认证、授权和安全边界。", "security"),
        "observability": ("observability", "负责日志、指标、追踪与诊断。", "observability"),
        "platform": ("platform-support", "负责配置、平台控制或共享策略。", "platform"),
        "compilation": ("compilation-pipeline", "负责解析、分析和产物转换。", "compilation"),
        "hardware": ("hardware-abstraction", "负责硬件抽象与设备控制。", "hardware"),
        "tooling": ("build-tooling", "负责构建、资源处理或开发工具流程。", "tooling"),
    }
    if ownership in mapping:
        return mapping[ownership]
    names = ", ".join(c.name for c in capabilities[:2])
    return (
        "domain-core",
        f"负责 {names} 等能力的收敛与封装。",
        "domain",
    )


def _derive_dependencies(
    bundles: Sequence[ModuleBundle],
) -> Tuple[List[Dict[str, str]], List[str]]:
    """Derive candidate dependency edges from module contract drafts."""

    providers: Dict[str, List[str]] = {}
    for bundle in bundles:
        for tag in _bundle_provides(bundle):
            providers.setdefault(tag, []).append(bundle.name)

    edges: Dict[Tuple[str, str], str] = {}
    open_questions: List[str] = []
    for bundle in bundles:
        consumer = bundle.name
        for tag in _bundle_requires(bundle):
            candidates = [name for name in providers.get(tag, []) if name != consumer]
            if not candidates:
                open_questions.append(
                    f"模块 `{consumer}` 需要能力 `{tag}`，但当前建议中没有明确提供者；请确认是否需要新增模块或由 main 分支共享契约承载。"
                )
                continue
            provider = _choose_best_provider(consumer, candidates, bundles)
            if provider is None:
                continue
            if (provider, consumer) in edges:
                kept = _resolve_bidirectional_conflict(
                    consumer=consumer,
                    provider=provider,
                    bundles=bundles,
                )
                if kept is None:
                    open_questions.append(
                        f"模块 `{consumer}` 与 `{provider}` 之间出现双向耦合迹象，请重新确认边界或提升共享契约到 main 分支。"
                    )
                    del edges[(provider, consumer)]
                    continue
                if kept == (provider, consumer):
                    continue
                del edges[(provider, consumer)]
            edges[(consumer, provider)] = _reason_for_edge(bundle, tag, provider)

        for fallback_provider, reason in _fallback_edges(bundle, bundles):
            if fallback_provider == consumer:
                continue
            edge = (consumer, fallback_provider)
            if edge not in edges and (fallback_provider, consumer) not in edges:
                edges[edge] = reason

    discovered = [
        {"consumer": c, "provider": p, "reason": reason}
        for (c, p), reason in sorted(edges.items())
    ]
    return discovered, _dedupe_preserve_order(open_questions)


def _bundle_provides(bundle: ModuleBundle) -> List[str]:
    """Aggregate provided contract tags for a module bundle."""

    tags: List[str] = []
    for cap in bundle.capabilities:
        tags.extend(cap.provides_tags)
    return sorted(set(tags))


def _bundle_requires(bundle: ModuleBundle) -> List[str]:
    """Aggregate required contract tags for a module bundle."""

    tags: List[str] = []
    for cap in bundle.capabilities:
        tags.extend(cap.requires_tags)
    return sorted(set(tags))


def _choose_best_provider(
    consumer: str,
    candidates: Sequence[str],
    bundles: Sequence[ModuleBundle],
) -> str | None:
    """Choose a provider with the lowest architectural layer rank."""

    lookup = {bundle.name: bundle for bundle in bundles}
    ranked = sorted(
        candidates,
        key=lambda name: (_module_kind_rank(lookup[name].primary_kind), name),
    )
    return ranked[0] if ranked else None


def _resolve_bidirectional_conflict(
    *, consumer: str, provider: str, bundles: Sequence[ModuleBundle]
) -> Tuple[str, str] | None:
    """Resolve a bidirectional edge by preferring higher-level consumer -> lower-level provider."""

    lookup = {bundle.name: bundle for bundle in bundles}
    c_rank = _module_kind_rank(lookup[consumer].primary_kind)
    p_rank = _module_kind_rank(lookup[provider].primary_kind)
    if c_rank == p_rank:
        return None
    return (consumer, provider) if c_rank > p_rank else (provider, consumer)


def _reason_for_edge(bundle: ModuleBundle, tag: str, provider: str) -> str:
    """Build a human-readable reason for a dependency edge."""

    return (
        f"`{bundle.name}` 需要 `{tag}` 能力，当前由 `{provider}` 提供；按 consumer -> provider 规则建立依赖。"
    )


def _fallback_edges(
    bundle: ModuleBundle, bundles: Sequence[ModuleBundle]
) -> List[Tuple[str, str]]:
    """Add domain-agnostic fallback edges when contracts alone are too sparse."""

    by_kind = {b.primary_kind: b.name for b in bundles}
    out: List[Tuple[str, str]] = []
    if bundle.primary_kind == "interface":
        for kind in ("domain", "orchestration", "security"):
            if kind in by_kind:
                out.append((by_kind[kind], f"`{bundle.name}` 是入口层，默认依赖 `{by_kind[kind]}` 提供的核心能力。"))
                break
    if bundle.primary_kind == "orchestration":
        for kind in ("execution", "persistence", "domain"):
            if kind in by_kind:
                out.append((by_kind[kind], f"`{bundle.name}` 负责编排，默认消费 `{by_kind[kind]}` 的执行或状态能力。"))
    if bundle.primary_kind == "execution":
        for kind in ("domain", "persistence"):
            if kind in by_kind:
                out.append((by_kind[kind], f"`{bundle.name}` 负责执行，默认依赖 `{by_kind[kind]}` 的规则或状态。"))
    if bundle.primary_kind == "integration":
        for kind in ("domain", "persistence", "hardware"):
            if kind in by_kind:
                out.append((by_kind[kind], f"`{bundle.name}` 负责外部接入，默认依赖 `{by_kind[kind]}` 的边界契约。"))
                break
    if bundle.primary_kind == "security" and "persistence" in by_kind:
        out.append((by_kind["persistence"], f"`{bundle.name}` 通常需要状态存储承载身份或密钥映射。"))
    return out


def _build_shared_hotspots(bundles: Sequence[ModuleBundle]) -> List[Dict[str, Any]]:
    """Identify shared hotspots that should stay on the main branch."""

    names = [bundle.name for bundle in bundles]
    hotspots: List[Dict[str, Any]] = []
    if len(names) > 1:
        hotspots.append(
            {
                "file": "contract.yaml",
                "reason": "模块共享契约文件；应由 main 分支集中管理，模块分支只引用对应片段，不并行改写整体契约。",
                "affected_modules": names,
            }
        )

    tag_to_modules: Dict[str, List[str]] = {}
    for bundle in bundles:
        tags = set(_bundle_provides(bundle) + _bundle_requires(bundle))
        for tag in tags:
            tag_to_modules.setdefault(tag, []).append(bundle.name)

    shared_defs: Sequence[Tuple[str, str, str]] = (
        ("domain_model", "shared/base_models.*", "多个模块共享核心数据语义，基础模型应由 main 分支集中托管。"),
        ("config_store", "shared/config_schema.*", "多个模块消费统一配置语义，配置 schema 应集中维护。"),
        ("diagnostic_service", "shared/diagnostics_contract.*", "多模块依赖统一诊断输出格式，建议集中管理。"),
        ("transport_channel", "public/protocols/*", "通信协议会被多个模块共同引用，建议由 main 分支管理公共协议定义。"),
        ("validated_ir", "shared/intermediate_representation.*", "中间表示会跨模块流动，建议将共享 IR 定义放在主干共享层。"),
        ("trained_model", "shared/model_contracts/*", "模型产物格式会被训练与推理模块共同引用，建议集中定义。"),
    )
    for tag, file_name, reason in shared_defs:
        affected = sorted(set(tag_to_modules.get(tag, [])))
        if len(affected) > 1:
            hotspots.append(
                {
                    "file": file_name,
                    "reason": reason,
                    "affected_modules": affected,
                }
            )
    return hotspots


def _module_to_dict(
    bundle: ModuleBundle, dependency_candidates: Sequence[Dict[str, str]]
) -> Dict[str, Any]:
    """Serialize a module bundle into output JSON."""

    estimated_files = sum(cap.estimated_files for cap in bundle.capabilities)
    direct_deps = sum(1 for edge in dependency_candidates if edge["consumer"] == bundle.name)
    provides = _bundle_provides(bundle)
    requires = _bundle_requires(bundle)
    shared_model_risk = 5 if "domain_model" in provides and direct_deps >= 2 else 3
    boundary_clarity = 5 if len({c.ownership_hint for c in bundle.capabilities}) <= 2 else 3
    test_independence = 5 if bundle.primary_kind in {"persistence", "domain", "tooling"} else 3
    parallel_benefit = 5 if direct_deps <= 2 and boundary_clarity >= 4 else 3
    risk_score = shared_model_risk + max(1, direct_deps)
    risk_level = "high" if risk_score >= 8 else "medium" if risk_score >= 5 else "low"

    return {
        "name": bundle.name,
        "description": bundle.description,
        "capabilities": [cap.name for cap in bundle.capabilities],
        "estimated_files": estimated_files,
        "risk_level": risk_level,
        "score": {
            "boundary_clarity": boundary_clarity,
            "estimated_deps": direct_deps,
            "shared_model_risk": shared_model_risk,
            "test_independence": test_independence,
            "parallel_benefit": parallel_benefit,
        },
        "contract_draft": {
            "provides": {
                "functions": provides,
                "events": _events_from_tags(provides),
                "resources": _resources_from_tags(provides),
            },
            "requires": {
                "functions": requires,
                "events": _events_from_tags(requires),
                "resources": _resources_from_tags(requires),
            },
        },
    }


def _events_from_tags(tags: Sequence[str]) -> List[str]:
    """Derive event names from contract tags."""

    return [f"{tag}.updated" for tag in tags if tag.endswith("_state") or tag.endswith("_dataset")]


def _resources_from_tags(tags: Sequence[str]) -> List[str]:
    """Derive resource names from contract tags."""

    resources = []
    for tag in tags:
        if tag.endswith("_store") or tag.endswith("_bundle") or tag.endswith("_artifact"):
            resources.append(tag)
    return resources


def _capability_to_dict(cap: CapabilityRecord) -> Dict[str, Any]:
    """Serialize a capability record into output JSON."""

    return {
        "name": cap.name,
        "description": cap.description,
        "source": cap.source,
        "ownership_hint": cap.ownership_hint,
        "data_shape": cap.data_shape,
        "change_frequency": cap.change_frequency,
        "matched_keywords": list(cap.matched_keywords),
    }


def _module_kind_rank(kind: str) -> int:
    """Architectural rank used to orient dependency edges."""

    order = {
        "persistence": 0,
        "hardware": 0,
        "platform": 1,
        "domain": 2,
        "tooling": 2,
        "integration": 3,
        "security": 3,
        "execution": 4,
        "compilation": 4,
        "orchestration": 5,
        "interface": 6,
        "observability": 6,
    }
    return order.get(kind, 3)


def _default_open_questions(
    requirement: str, bundles: Sequence[ModuleBundle]
) -> List[str]:
    """Emit domain-agnostic follow-up questions for user review."""

    questions: List[str] = []
    if len(bundles) <= 2:
        questions.append("当前建议模块数较少；请确认需求是否还包含尚未显式写出的独立能力面。")
    if not any(bundle.primary_kind == "interface" for bundle in bundles):
        questions.append("需求文本未明显暴露入口层；请确认系统是以库/组件形式嵌入，还是仍需 CLI/API/UI 入口。")
    if "分布式" in requirement or "distributed" in requirement.lower():
        questions.append("如果这是分布式系统，请确认一致性、成员发现和故障恢复是否需要独立模块边界。")
    if "embedded" in requirement.lower() or "嵌入式" in requirement:
        questions.append("如果这是嵌入式/固件场景，请确认硬件抽象层与控制回路是否需要严格隔离为不同模块。")
    return _dedupe_preserve_order(questions)


def _dedupe_preserve_order(values: Iterable[str]) -> List[str]:
    """De-duplicate strings while preserving order."""

    seen = set()
    out: List[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out
