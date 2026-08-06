from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from orch import cli, observe, state  # noqa: E402


class _FakeProject:
    def __init__(self, tasks: list[state.TaskState]):
        self._tasks = tasks

    @classmethod
    def from_dir(cls, _start: Path) -> "_FakeProject | None":
        return cls._instance

    _instance: "_FakeProject | None" = None

    def scan(self, port: int | None = None) -> list[state.TaskState]:
        return self._tasks


def _blocked(name: str = "task-a", kind: str = "bash") -> state.TaskState:
    b = observe.Blocked(kind=kind, request_id="per_1", session_id="ses_1",
                        detail="/etc/*")
    return state.TaskState(project="/tmp/lab/lab.git", name=name, base="main",
                           blocked=[b])


def _args(**kw) -> object:
    class A:
        pass
    a = A()
    for k, v in kw.items():
        setattr(a, k, v)
    a.directory = None
    return a


def test_answer_allow_replies_to_a_permission() -> None:
    replies: list[tuple] = []
    t = _blocked()
    _FakeProject._instance = _FakeProject([t])
    orig_status = cli.server_mod.status
    orig_reply = cli.harness.reply_permission
    orig_from = cli.state.Project.from_dir
    cli.state.Project.from_dir = _FakeProject.from_dir
    cli.server_mod.status = lambda: {"running": True, "unresponsive": False,
                                     "port": 4096}
    cli.harness.reply_permission = lambda *a, **k: replies.append((a, k))
    try:
        rc = cli.cmd_task_answer(_args(name="task-a", reply="allow",
                                       request=None, remember=False))
        assert rc == 0, rc
        (port, sid, pid, reply), kw = replies[0]
        assert (port, sid, pid, reply) == (4096, "ses_1", "per_1", "allow")
        assert kw == {"remember": False}
    finally:
        cli.state.Project.from_dir = orig_from
        cli.server_mod.status = orig_status
        cli.harness.reply_permission = orig_reply
    print("ok: `task answer allow` replies to the permission request")


def test_answer_rejects_non_allow_deny_for_a_permission() -> None:
    t = _blocked()
    _FakeProject._instance = _FakeProject([t])
    orig_status = cli.server_mod.status
    orig_from = cli.state.Project.from_dir
    cli.state.Project.from_dir = _FakeProject.from_dir
    cli.server_mod.status = lambda: {"running": True, "unresponsive": False,
                                     "port": 4096}
    try:
        rc = cli.cmd_task_answer(_args(name="task-a", reply="maybe",
                                       request=None, remember=False))
        assert rc == 2, rc
    finally:
        cli.state.Project.from_dir = orig_from
        cli.server_mod.status = orig_status
    print("ok: a permission only accepts allow or deny")


def test_answer_question_routes_to_reply_question() -> None:
    t = _blocked(kind="question")
    _FakeProject._instance = _FakeProject([t])
    sent: list[tuple] = []
    orig_status = cli.server_mod.status
    orig_from = cli.state.Project.from_dir
    orig_q = cli.harness.reply_question
    cli.state.Project.from_dir = _FakeProject.from_dir
    cli.server_mod.status = lambda: {"running": True, "unresponsive": False,
                                     "port": 4096}
    cli.harness.reply_question = lambda *a, **k: sent.append((a, k))
    try:
        rc = cli.cmd_task_answer(_args(name="task-a", reply="go ahead",
                                       request=None, remember=False))
        assert rc == 0, rc
        (port, rid, answers), kw = sent[0]
        assert port == 4096 and rid == "per_1"
        assert answers == [{"value": "go ahead"}]
    finally:
        cli.state.Project.from_dir = orig_from
        cli.server_mod.status = orig_status
        cli.harness.reply_question = orig_q
    print("ok: a question routes to the question reply endpoint")


def test_answer_unblocked_task_is_refused() -> None:
    _FakeProject._instance = _FakeProject(
        [state.TaskState(project="/tmp/lab/lab.git", name="task-a", base="main")])
    orig_status = cli.server_mod.status
    orig_from = cli.state.Project.from_dir
    cli.state.Project.from_dir = _FakeProject.from_dir
    cli.server_mod.status = lambda: {"running": True, "unresponsive": False,
                                     "port": 4096}
    try:
        rc = cli.cmd_task_answer(_args(name="task-a", reply="allow",
                                       request=None, remember=False))
        assert rc == 1, rc
    finally:
        cli.state.Project.from_dir = orig_from
        cli.server_mod.status = orig_status
    print("ok: answering a task that is not blocked is refused")


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"\n{len(tests)} cli tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
