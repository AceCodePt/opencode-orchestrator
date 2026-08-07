from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path

# Secrets a dispatched agent or a project gate never needs. Provider keys
# (`*_API_KEY`, `*_AUTH_TOKEN`) are deliberately NOT here - opencode needs them
# to talk to the model. The machine owner can drop more, or keep something back,
# in ~/.config/orch/env.json.
DEFAULT_DROP = {
    "SSH_AUTH_SOCK", "SSH_AGENT_PID",
    "GH_TOKEN", "GITHUB_TOKEN", "GITLAB_TOKEN", "BITBUCKET_TOKEN",
    "DATABASE_URL", "REDIS_URL", "MEMCACHED_URL", "AMQP_URL", "MONGO_URL",
    "KUBECONFIG", "GPG_AGENT_INFO", "KRB5CCNAME", "GIT_ASKPASS",
}

DEFAULT_DROP_PREFIXES = (
    "AWS_", "GCP_", "AZURE_", "GOOGLE_", "GCLOUD_", "CLOUDSDK_",
    "ALIBABA_", "ORACLE_", "DO_", "DIGITALOCEAN_", "TWILIO_", "STRIPE_",
    "SENDGRID_", "MAILGUN_", "HCP_", "VAULT_", "TF_VAR_",
)

DEFAULT_DROP_SUFFIXES = (
    "_PASSWORD", "_PASSPHRASE", "_SECRET", "_CREDENTIAL", "_CREDENTIALS",
    "_PRIVATE_KEY", "_ACCESS_KEY", "_CLIENT_SECRET", "_SIGNING_KEY",
    "_DATABASE_URL", "_MASTER_KEY",
)

AGENT_GIT_ENV = {
    "GIT_AUTHOR_NAME": "orch",
    "GIT_AUTHOR_EMAIL": "orch@localhost",
    "GIT_COMMITTER_NAME": "orch",
    "GIT_COMMITTER_EMAIL": "orch@localhost",
}


def machine_config() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "orch" / "env.json"


def _read_json(path: Path) -> dict:
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def machine_policy() -> tuple[set[str], set[str]]:
    """(drop, keep) from the machine owner's ~/.config/orch/env.json. Only the
    owner can relax the filter; a project can only ever restrict it further."""
    cfg = _read_json(machine_config())
    return set(cfg.get("drop") or []), set(cfg.get("keep") or [])


def repo_drops(worktree: Path) -> set[str]:
    """Extra drops a project declares in .orchestration/config.json
    (\"agentEnv\": {\"drop\": [...]}). Restriction-only: a repo cannot keep a
    variable the machine default drops."""
    cfg = _read_json(worktree / ".orchestration" / "config.json")
    return set((cfg.get("agentEnv") or {}).get("drop") or [])


def gate_env_full(worktree: Path) -> bool:
    """Opt-out for gates that legitimately need the full environment:
    \"gate\": {\"env\": \"full\"} in .orchestration/config.json."""
    cfg = _read_json(worktree / ".orchestration" / "config.json")
    return str((cfg.get("gate") or {}).get("env", "filtered")).lower() == "full"


def filtered_env(base: Mapping[str, str] | None = None, extra_drop=(), extra_keep=(),
                 repo_drop=(), git: bool = True) -> dict:
    base = dict(base) if base is not None else dict(os.environ)
    m_drop, m_keep = machine_policy()
    drop = set(DEFAULT_DROP) | set(extra_drop) | set(repo_drop) | m_drop
    drop -= (set(extra_keep) | m_keep)

    out: dict[str, str] = {}
    for k, v in base.items():
        u = k.upper()
        if u in drop:
            continue
        if any(u.startswith(p) for p in DEFAULT_DROP_PREFIXES):
            continue
        if any(u.endswith(s) for s in DEFAULT_DROP_SUFFIXES):
            continue
        out[k] = v

    if git:
        for k, v in AGENT_GIT_ENV.items():
            out.setdefault(k, v)
    return out
