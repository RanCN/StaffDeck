"""Seed the ft-loong posts and their tao-tie MCP server.

The checkout path comes from ``FT_LOONG_ROOT``. Commands and arguments stay
relative to that directory, so each machine can keep the repository somewhere
else without changing code. An empty or unusable path skips this seed.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any

from sqlmodel import Session, select

from app.agents.branching import (
    _ensure_knowledge_branch,
    ensure_knowledge_base_version,
    ensure_private_resource_binding,
    mark_resource_private_for_agent,
)
from app.config import get_settings
from app.db.models import (
    AgentModelBinding,
    AgentProfile,
    KnowledgeBase,
    KnowledgeDocument,
    MCPServer,
    ModelConfig,
    Tenant,
    Tool,
    utc_now,
)

logger = logging.getLogger(__name__)

TENANT_ID = "tenant_demo"
MCP_NAME = "tao-tie-review"
MCP_ARGS = ["run", "--directory", "services/tao-tie/backend", "python", "-m", "mcp"]
MCP_SYNC_TIMEOUT_SECONDS = 60.0
STAFF_ROOT = Path(".agents/skills/digital-employee/staff")

TRADER_PERSONA = """你是 ft-loong 的交易员。工位名是 交易员-staffdeck。每次调用 tao-tie 工具都必须带 seat=交易员-staffdeck 和 sessionId=7c2a9e14-6b31-4f0a-9d55-1a2b3c4d5e01。不要使用交易员-01，不要读写 .post-token。

职责：按派活点名的模式交一份决策，并在同一份材料里写下可执行参数。没点名模式时用 rsi。决策只有一份。执行分三条线：dry-run 试运行、pro 生产、back 回测。先决策，再按点名的那一条执行。本对话自己交稿、入库、开跑、验收，不派子会话。

轮次：先依据知识库里的岗位页和经验。这一轮只打开轮次表点名的那一页。
- 交决策、今天决策、RSI 开跑、按岗位、补数，还没入库：交稿。参数再看当前模式的参数页。
- 用户说了「开」且没点名 pro：开，执行页是 dry-run。
- 用户说了「开」且点名 pro：开，执行页是 pro。
- 用户说了「回测」并给出日期：回测。
- 用户说了「思考链对照」并点名至少两份已交决策：思考链对照。
- 用户说了「验收」或「复盘」：验收。
- 被叫醒写回测复盘：复盘。

边界：不改仓库、不改策略源码、不改 modules、不给 QQ 发消息、不碰别的单。不做 hyperopt，不保存因子参数。不许弹选择题或停在半路等人点选。一轮必须结轮。「开」没点名走 dry-run。只有用户明确点名 pro 才调用 desk_launch_pro。不要两条执行线一起拉。

StaffDeck 不会把决策 Markdown 或 notes 写进 ft-loong 的 workspace/quants。入库、开跑、验收通过已绑定的 MCP 工具完成，并在回复里给出 decisionId、launchId 和卡点。"""

PRODUCT_PERSONA = """你是 ft-loong 的产品。工位名是 产品-staffdeck。每次调用 tao-tie 工具都必须带 seat=产品-staffdeck 和 sessionId=7c2a9e14-6b31-4f0a-9d55-1a2b3c4d5e02。不要读写 .post-token。

职责：提出战略和需求，用户确认后跟到能用。战略只写平台后续方向。一件一次能做完的事写成需求。不改策略源码，不跑回测，不代替交易员选币。不调用 desk_launch_dryrun、desk_launch_pro、desk_launch_backtest，不启停容器，不改 pro。

轮次：先依据知识库里的岗位页和经验。这一轮只打开轮次表点名的那一页。
- 写战略，还没确认：战略。
- 写需求，还没确认：需求。
- 用户说了「确认」：跟进。
- 一份需求验收通过：复盘。
- 用户说了「跟进」「今天产品」「产品进度」：跟进。

边界：用户说「确认」之前不改代码。不主动 commit。实盘、删数据、改 pro，或需求里写明要拍板的那一句，才再停。只要 3 到 5 个方案对比，不在本岗位里展开对比。

在 StaffDeck 里不能按原页修改 ft-loong 仓库，也不能派 Cursor 子对话。仓库文件（spec/brooks、workspace/产品 下的战略）和子对话不在本系统执行。你可以整理战略、需求和进度，并说明这些文件需要在 ft-loong 仓库里落盘。"""

POSTS: tuple[dict[str, str], ...] = (
    {
        "key": "trader",
        "name": "交易员",
        "description": "ft-loong 交易员，通过 tao-tie 交决策、开跑和验收。",
        "knowledge_name": "交易员岗位",
        "knowledge_description": "ft-loong 交易员岗位页、经验和在岗轮次。",
        "staff_dir": "交易员",
        "persona": TRADER_PERSONA,
    },
    {
        "key": "product",
        "name": "产品",
        "description": "ft-loong 产品，整理战略和需求；仓库文件与子对话不在 StaffDeck 执行。",
        "knowledge_name": "产品岗位",
        "knowledge_description": "ft-loong 产品岗位页、经验和在岗轮次。",
        "staff_dir": "产品",
        "persona": PRODUCT_PERSONA,
    },
)


def seed_ft_loong(session: Session, *, sync_tools: bool = True) -> None:
    """Upsert the tao-tie MCP server, the two posts, and their knowledge."""
    root = _ft_loong_root()
    if root is None:
        return
    if not session.get(Tenant, TENANT_ID):
        session.add(Tenant(id=TENANT_ID, name="Demo Enterprise"))
        session.flush()

    server = _upsert_mcp_server(session, root)
    agents = [_upsert_agent(session, post) for post in POSTS]
    for post, agent in zip(POSTS, agents, strict=True):
        _bind_default_model(session, agent)
        _sync_post_knowledge(session, root, agent, post)
    session.commit()
    if sync_tools:
        _sync_mcp_tools(session, server, agents)
        session.commit()


def _ft_loong_root() -> Path | None:
    settings = get_settings()
    raw = settings.ft_loong_root.strip()
    if not raw:
        logger.debug("FT_LOONG_ROOT 未配置，跳过 ft-loong 种子数据。")
        return None
    root = Path(raw).expanduser().resolve()
    staff = root / STAFF_ROOT
    backend = root / "services" / "tao-tie" / "backend"
    if not root.is_dir() or not staff.is_dir() or not backend.is_dir():
        logger.warning("FT_LOONG_ROOT=%s 不是可用的 ft-loong 仓库，跳过种子数据。", root)
        return None
    return root


def _taotie_base_url() -> str:
    raw = get_settings().taotie_base_url.strip() or "http://127.0.0.1:8000"
    return raw.rstrip("/")


def _upsert_mcp_server(session: Session, root: Path) -> MCPServer:
    server = session.exec(
        select(MCPServer).where(MCPServer.tenant_id == TENANT_ID, MCPServer.name == MCP_NAME)
    ).first()
    connection = {
        "transport": "stdio",
        "command": "uv",
        "args_json": list(MCP_ARGS),
        "env_json": {"PYTHONPATH": "..", "TAOTIE_BASE_URL": _taotie_base_url()},
        "cwd": str(root),
        "enabled": True,
    }
    if server is None:
        server = MCPServer(
            tenant_id=TENANT_ID,
            name=MCP_NAME,
            display_name="tao-tie",
            description="ft-loong tao-tie 工位工具。工作目录取本机 FT_LOONG_ROOT。",
            bucket="MCP 工具",
            **connection,
        )
        session.add(server)
    else:
        for key, value in connection.items():
            setattr(server, key, value)
        server.updated_at = utc_now()
        session.add(server)
    session.flush()
    return server


def _upsert_agent(session: Session, post: dict[str, str]) -> AgentProfile:
    agent = _find_agent(session, post)
    metadata = {
        "ft_loong_post": post["key"],
        "seed_source": "ft_loong",
    }
    if agent is None:
        agent = AgentProfile(
            tenant_id=TENANT_ID,
            name=post["name"],
            description=post["description"],
            persona_prompt=post["persona"],
            status="active",
            metadata_json=metadata,
        )
        session.add(agent)
    else:
        agent.name = post["name"]
        agent.description = post["description"]
        agent.persona_prompt = post["persona"]
        agent.status = "active"
        agent.metadata_json = {**(agent.metadata_json or {}), **metadata}
        agent.updated_at = utc_now()
        session.add(agent)
    session.flush()
    return agent


def _find_agent(session: Session, post: dict[str, str]) -> AgentProfile | None:
    agents = session.exec(
        select(AgentProfile).where(
            AgentProfile.tenant_id == TENANT_ID,
            AgentProfile.status != "archived",
        )
    ).all()
    for agent in agents:
        if (agent.metadata_json or {}).get("ft_loong_post") == post["key"]:
            return agent
    for agent in agents:
        if agent.name == post["name"] and not (agent.metadata_json or {}).get("ft_loong_post"):
            return agent
    return None


def _bind_default_model(session: Session, agent: AgentProfile) -> None:
    existing = session.exec(
        select(AgentModelBinding).where(
            AgentModelBinding.tenant_id == TENANT_ID,
            AgentModelBinding.agent_id == agent.id,
            AgentModelBinding.role == "default",
        )
    ).first()
    if existing is not None:
        return
    model = session.exec(
        select(ModelConfig).where(
            ModelConfig.tenant_id == TENANT_ID,
            ModelConfig.is_default == True,
            ModelConfig.enabled == True,
        )
    ).first()
    if model is None:
        return
    session.add(
        AgentModelBinding(
            tenant_id=TENANT_ID,
            agent_id=agent.id,
            role="default",
            model_config_id=model.id,
        )
    )


def _sync_post_knowledge(
    session: Session, root: Path, agent: AgentProfile, post: dict[str, str]
) -> None:
    knowledge_base = _ensure_knowledge_base(session, agent, post)
    branch = _ensure_knowledge_branch(session, TENANT_ID, agent.id, knowledge_base)
    session.flush()
    version = ensure_knowledge_base_version(session, knowledge_base, branch.head_version)
    staff_dir = root / STAFF_ROOT / post["staff_dir"]
    if not staff_dir.is_dir():
        logger.warning("ft-loong 岗位目录不存在：%s", staff_dir)
        return
    for path in _staff_markdown(staff_dir):
        relative = path.relative_to(staff_dir).as_posix()
        text = path.read_text(encoding="utf-8")
        if not text.strip():
            continue
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        document = _ready_document(session, knowledge_base.id, version.id, relative)
        if document is None:
            document = KnowledgeDocument(
                tenant_id=TENANT_ID,
                knowledge_base_id=knowledge_base.id,
                knowledge_base_version_id=version.id,
                filename=relative,
                file_type="md",
                title=relative,
                status="processing",
                metadata_json={"ft_loong_source": relative, "ft_loong_sha256": digest},
            )
            session.add(document)
            session.flush()
            _replace_document(session, document, text, relative)
            continue
        stored = str((document.metadata_json or {}).get("ft_loong_sha256") or "")
        if stored == digest:
            continue
        metadata = dict(document.metadata_json or {})
        metadata["ft_loong_source"] = relative
        metadata["ft_loong_sha256"] = digest
        document.metadata_json = metadata
        document.updated_at = utc_now()
        session.add(document)
        if stored or document.status != "ready" or not document.chunk_count:
            _replace_document(session, document, text, relative)


def _ensure_knowledge_base(
    session: Session, agent: AgentProfile, post: dict[str, str]
) -> KnowledgeBase:
    knowledge_base = session.exec(
        select(KnowledgeBase).where(
            KnowledgeBase.tenant_id == TENANT_ID,
            KnowledgeBase.name == post["knowledge_name"],
        )
    ).first()
    if knowledge_base is None:
        knowledge_base = KnowledgeBase(
            tenant_id=TENANT_ID,
            name=post["knowledge_name"],
            description=post["knowledge_description"],
            status="active",
        )
        session.add(knowledge_base)
        session.flush()
    mark_resource_private_for_agent(knowledge_base, agent.id)
    knowledge_base.updated_at = utc_now()
    session.add(knowledge_base)
    ensure_private_resource_binding(
        session,
        TENANT_ID,
        agent.id,
        "knowledge_base",
        knowledge_base.id,
        "active",
    )
    return knowledge_base


def _staff_markdown(staff_dir: Path) -> list[Path]:
    return sorted(
        path
        for path in staff_dir.rglob("*.md")
        if path.is_file() and not path.name.startswith(".") and not path.name.endswith("_tmp.md")
    )


def _ready_document(
    session: Session, knowledge_base_id: str, version_id: str, filename: str
) -> KnowledgeDocument | None:
    rows = session.exec(
        select(KnowledgeDocument).where(
            KnowledgeDocument.tenant_id == TENANT_ID,
            KnowledgeDocument.knowledge_base_id == knowledge_base_id,
            KnowledgeDocument.knowledge_base_version_id == version_id,
            KnowledgeDocument.filename == filename,
        )
    ).all()
    ready = [row for row in rows if row.status == "ready" and row.chunk_count]
    if ready:
        return ready[0]
    return rows[0] if rows else None


def _replace_document(
    session: Session, document: KnowledgeDocument, text: str, title: str
) -> None:
    from app.knowledge.service import KnowledgeService

    KnowledgeService(session).replace_document_content(document, text, title=title)


def _sync_mcp_tools(session: Session, server: MCPServer, agents: list[AgentProfile]) -> None:
    server = session.get(MCPServer, server.id) or server
    existing = session.exec(select(Tool).where(Tool.mcp_server_id == server.id)).all()
    if not existing:
        discovered = _discover_tools(server)
        if discovered is None:
            return
        for tool in discovered:
            name = str(tool.get("name") or "").strip()
            if not name:
                continue
            session.add(_new_tool(server, tool))
        server.discovered_tools_json = discovered
        server.last_synced_at = utc_now()
        server.updated_at = utc_now()
        session.add(server)
        session.flush()
        existing = session.exec(select(Tool).where(Tool.mcp_server_id == server.id)).all()
    for agent in agents:
        for tool in existing:
            if not tool.enabled:
                continue
            ensure_private_resource_binding(
                session,
                TENANT_ID,
                agent.id,
                "tool",
                tool.id,
                "active",
            )


def _discover_tools(server: MCPServer) -> list[dict[str, Any]] | None:
    from app.tools.mcp_client import MCPClientError, discover_mcp_server

    config = {
        "transport": "stdio",
        "command": server.command,
        "args": list(server.args_json or []),
        "env": dict(server.env_json or {}),
        "cwd": server.cwd,
    }
    try:
        discovery = discover_mcp_server(config, timeout_seconds=MCP_SYNC_TIMEOUT_SECONDS)
    except MCPClientError as exc:
        logger.warning("tao-tie MCP 工具同步失败，启动继续：%s", exc)
        return None
    except Exception as exc:  # noqa: BLE001 - a down tao-tie must not block startup
        logger.warning("tao-tie MCP 工具同步失败，启动继续：%s", exc)
        return None
    tools = discovery.get("tools")
    if not isinstance(tools, list):
        return []
    return [item for item in tools if isinstance(item, dict)]


def _new_tool(server: MCPServer, tool: dict[str, Any]) -> Tool:
    name = str(tool["name"]).strip()
    config: dict[str, Any] = {"tool": name}
    annotations = tool.get("annotations")
    if isinstance(annotations, dict) and annotations:
        config["mcp_annotations"] = annotations
    app = tool.get("app")
    if isinstance(app, dict) and app.get("resource_uri"):
        config["mcp_apps"] = app
    return Tool(
        tenant_id=TENANT_ID,
        name=f"{server.name}.{name}",
        display_name=name,
        description=str(tool.get("description") or ""),
        bucket=server.bucket or "MCP 工具",
        tool_type="mcp",
        method="POST",
        url=f"mcp://{server.name}/{name}",
        config_json=config,
        input_schema=tool.get("input_schema") if isinstance(tool.get("input_schema"), dict) else {},
        output_schema=tool.get("output_schema")
        if isinstance(tool.get("output_schema"), dict)
        else {},
        mcp_server_id=server.id,
        capability_scope=server.capability_scope,
        capability_scope_inherited=True,
        enabled=True,
    )


__all__ = ["seed_ft_loong"]
