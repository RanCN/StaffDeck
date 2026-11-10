from pathlib import Path
from types import SimpleNamespace

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.db import ft_loong_seed
from app.db.models import (
    AgentKnowledgeBranch,
    AgentProfile,
    AgentResourceBinding,
    KnowledgeDocument,
    MCPServer,
    Tool,
)


def _session() -> Session:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def _settings(root: str, base_url: str = "http://127.0.0.1:8000") -> SimpleNamespace:
    return SimpleNamespace(ft_loong_root=root, taotie_base_url=base_url)


def _checkout(root: Path) -> None:
    for post in ("交易员", "产品"):
        staff = root / ".agents/skills/digital-employee/staff" / post
        staff.mkdir(parents=True)
        (staff / "岗位.md").write_text(f"# {post}\n\n职责说明。\n", encoding="utf-8")
        (staff / "草稿_tmp.md").write_text("临时文件\n", encoding="utf-8")
    backend = root / "services/tao-tie/backend"
    backend.mkdir(parents=True)


def test_skips_when_root_is_empty(monkeypatch) -> None:
    monkeypatch.setattr(ft_loong_seed, "get_settings", lambda: _settings(""))
    with _session() as db:
        ft_loong_seed.seed_ft_loong(db)
        assert db.exec(select(MCPServer)).first() is None
        assert db.exec(select(AgentProfile)).first() is None


def test_skips_when_checkout_is_incomplete(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(ft_loong_seed, "get_settings", lambda: _settings(str(tmp_path)))
    with _session() as db:
        ft_loong_seed.seed_ft_loong(db)
        assert db.exec(select(MCPServer)).first() is None


def test_seeds_posts_from_the_configured_checkout(monkeypatch, tmp_path: Path) -> None:
    first = tmp_path / "machine-a"
    _checkout(first)
    monkeypatch.setattr(ft_loong_seed, "get_settings", lambda: _settings(str(first), "http://10.0.0.8:8000/"))
    with _session() as db:
        ft_loong_seed.seed_ft_loong(db, sync_tools=False)
        server = db.exec(select(MCPServer).where(MCPServer.name == "tao-tie-review")).one()
        assert server.cwd == str(first.resolve())
        assert server.command == "uv"
        assert server.args_json == [
            "run",
            "--directory",
            "services/tao-tie/backend",
            "python",
            "-m",
            "mcp",
        ]
        assert server.env_json["TAOTIE_BASE_URL"] == "http://10.0.0.8:8000"
        assert server.env_json["PYTHONPATH"] == ".."
        trader = db.exec(select(AgentProfile).where(AgentProfile.name == "交易员")).one()
        product = db.exec(select(AgentProfile).where(AgentProfile.name == "产品")).one()
        assert trader.metadata_json["ft_loong_post"] == "trader"
        assert "交易员-staffdeck" in (trader.persona_prompt or "")
        assert "产品-staffdeck" in (product.persona_prompt or "")
        documents = db.exec(select(KnowledgeDocument)).all()
        assert {row.filename for row in documents} == {"岗位.md"}
        assert all(row.status == "ready" and row.chunk_count for row in documents)
        assert all("草稿_tmp.md" not in row.filename for row in documents)

        moved = tmp_path / "machine-b"
        first.rename(moved)
        monkeypatch.setattr(ft_loong_seed, "get_settings", lambda: _settings(str(moved)))
        ft_loong_seed.seed_ft_loong(db, sync_tools=False)
        db.refresh(server)
        assert server.cwd == str(moved.resolve())
        assert len(db.exec(select(AgentProfile)).all()) == 2
        ready = db.exec(
            select(KnowledgeDocument).where(KnowledgeDocument.status == "ready")
        ).all()
        assert len(ready) == 2
        branch = db.exec(select(AgentKnowledgeBranch)).first()
        assert branch is not None
        assert branch.head_version == "1.0.0"


def test_preserves_an_existing_knowledge_head(monkeypatch, tmp_path: Path) -> None:
    checkout = tmp_path / "ft-loong"
    _checkout(checkout)
    monkeypatch.setattr(ft_loong_seed, "get_settings", lambda: _settings(str(checkout)))
    with _session() as db:
        ft_loong_seed.seed_ft_loong(db, sync_tools=False)
        branch = db.exec(
            select(AgentKnowledgeBranch).where(AgentKnowledgeBranch.agent_id != "")
        ).first()
        assert branch is not None
        branch.head_version = "1.0.0-branch.kept"
        db.add(branch)
        db.commit()
        ft_loong_seed.seed_ft_loong(db, sync_tools=False)
        db.refresh(branch)
        assert branch.head_version == "1.0.0-branch.kept"


def test_sync_binds_discovered_tools_without_failing_startup(monkeypatch, tmp_path: Path) -> None:
    checkout = tmp_path / "ft-loong"
    _checkout(checkout)
    monkeypatch.setattr(ft_loong_seed, "get_settings", lambda: _settings(str(checkout)))
    calls = {"count": 0}

    def discover(server):
        calls["count"] += 1
        return [{"name": "staff_list", "description": "列出工位", "input_schema": {"type": "object"}}]

    monkeypatch.setattr(ft_loong_seed, "_discover_tools", discover)
    with _session() as db:
        ft_loong_seed.seed_ft_loong(db)
        tool = db.exec(select(Tool).where(Tool.name == "tao-tie-review.staff_list")).one()
        bindings = db.exec(
            select(AgentResourceBinding).where(
                AgentResourceBinding.resource_type == "tool",
                AgentResourceBinding.resource_id == tool.id,
            )
        ).all()
        assert len(bindings) == 2
        assert calls["count"] == 1
        ft_loong_seed.seed_ft_loong(db)
        assert calls["count"] == 1
        assert len(db.exec(select(Tool)).all()) == 1


def test_tool_sync_failure_keeps_the_server(monkeypatch, tmp_path: Path) -> None:
    checkout = tmp_path / "ft-loong"
    _checkout(checkout)
    monkeypatch.setattr(ft_loong_seed, "get_settings", lambda: _settings(str(checkout)))
    monkeypatch.setattr(ft_loong_seed, "_discover_tools", lambda _server: None)
    with _session() as db:
        ft_loong_seed.seed_ft_loong(db)
        server = db.exec(select(MCPServer)).one()
        assert server.cwd == str(checkout.resolve())
        assert db.exec(select(Tool)).first() is None
