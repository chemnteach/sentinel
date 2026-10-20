from __future__ import annotations

from dataclasses import dataclass

from sentinel.chat.session import ChatSession
from sentinel.orchestrator.main import HumanGateRequired


@dataclass
class FakeConfig:
    self_approve_mode: bool = False


class _OrchestratorGate:
    def __init__(self, cfg: FakeConfig) -> None:
        self.cfg = cfg

    def __enter__(self) -> "_OrchestratorGate":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def run_phase2(self, task_id: str | None = None, limit: int | None = None) -> None:
        raise HumanGateRequired("local_review", {"task_id": task_id, "limit": limit})


class _OrchestratorOK:
    def __init__(self, cfg: FakeConfig) -> None:
        self.cfg = cfg
        self.diff_calls: list[str] = []
        self.change_calls: list[tuple[str, str]] = []
        self.approve_calls: list[tuple[str, str]] = []

    def __enter__(self) -> "_OrchestratorOK":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def run_phase2(self, task_id: str | None = None, limit: int | None = None) -> None:
        return None

    def get_task_diff(self, task_id: str) -> str:
        self.diff_calls.append(task_id)
        return f"diff for {task_id}"

    def request_local_changes(self, task_id: str, reason: str) -> None:
        self.change_calls.append((task_id, reason))

    def approve_local_and_push(self, task_id: str, comment: str = "") -> None:
        self.approve_calls.append((task_id, comment))


class _OrchestratorNeedsSelfApprove:
    """Simulate a flow that only completes when self-approval mode is enabled."""

    def __init__(self, cfg: FakeConfig) -> None:
        self.cfg = cfg

    def __enter__(self) -> "_OrchestratorNeedsSelfApprove":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def run_phase2(self, task_id: str | None = None, limit: int | None = None) -> None:
        if self.cfg.self_approve_mode:
            return None
        raise HumanGateRequired("local_review", {"task_id": task_id, "limit": limit})


def test_run_single_task_returns_human_gate(monkeypatch) -> None:
    cfg = FakeConfig()
    monkeypatch.setattr("sentinel.chat.session.Orchestrator", _OrchestratorGate)
    session = ChatSession(config=cfg)

    result = session.run_single_task("task-1", self_approve=True)

    assert cfg.self_approve_mode is False
    assert result["status"] == "human_gate"
    assert result["gate_type"] == "local_review"
    assert result["payload"]["task_id"] == "task-1"


def test_run_single_task_default_does_not_enable_self_approval(monkeypatch) -> None:
    cfg = FakeConfig()
    monkeypatch.setattr("sentinel.chat.session.Orchestrator", _OrchestratorNeedsSelfApprove)
    session = ChatSession(config=cfg)

    result = session.run_single_task("task-1")

    assert cfg.self_approve_mode is False
    assert result["status"] == "human_gate"


def test_run_single_task_self_approve_requires_policy_authorization(monkeypatch) -> None:
    cfg = FakeConfig(self_approve_mode=False)
    monkeypatch.setattr("sentinel.chat.session.Orchestrator", _OrchestratorNeedsSelfApprove)
    session = ChatSession(config=cfg)

    result = session.run_single_task("task-1", self_approve=True)

    assert cfg.self_approve_mode is False
    assert result["status"] == "human_gate"


def test_run_single_task_self_approve_works_when_policy_authorized(monkeypatch) -> None:
    cfg = FakeConfig(self_approve_mode=True)
    monkeypatch.setattr("sentinel.chat.session.Orchestrator", _OrchestratorNeedsSelfApprove)
    session = ChatSession(config=cfg)

    result = session.run_single_task("task-1", self_approve=True)

    assert cfg.self_approve_mode is True
    assert result["status"] == "completed"


def test_chat_helpers_delegate_to_orchestrator(monkeypatch) -> None:
    cfg = FakeConfig()
    calls = {
        "diff": [],
        "changes": [],
        "approve": [],
    }

    def factory(config: FakeConfig) -> _OrchestratorOK:
        inst = _OrchestratorOK(config)
        original_get = inst.get_task_diff
        original_request = inst.request_local_changes
        original_approve = inst.approve_local_and_push

        def wrapped_get(task_id: str) -> str:
            result = original_get(task_id)
            calls["diff"].extend(inst.diff_calls)
            return result

        def wrapped_request(task_id: str, reason: str) -> None:
            original_request(task_id, reason)
            calls["changes"].extend(inst.change_calls)

        def wrapped_approve(task_id: str, comment: str = "") -> None:
            original_approve(task_id, comment)
            calls["approve"].extend(inst.approve_calls)

        inst.get_task_diff = wrapped_get  # type: ignore[method-assign]
        inst.request_local_changes = wrapped_request  # type: ignore[method-assign]
        inst.approve_local_and_push = wrapped_approve  # type: ignore[method-assign]
        return inst

    monkeypatch.setattr("sentinel.chat.session.Orchestrator", factory)
    session = ChatSession(config=cfg)

    assert session.show_diff("task-2") == "diff for task-2"
    session.request_changes("task-2", "please revise")
    session.approve_local("task-2", "looks good")

    assert calls["diff"] == ["task-2"]
    assert calls["changes"] == [("task-2", "please revise")]
    assert calls["approve"] == [("task-2", "looks good")]
