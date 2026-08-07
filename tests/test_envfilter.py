from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from orch import envfilter  # noqa: E402


def _machine(tmp: tempfile.TemporaryDirectory, cfg: dict) -> None:
    p = Path(tmp.name) / "orch" / "env.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cfg))


def _env(**kw) -> dict:
    base = {"PATH": "/usr/bin", "HOME": "/home/u", "USER": "u",
            "ANTHROPIC_API_KEY": "sk-ant-1", "OPENAI_API_KEY": "sk-2",
            "LANG": "C"}
    base.update(kw)
    return base


def test_default_drops_and_keeps() -> None:
    env = envfilter.filtered_env(_env(
        SSH_AUTH_SOCK="/tmp/ssh-agent.sock",
        GH_TOKEN="ghp_x",
        AWS_ACCESS_KEY_ID="AKIA",
        AWS_SECRET_ACCESS_KEY="x",
        MY_APP_PASSWORD="pw",
    ))
    for dropped in ("SSH_AUTH_SOCK", "GH_TOKEN", "AWS_ACCESS_KEY_ID",
                    "AWS_SECRET_ACCESS_KEY", "MY_APP_PASSWORD"):
        assert dropped not in env, f"{dropped} must be dropped"
    for kept in ("PATH", "HOME", "USER", "ANTHROPIC_API_KEY",
                 "OPENAI_API_KEY", "LANG"):
        assert kept in env, f"{kept} must survive"
    print("ok: the default filter drops secrets and keeps provider keys")


def test_provider_auth_tokens_survive() -> None:
    env = envfilter.filtered_env(_env(
        ANTHROPIC_AUTH_TOKEN="tok", OPENAI_AUTH_TOKEN="tok2"))
    assert env.get("ANTHROPIC_AUTH_TOKEN") == "tok"
    assert env.get("OPENAI_AUTH_TOKEN") == "tok2"
    print("ok: provider auth tokens (used instead of api keys) are not dropped")


def test_machine_keep_overrides_a_drop() -> None:
    tmp = tempfile.TemporaryDirectory()
    try:
        _machine(tmp, {"drop": ["SSH_AUTH_SOCK"], "keep": ["SSH_AUTH_SOCK"]})
        os.environ["XDG_CONFIG_HOME"] = tmp.name
        env = envfilter.filtered_env(_env(SSH_AUTH_SOCK="/tmp/sock"))
        assert env.get("SSH_AUTH_SOCK") == "/tmp/sock"
    finally:
        tmp.cleanup()
    print("ok: the machine owner can keep a variable the default drops")


def test_machine_drop_is_additive() -> None:
    tmp = tempfile.TemporaryDirectory()
    try:
        _machine(tmp, {"drop": ["MY_CUSTOM_SECRET"]})
        os.environ["XDG_CONFIG_HOME"] = tmp.name
        env = envfilter.filtered_env(_env(MY_CUSTOM_SECRET="v"))
        assert "MY_CUSTOM_SECRET" not in env
    finally:
        tmp.cleanup()
    print("ok: the machine owner can drop more variables")


def test_repo_drops_and_gate_env_full() -> None:
    tmp = tempfile.TemporaryDirectory()
    try:
        root = Path(tmp.name) / "wt"
        (root / ".orchestration").mkdir(parents=True)
        (root / ".orchestration" / "config.json").write_text(json.dumps({
            "agentEnv": {"drop": ["REPO_SECRET"]},
            "gate": {"env": "full"},
        }))
        assert envfilter.repo_drops(root) == {"REPO_SECRET"}
        assert envfilter.gate_env_full(root) is True
        env = envfilter.filtered_env(_env(REPO_SECRET="v"),
                                     repo_drop=envfilter.repo_drops(root))
        assert "REPO_SECRET" not in env
    finally:
        tmp.cleanup()
    print("ok: a repo can only restrict further, and a gate can opt into full env")


def test_agent_git_identity_is_injected() -> None:
    env = envfilter.filtered_env(_env())
    assert env.get("GIT_AUTHOR_NAME") == "orch"
    assert env.get("GIT_COMMITTER_EMAIL") == "orch@localhost"
    env = envfilter.filtered_env(_env(), git=False)
    assert "GIT_AUTHOR_NAME" not in env
    print("ok: a scoped git identity is injected unless disabled")


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"\n{len(tests)} envfilter tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
