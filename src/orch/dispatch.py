from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from . import envfilter, gate, server, state

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


def branch_exists(project: Path, branch: str) -> bool:
    code, _ = _git(project, "show-ref", "--verify", "-q", f"refs/heads/{branch}")
    return code == 0


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


def current_branch(start: Path) -> str | None:
    code, out = _git(start, "symbolic-ref", "--short", "HEAD")
    if code == 0 and out:
        return out
    return None


def worktrees(start: Path) -> list[tuple[Path, str | None]]:
    code, out = _git(start, "worktree", "list", "--porcelain")
    if code != 0:
        return []
    found: list[tuple[Path, str | None]] = []
    path: Path | None = None
    branch: str | None = None
    bare = False
    for line in [*out.splitlines(), ""]:
        if line.startswith("worktree "):
            path, branch, bare = Path(line[len("worktree "):]), None, False
        elif line.startswith("branch "):
            branch = line[len("branch "):]
            if branch.startswith("refs/heads/"):
                branch = branch[len("refs/heads/"):]
        elif line.strip() == "bare":
            bare = True
        elif not line.strip():
            if path is not None and not bare:
                found.append((path, branch))
            path, branch, bare = None, None, False
    return found


def worktree_on_branch(start: Path, branch: str) -> Path | None:
    for path, br in worktrees(start):
        if br == branch and path.is_dir():
            return path
    return None


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
    """Run the committed copy of a lifecycle hook on the base branch — never
    the working-tree copy — so an agent's (or anyone's) uncommitted edit cannot
    change what runs, the same rule as the gate. Fail-open: an absent hook (or
    one only present in the working tree) is a no-op."""
    worktree = Path(worktree)
    base = gate.base_branch(worktree)
    rel = f"{gate.HOOKS_REL}/{name}"
    committed = gate.show(worktree, base, rel)
    if committed is None or not committed.strip():
        note = ""
        if (worktree / rel).exists():
            note = f" (exists in the working tree but not committed on {base}; not run)"
        return True, f"no {name} committed on {base}{note}"

    tmp = Path(tempfile.mkdtemp(prefix="orch-hook-"))
    exe = tmp / name
    try:
        exe.write_text(committed)
        exe.chmod(0o755)

        if envfilter.gate_env_full(worktree):
            base_env = dict(os.environ)
        else:
            base_env = envfilter.filtered_env(
                os.environ, repo_drop=envfilter.repo_drops(worktree))
        hook_env = {**base_env, **env}
        try:
            p = subprocess.run([str(exe)], cwd=str(worktree),
                               env=hook_env,
                               capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as e:
            return False, f"{name} failed to run: {e}"
        out = ((p.stdout or "") + (p.stderr or "")).strip()
        if p.returncode != 0:
            return False, f"{name} exited {p.returncode}: {out[-1500:]}"
        return True, f"{name} ok"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


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
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        d.chmod(0o700)
    except OSError:
        pass
    return d / f"{p.task}.jsonl"


RESOLVE_PROMPT = (
    "A merge into the base branch is in progress and conflicts. Resolve every "
    "conflict marker in this worktree so the tree is coherent, then commit the "
    "resolution with `git add -A && git commit`. Leave the worktree clean. Do "
    "not add unrelated changes."
)


def resolve_command(base_wt: Path, task: str, url: str) -> list[str]:
    return [
        "opencode", "run", "--attach", url, "--dir", str(base_wt),
        "--format", "json", "--title", f"{slug(task)}-merge-conflict",
        RESOLVE_PROMPT,
    ]


def resolve_log_path(task: str) -> Path:
    d = server.state_dir() / "tasks"
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        d.chmod(0o700)
    except OSError:
        pass
    return d / f"{slug(task)}.merge.jsonl"


def _open_private(path: Path):
    """Open a log for the agent's stdout with owner-only permissions."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    return os.fdopen(fd, "wb", buffering=0)


def start_resolve(base_wt: Path, task: str) -> Result:
    """Spawn an agent to resolve <task>'s merge conflict in the base worktree,
    which is mid-merge (MERGE_HEAD set). The agent must resolve and commit."""
    result = Result(ok=True)
    try:
        srv = server.current()
    except (RuntimeError, TimeoutError, OSError) as e:
        return result.fail(f"could not reach the shared server: {e}")
    if srv is None:
        return result.fail("no shared server is running, so nothing to attach to")

    cmd = resolve_command(base_wt, task, srv.url)
    out = resolve_log_path(task)
    handle = _open_private(out)
    try:
        proc = subprocess.Popen(
            cmd, cwd=str(base_wt), stdout=handle,
            stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            start_new_session=True,
            env=envfilter.filtered_env(os.environ,
                                       repo_drop=envfilter.repo_drops(base_wt)),
        )
    except OSError as e:
        handle.close()
        return result.fail(f"could not spawn the resolution agent: {e}")
    finally:
        handle.close()

    result.pid = proc.pid
    result.step(f"resolution agent pid={proc.pid} resolving {slug(task)} in {base_wt}")
    result.step(f"events -> {out}")
    return result


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
) -> Result:
    result = Result(ok=True)

    p, err = plan(start_dir, task, prompt=prompt, branch=branch,
                  worktree=worktree, model=model, agent=agent)
    if err or p is None:
        return result.fail(err or "could not plan the dispatch")

    result.worktree = p.worktree
    result.step(f"task {p.task} -> {p.worktree} (branch {p.branch}, base {p.base})")

    if not state.is_trusted(Path(p.project)):
        result.step("note: this project is not trusted by the daemon, so it "
                    "will not settle, gate or merge this task. "
                    "`orch project trust` fixes that.")

    if branch_exists(Path(p.project), p.branch):
        return result.fail(
            f"{p.task} is already claimed: branch {p.branch} exists in {p.project}. "
            f"A branch is the claim. Use `orch task list` to see it, or `orch "
            f"task reset` after confirming it is dead."
        )

    if dry_run:
        srv = server.current()
        url = srv.url if srv else "<no server running>"
        result.step("dry run, nothing executed")
        result.step("would run: " + " ".join(dispatch_command(p, url)))
        return result

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
        return result.fail(msg)

    if not create_worktree(p, result):
        return result

    ok, msg = run_hook(p.worktree, HOOK_POST, env)
    result.step(f"post-create: {msg}")
    if not ok:
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
        return result.fail(reason + " Pass --allow-ungated to dispatch anyway.")
    if reason:
        result.step(f"WARNING, dispatching ungated on request: {reason}")

    try:
        srv = server.start()
    except (RuntimeError, TimeoutError, OSError) as e:
        return result.fail(f"could not start the shared server: {e}")
    result.step(f"shared server pid={srv.pid} {srv.url}")
    result.url = srv.url

    cmd = dispatch_command(p, srv.url)
    out = log_path(p)
    handle = _open_private(out)
    try:
        proc = subprocess.Popen(
            cmd, cwd=str(p.worktree), stdout=handle,
            stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            start_new_session=True,
            env=envfilter.filtered_env(os.environ,
                                       repo_drop=envfilter.repo_drops(p.dispatcher)),
        )
    except OSError as e:
        handle.close()
        return result.fail(f"could not spawn opencode run: {e}")
    finally:
        handle.close()

    result.pid = proc.pid
    result.step(f"dispatched pid={proc.pid}, events -> {out}")
    result.step(
        "completion is NOT this process exiting: the daemon settles on a clean, "
        "committed, diffing branch. Watch `orch task list`."
    )
    return result
