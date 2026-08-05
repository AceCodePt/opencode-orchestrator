from __future__ import annotations

import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import gate, server
from .store import Store

HOOK_PRE = "pre-task-create"
HOOK_POST = "post-task-create"

SLUG_RE = re.compile(r"[^a-z0-9]+")


def slug(text: str) -> str:
    return SLUG_RE.sub("-", text.strip().lower()).strip("-")


def _git(cwd: Path, *args: str, timeout: int = 60) -> tuple[int, str]:
    try:
        p = subprocess.run(["git", "-C", str(cwd), *args],
                           capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)
    return p.returncode, ((p.stdout or "") + (p.stderr or "")).strip()


@dataclass
class Plan:

    task: str
    branch: str
    worktree: Path
    project: str
    dispatcher: Path
    base: str
    prompt: str
    model: str | None = None
    agent: str | None = None


@dataclass
class Result:
    ok: bool
    steps: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    pid: int | None = None
    url: str | None = None
    worktree: Path | None = None

    def step(self, msg: str) -> None:
        self.steps.append(msg)

    def fail(self, msg: str) -> "Result":
        self.problems.append(msg)
        self.ok = False
        return self


def project_of(start: Path) -> tuple[Path | None, Path | None, str | None]:
    code, top = _git(start, "rev-parse", "--show-toplevel")
    if code != 0 or not top:
        return None, None, (
            "not inside a git worktree. Dispatch has to run from a worktree: the "
            "pre-create hook lives in one, and a bare repo has no working tree for "
            "it to live in."
        )
    code, common = _git(start, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if code != 0 or not common:
        return Path(top), Path(top), None
    return Path(top), Path(common), None


def plan(
    start: Path,
    task: str,
    prompt: str | None = None,
    branch: str | None = None,
    worktree: Path | None = None,
    model: str | None = None,
    agent: str | None = None,
) -> tuple[Plan | None, str | None]:
    dispatcher, project, err = project_of(start)
    if err or dispatcher is None or project is None:
        return None, err

    name = slug(task)
    if not name:
        return None, f"{task!r} does not slugify to anything usable"

    br = branch or name
    wt = worktree or (dispatcher.parent / name)
    base = gate.base_branch(dispatcher)

    if prompt:
        text = prompt
    else:
        text, err = _prompt_from_task_file(dispatcher, name, base)
        if err:
            return None, err
    return Plan(task=name, branch=br, worktree=wt, project=str(project),
                dispatcher=dispatcher, base=base, prompt=text or "",
                model=model, agent=agent), None


def task_names(dispatcher: Path) -> list[str]:
    root = dispatcher / "tasks"
    if not root.is_dir():
        return []
    return sorted(
        d.name for d in root.iterdir() if d.is_dir() and (d / "task.md").exists()
    )


def _prompt_from_task_file(
    dispatcher: Path, name: str, base: str
) -> tuple[str | None, str | None]:
    rel = f"tasks/{name}/task.md"
    committed = gate.show(dispatcher, base, rel)
    if committed is not None:
        return (
            f"Read {rel} and execute it. Follow its Verification section; a task is "
            f"not done because code exists."
        ), None

    if (dispatcher / rel).exists():
        return None, (
            f"{rel} exists but is not committed on {base}. The worktree is created "
            f"from {base}, so the agent would start with no instructions at all and "
            f"fail silently. Commit it, or pass --prompt to dispatch ad-hoc work."
        )
    return None, (
        f"no {rel} on {base} and no --prompt given, so there is nothing to tell the "
        f"agent to do."
    )


def run_hook(worktree: Path, name: str, env: dict[str, str],
             timeout: int = 900) -> tuple[bool, str]:
    hook = worktree / gate.HOOKS_REL / name
    if not hook.exists():
        return True, f"no {name} hook"
    if not os.access(hook, os.X_OK):
        return False, f"{name} exists but is not executable"
    try:
        p = subprocess.run([str(hook)], cwd=str(worktree),
                           env={**os.environ, **env},
                           capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, f"{name} failed to run: {e}"
    out = ((p.stdout or "") + (p.stderr or "")).strip()
    if p.returncode != 0:
        return False, f"{name} exited {p.returncode}: {out[-1500:]}"
    return True, f"{name} ok"


def create_worktree(p: Plan, result: Result) -> bool:
    if p.worktree.exists():
        code, _ = _git(p.worktree, "rev-parse", "--show-toplevel")
        if code == 0:
            result.step(f"worktree already exists at {p.worktree}")
            return True
        result.fail(
            f"{p.worktree} exists but is not a git worktree. Refusing to touch it: "
            f"a directory left behind by a previous run may hold work nobody has "
            f"looked at."
        )
        return False

    _git(p.dispatcher, "worktree", "prune")

    code, out = _git(p.dispatcher, "worktree", "add", "-b", p.branch,
                     str(p.worktree), p.base)
    if code != 0:
        code2, out2 = _git(p.dispatcher, "worktree", "add", str(p.worktree), p.branch)
        if code2 != 0:
            result.fail(f"git worktree add failed: {out}\nand as existing branch: {out2}")
            return False
        result.step(f"worktree {p.worktree.name} on existing branch {p.branch}")
        return True
    result.step(f"worktree {p.worktree.name} on new branch {p.branch} from {p.base}")
    return True


def dispatch_command(p: Plan, url: str, json_stream: bool = True) -> list[str]:
    cmd = ["opencode", "run", "--attach", url, "--dir", str(p.worktree)]
    if json_stream:
        cmd += ["--format", "json"]
    if p.model:
        cmd += ["-m", p.model]
    if p.agent:
        cmd += ["--agent", p.agent]
    cmd += ["--title", p.task, p.prompt]
    return cmd


def log_path(p: Plan) -> Path:
    d = server.state_dir() / "tasks"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{p.task}.jsonl"


def start(
    start_dir: Path,
    task: str,
    prompt: str | None = None,
    model: str | None = None,
    agent: str | None = None,
    branch: str | None = None,
    worktree: Path | None = None,
    dry_run: bool = False,
    allow_ungated: bool = False,
    store: Store | None = None,
) -> Result:
    result = Result(ok=True)

    p, err = plan(start_dir, task, prompt=prompt, branch=branch,
                  worktree=worktree, model=model, agent=agent)
    if err or p is None:
        return result.fail(err or "could not plan the dispatch")

    result.worktree = p.worktree
    result.step(f"task {p.task} -> {p.worktree} (branch {p.branch}, base {p.base})")

    if dry_run:
        srv = server.current()
        url = srv.url if srv else "<no server running>"
        result.step("dry run, nothing executed")
        result.step("would run: " + " ".join(dispatch_command(p, url)))
        return result

    own_store = store is None
    st = store or Store()
    try:
        declared = task_names(p.dispatcher)
        if p.task not in declared:
            declared.append(p.task)
        st.sync_intent(p.project, declared)

        claimed = st.claim(p.project, p.task)
        if claimed is None:
            existing = st.get(p.project, p.task)
            if existing is None:
                return result.fail(
                    f"{p.task} is not registered and could not be created. That "
                    f"should not happen - the store is probably not writable."
                )
            if existing.status == "running":
                held = (
                    f", lease expires in {existing.lease_until - int(time.time())}s"
                    if existing.lease_until else ""
                )
                return result.fail(
                    f"{p.task} is already running (pid={existing.pid or '?'}{held}). "
                    f"Use `orch observe` to see it; reclaiming needs proof it is "
                    f"dead, not just an expired lease."
                )
            return result.fail(
                f"{p.task} is {existing.status}, so there is nothing to claim. "
                f"Last outcome: {existing.status}."
            )
        result.step(f"claimed {p.task}")

        env = {
            "TASK_NAME": p.task,
            "BRANCH_NAME": p.branch,
            "WORKTREE_PATH": str(p.worktree),
            "WORKTREE_NAME": p.worktree.name,
            "BASE_BRANCH": p.base,
        }

        ok, msg = run_hook(p.dispatcher, HOOK_PRE, env)
        result.step(f"pre-create: {msg}")
        if not ok:
            st.reclaim(p.project, p.task, f"pre-create hook: {msg}")
            return result.fail(msg)

        if not create_worktree(p, result):
            st.reclaim(p.project, p.task, "worktree creation failed")
            return result

        ok, msg = run_hook(p.worktree, HOOK_POST, env)
        result.step(f"post-create: {msg}")
        if not ok:
            st.finish(p.project, p.task, "failed", f"post-create hook: {msg}")
            return result.fail(msg)

        findings = gate.inspect(p.worktree, p.base)
        for f in findings:
            result.step(f"gate {f.name}: {f.state} - {f.detail}")

        reason = None
        if not findings:
            reason = (
                f"no gate is committed on {p.base}, so nothing would verify this "
                f"task. Commit {gate.HOOKS_REL}/session-idle first."
            )
        elif gate.blocking(findings):
            reason = "this task would run ungated:\n" + "\n".join(
                f"      - {f.name} ({f.state}): {f.detail}"
                for f in gate.blocking(findings)
            )
        if reason and not allow_ungated:
            st.reclaim(p.project, p.task, "refused: ungated")
            return result.fail(reason + " Pass --allow-ungated to dispatch anyway.")
        if reason:
            result.step(f"WARNING, dispatching ungated on request: {reason}")

        try:
            srv = server.start()
        except (RuntimeError, TimeoutError, OSError) as e:
            st.reclaim(p.project, p.task, "no shared server")
            return result.fail(f"could not start the shared server: {e}")
        result.step(f"shared server pid={srv.pid} {srv.url}")
        result.url = srv.url

        cmd = dispatch_command(p, srv.url)
        out = log_path(p)
        handle = open(out, "wb", buffering=0)
        try:
            proc = subprocess.Popen(
                cmd, cwd=str(p.worktree), stdout=handle,
                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as e:
            handle.close()
            st.reclaim(p.project, p.task, "dispatch failed to spawn")
            return result.fail(f"could not spawn opencode run: {e}")
        finally:
            handle.close()

        result.pid = proc.pid
        st.record_launch(p.project, p.task, worktree=str(p.worktree),
                         branch=p.branch, port=srv.port, pid=proc.pid)
        result.step(f"dispatched pid={proc.pid}, events -> {out}")
        result.step(
            "completion is NOT this process exiting: the session can keep working "
            "after it returns. Watch `orch observe`."
        )
        return result
    finally:
        if own_store:
            st.close()


def poll_once(seconds: float = 0.0) -> None:
    if seconds > 0:
        time.sleep(seconds)
