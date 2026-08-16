from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import harness, server, state, verify

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
    session_id: str | None = None
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
    base = verify.base_branch(dispatcher)

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
    committed = verify.show(dispatcher, base, rel)
    if committed is not None:
        return (
            f"Read {rel} and execute it. Follow its Verification section; a task is "
            f"not done because code exists. When you believe the Verification is "
            f"satisfied, declare the task done by committing your work to this branch "
            f"(`git add -A && git commit`). The committed branch is the done signal "
            f"the daemon reads; it then verifies and merges."
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
    change what runs, the same rule as the verification hook. Fail-open: an absent hook (or
    one only present in the working tree) is a no-op."""
    worktree = Path(worktree)
    base = verify.base_branch(worktree)
    rel = f"{verify.HOOKS_REL}/{name}"
    r = verify.run_committed_hook(worktree, name, env=env, timeout=timeout,
                                  base=base)
    if not r.present:
        note = ""
        if (worktree / rel).exists():
            note = f" (exists in the working tree but not committed on {base}; not run)"
        return True, f"no {name} committed on {base}{note}"
    if r.error:
        return False, f"{name} failed to run: {r.error}"
    if not r.ok:
        return False, f"{name} exited {r.exit_code}: {r.output[-1500:]}"
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


AGENT_IDENTITY_NAME = "orch"
AGENT_IDENTITY_EMAIL = "orch@localhost"


def configure_identity(worktree: Path) -> str | None:
    """Give the task worktree its own git identity so the agent can commit its
    work. Per-worktree (`--worktree`) config, never the shared repo config, so
    the human's base-worktree commits keep their own author. Best-effort: a
    machine that already has a global identity does not need this, so a
    failure is a note, not a dispatch error."""
    code, out = _git(worktree, "config", "extensions.worktreeConfig", "true")
    if code != 0:
        return f"could not enable per-worktree git config: {out[:200]}"
    for key, value in (("user.name", AGENT_IDENTITY_NAME),
                       ("user.email", AGENT_IDENTITY_EMAIL)):
        code, out = _git(worktree, "config", "--worktree", key, value)
        if code != 0:
            return f"could not set worktree git {key}: {out[:200]}"
    return None


RESOLVE_PROMPT = (
    "A merge into the base branch is in progress and conflicts. Resolve every "
    "conflict marker in this worktree so the tree is coherent, then commit the "
    "resolution with `git add -A && git commit`. Leave the worktree clean. Do "
    "not add unrelated changes."
)


def start_resolve(base_wt: Path, task: str) -> Result:
    """Start an agent to resolve <task>'s merge conflict in the base worktree,
    which is mid-merge (MERGE_HEAD set). The agent must resolve and commit."""
    result = Result(ok=True)
    try:
        srv = server.current()
    except (RuntimeError, TimeoutError, OSError) as e:
        return result.fail(f"could not reach the shared server: {e}")
    if srv is None:
        return result.fail("no shared server is running, so nothing to attach to")

    try:
        sess = harness.create_session(srv.port, str(base_wt),
                                      title=f"{slug(task)}-merge-conflict")
    except harness.HarnessError as e:
        return result.fail(f"could not create the resolution session: {e}")
    sid = (sess or {}).get("id")
    if not sid:
        return result.fail("the server did not return a session id")
    try:
        harness.prompt_async(srv.port, sid, RESOLVE_PROMPT)
    except harness.HarnessError as e:
        return result.fail(f"could not prompt the resolution session: {e}")
    result.session_id = sid
    result.step(f"resolution session {sid} resolving {slug(task)} in {base_wt}")
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
) -> Result:
    result = Result(ok=True)

    p, err = plan(start_dir, task, prompt=prompt, branch=branch,
                  worktree=worktree, model=model, agent=agent)
    if err or p is None:
        return result.fail(err or "could not plan the dispatch")

    result.worktree = p.worktree
    result.step(f"task {p.task} -> {p.worktree} (branch {p.branch}, base {p.base})")

    findings = verify.inspect(p.dispatcher, p.base)
    for f in findings:
        result.step(f"verify {f.name}: {f.state} - {f.detail}")
    if not findings:
        result.step("no verification hook committed - this task's work will be "
                    "accepted without verification")

    if not state.is_trusted(Path(p.project)):
        result.step("note: this project is not trusted by the daemon, so it "
                    "will not settle, verify or merge this task. "
                    "`orch project trust` fixes that.")

    if branch_exists(Path(p.project), p.branch):
        return result.fail(
            f"{p.task} is already claimed: branch {p.branch} exists in {p.project}. "
            f"A branch is the claim. Use `orch task list` to see it, or `orch "
            f"task reset` after confirming it is dead."
        )

    if dry_run:
        result.step("dry run, nothing executed")
        result.step(f"would run a task session for {p.task} on the shared "
                    f"server, bound to {p.worktree}")
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

    ident_note = configure_identity(p.worktree)
    if ident_note:
        result.step(f"identity: {ident_note}")
    else:
        result.step("worktree git identity set to "
                    f"{AGENT_IDENTITY_NAME} <{AGENT_IDENTITY_EMAIL}> so the agent "
                    "can commit its work")

    ok, msg = run_hook(p.worktree, HOOK_POST, env)
    result.step(f"post-create: {msg}")
    if not ok:
        return result.fail(msg)

    try:
        srv = server.start()
    except (RuntimeError, TimeoutError, OSError) as e:
        return result.fail(f"could not start the shared server: {e}")
    result.step(f"shared server pid={srv.pid} {srv.url}")
    result.url = srv.url

    try:
        sess = harness.create_session(srv.port, str(p.worktree), title=p.task)
    except harness.HarnessError as e:
        return result.fail(f"could not create the task session: {e}")
    sid = (sess or {}).get("id")
    if not sid:
        return result.fail("the server did not return a session id")
    try:
        harness.prompt_async(srv.port, sid, p.prompt,
                             agent=p.agent, model=p.model)
    except harness.HarnessError as e:
        return result.fail(f"could not prompt the task session: {e}")
    result.session_id = sid
    result.step(f"task session {sid} for {p.task} in {p.worktree}")
    result.step(
        "completion is NOT a process exiting: the agent declares done by committing "
        "its work, and the daemon settles on that committed, diffing branch. "
        "Watch `orch task list`."
    )
    return result
