from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from orch import cron, tasktool  # noqa: E402


def _epoch(year: int, month: int, day: int, hour: int, minute: int) -> int:
    """A local-time epoch, because cron schedules are evaluated in local time."""
    return int(time.mktime((year, month, day, hour, minute, 0, 0, 0, -1)))


def test_all_stars_match_every_minute() -> None:
    spec = cron.parse("* * * * *")
    at = _epoch(2024, 1, 15, 9, 0)
    assert spec.matches(at)
    assert spec.matches(at + 59)
    assert spec.matches(at + 60)
    print("ok: `* * * * *` matches every minute")


def test_exact_minute_and_hour() -> None:
    spec = cron.parse("0 9 * * *")
    assert spec.matches(_epoch(2024, 1, 15, 9, 0))
    assert not spec.matches(_epoch(2024, 1, 15, 9, 1)), \
        "the minute must match exactly"
    assert not spec.matches(_epoch(2024, 1, 15, 10, 0)), \
        "the hour must match exactly"
    print("ok: an exact minute/hour schedule matches only that slot")


def test_steps_ranges_lists_and_names() -> None:
    weekdays = cron.parse("*/15 9-17 * * mon-fri")
    assert weekdays.matches(_epoch(2024, 1, 15, 9, 0))     # Monday
    assert weekdays.matches(_epoch(2024, 1, 15, 17, 45))
    assert not weekdays.matches(_epoch(2024, 1, 15, 18, 0))
    assert not weekdays.matches(_epoch(2024, 1, 20, 9, 0))  # Saturday

    assert cron.parse("0 0 1 jan *").matches(_epoch(2024, 1, 1, 0, 0))
    assert cron.parse("0 0 * * 7").matches(_epoch(2024, 1, 14, 0, 0))
    print("ok: steps, ranges, lists, month and weekday names all parse")


def test_day_of_month_or_day_of_week() -> None:
    spec = cron.parse("0 0 1 * mon")
    assert spec.matches(_epoch(2024, 1, 1, 0, 0)), "the 1st (also a Monday)"
    assert spec.matches(_epoch(2024, 1, 8, 0, 0)), "a plain Monday"
    assert not spec.matches(_epoch(2024, 1, 2, 0, 0)), \
        "neither the 1st nor a Monday"
    print("ok: restricted day-of-month and weekday are ORed, as cron does")


def test_invalid_schedules_raise() -> None:
    for bad in ("", "* * * *", "60 * * * *", "* 24 * * *",
                "*/0 * * * *", "a b c d e", "5-1 * * * *"):
        try:
            cron.parse(bad)
        except cron.CronError:
            continue
        raise AssertionError(f"{bad!r} should have been rejected")
    print("ok: malformed schedules raise CronError")


def test_due_slot_catches_the_crossing() -> None:
    spec = cron.parse("0 * * * *")
    prev = _epoch(2024, 1, 15, 9, 55)
    now = _epoch(2024, 1, 15, 10, 5)
    assert cron.due_slot(spec, prev, now) == _epoch(2024, 1, 15, 10, 0)
    print("ok: a slot that fell between two checks is still seen")


def test_due_slot_fires_once_per_slot() -> None:
    spec = cron.parse("0 * * * *")
    slot = _epoch(2024, 1, 15, 10, 0)
    assert cron.due_slot(spec, slot - 30, slot + 30) == slot
    assert cron.due_slot(spec, slot, slot + 30) is None, \
        "the same minute must not fire twice"
    print("ok: a due slot fires once, even though the daemon ticks every second")


def test_due_slot_does_not_replay_before_the_window() -> None:
    spec = cron.parse("0 * * * *")
    assert cron.due_slot(spec, _epoch(2024, 1, 15, 8, 0),
                         _epoch(2024, 1, 15, 8, 30)) is None
    print("ok: no slot in the window means no fire")


def test_jobs_require_only_name_schedule_prompt() -> None:
    jobs, errors = cron.jobs_from_config(
        {"cron": {"jobs": [
            {"name": "qa", "schedule": "0 9 * * *", "prompt": "Run QA"}]}},
        "/p", None, "main")
    assert not errors, errors
    assert len(jobs) == 1
    job = jobs[0]
    assert job.name == "qa" and job.prompt == "Run QA"
    assert job.model is None and job.agent is None
    print("ok: name + schedule + prompt is all a job needs")


def test_model_and_agent_are_optional_parameters() -> None:
    jobs, errors = cron.jobs_from_config(
        {"cron": {"jobs": [
            {"name": "qa", "schedule": "0 9 * * *", "prompt": "Run QA",
             "model": "provider/model", "agent": "build"}]}},
        "/p", None, "main")
    assert not errors, errors
    assert jobs[0].model == "provider/model"
    assert jobs[0].agent == "build"
    print("ok: model and agent ride through when present")


def test_prompt_file() -> None:
    with tempfile.TemporaryDirectory() as d:
        wt = Path(d)
        rel = Path(".orchestration/prompts/security.md")
        (wt / rel).parent.mkdir(parents=True)
        (wt / rel).write_text("Check dependencies and secrets\n")
        jobs, errors = cron.jobs_from_config(
            {"cron": {"jobs": [
                {"name": "security", "schedule": "0 * * * *",
                 "promptFile": str(rel)}]}},
            "/p", wt, "main")
        assert not errors, errors
        assert jobs[0].prompt == "Check dependencies and secrets\n"
    print("ok: promptFile is read when prompt is absent")


def test_bad_entries_are_reported_not_fired() -> None:
    jobs, errors = cron.jobs_from_config(
        {"cron": {"jobs": [
            {"name": "bad", "schedule": "nope", "prompt": "x"},
            {"name": "", "schedule": "* * * * *", "prompt": "x"},
            {"name": "noprompt", "schedule": "* * * * *"},
        ]}},
        "/p", None, "main")
    assert jobs == [], "nothing invalid may be fired"
    assert len(errors) == 3, errors
    print("ok: invalid entries are reported and skipped, never fired")


def test_disabled_block_and_entry() -> None:
    jobs, errors = cron.jobs_from_config(
        {"cron": {"enabled": False, "jobs": [
            {"name": "qa", "schedule": "0 9 * * *", "prompt": "Run QA"}]}},
        "/p", None, "main")
    assert jobs == [] and errors == []

    jobs, errors = cron.jobs_from_config(
        {"cron": {"jobs": [
            {"name": "qa", "schedule": "0 9 * * *", "prompt": "Run QA",
             "enabled": False}]}},
        "/p", None, "main")
    assert jobs == [] and errors == []
    print("ok: enabled:false disables the block or one entry")


def test_duplicate_names_are_reported() -> None:
    jobs, errors = cron.jobs_from_config(
        {"cron": {"jobs": [
            {"name": "qa", "schedule": "0 9 * * *", "prompt": "one"},
            {"name": "qa", "schedule": "0 10 * * *", "prompt": "two"},
        ]}},
        "/p", None, "main")
    assert len(jobs) == 1, "only the first declaration is kept"
    assert any("declared twice" in e for e in errors), errors
    print("ok: duplicate job names are reported, not silently merged")


def _repo() -> tuple[Path, tempfile.TemporaryDirectory]:
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name) / "wt"
    root.mkdir(parents=True)
    subprocess.run(["git", "-C", str(root), "init", "-q", "-b", "main"],
                   check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "t@t"],
                   check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "t"],
                   check=True)
    orch = root / ".orchestration"
    orch.mkdir(parents=True)
    (orch / "config.json").write_text('{"baseBranch": "main"}\n')
    (root / "seed").write_text("x")
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", "seed"],
                   check=True)
    return root, tmp


def _git(root: Path, *args: str) -> str:
    p = subprocess.run(["git", "-C", str(root), *args],
                       capture_output=True, text=True, check=True)
    return p.stdout.strip()


def test_add_list_update_remove_a_job() -> None:
    root, tmp = _repo()
    try:
        added = cron.add_job(root, {"name": "QA Pass", "schedule": "0 9 * * *",
                                    "prompt": "Run QA"}, commit=True)
        assert added["committed"], added
        assert added["job"]["name"] == "qa-pass", "names are slugified"

        listing = cron.list_jobs(root)
        assert [j["name"] for j in listing["jobs"]] == ["qa-pass"]
        assert listing["jobs"][0]["schedule"] == "0 9 * * *"
        assert listing["jobs"][0]["valid"] is True

        updated = cron.update_job(
            root, {"name": "qa-pass", "schedule": "0 * * * *",
                   "agent": "build", "model": "provider/model"},
            commit=True)
        assert updated["committed"], updated
        job = cron.list_jobs(root)["jobs"][0]
        assert job["schedule"] == "0 * * * *"
        assert job["agent"] == "build"
        assert job["model"] == "provider/model"

        removed = cron.remove_job(root, {"name": "qa-pass"}, commit=True)
        assert removed["committed"], removed
        assert cron.list_jobs(root)["jobs"] == []
    finally:
        tmp.cleanup()
    print("ok: cron add/list/update/remove round-trips through committed config")


def test_a_job_without_commit_stays_uncommitted() -> None:
    root, tmp = _repo()
    try:
        r = cron.add_job(root, {"name": "qa", "schedule": "0 9 * * *",
                                "prompt": "Run QA"}, commit=False)
        assert not r["committed"]
        dirty = _git(root, "status", "--porcelain")
        assert ".orchestration/config.json" in dirty
        assert "uncommitted" in r["message"]
    finally:
        tmp.cleanup()
    print("ok: commit=false writes the job but leaves the config for review")


def test_an_already_dirty_config_is_not_committed() -> None:
    root, tmp = _repo()
    try:
        cfg_path = root / ".orchestration" / "config.json"
        cfg = json.loads(cfg_path.read_text())
        cfg["$comment"] = "a human edit in progress"
        cfg_path.write_text(json.dumps(cfg, indent=2) + "\n")
        r = cron.add_job(root, {"name": "qa", "schedule": "0 9 * * *",
                                "prompt": "Run QA"}, commit=True)
        assert not r["committed"], "must not sweep someone else's edit"
        assert "already had uncommitted changes" in r["message"]
    finally:
        tmp.cleanup()
    print("ok: a dirty config is written but not committed for you")


def test_add_rejects_a_duplicate_and_a_missing_prompt_file() -> None:
    root, tmp = _repo()
    try:
        cron.add_job(root, {"name": "qa", "schedule": "0 9 * * *",
                            "prompt": "Run QA"})
        try:
            cron.add_job(root, {"name": "qa", "schedule": "0 * * * *",
                                "prompt": "again"})
        except cron.CronToolError as e:
            assert "already exists" in str(e)
        else:
            raise AssertionError("a duplicate name must be refused")

        try:
            cron.add_job(root, {"name": "security",
                                "schedule": "0 * * * *",
                                "promptFile": ".orchestration/missing.md"})
        except cron.CronToolError as e:
            assert "promptFile" in str(e)
        else:
            raise AssertionError("a missing promptFile must be refused")
    finally:
        tmp.cleanup()
    print("ok: duplicate names and missing prompt files are refused")


def test_tasktool_routes_the_cron_ops() -> None:
    root, tmp = _repo()
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = tasktool.main([
                "cron_add", "--json",
                json.dumps({"name": "qa", "schedule": "0 9 * * *",
                            "prompt": "Run QA", "commit": True}),
                "--dir", str(root),
            ])
        out = json.loads(buf.getvalue())
        assert rc == 0 and out["ok"], out
        assert out["job"]["name"] == "qa"

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = tasktool.main(["cron_list", "--json", "{}", "--dir", str(root)])
        out = json.loads(buf.getvalue())
        assert rc == 0 and out["ok"] and len(out["jobs"]) == 1, out

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = tasktool.main([
                "cron_update", "--json",
                json.dumps({"name": "qa", "schedule": "0 * * * *",
                            "agent": "build"}),
                "--dir", str(root),
            ])
        out = json.loads(buf.getvalue())
        assert rc == 0 and out["ok"] and out["job"]["agent"] == "build", out

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = tasktool.main(["cron_remove", "--json",
                                json.dumps({"name": "qa"}), "--dir", str(root)])
        out = json.loads(buf.getvalue())
        assert rc == 0 and out["ok"] and out["job"]["name"] == "qa", out
    finally:
        tmp.cleanup()
    print("ok: the custom-tool backend routes cron_list/add/update/remove")


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"\n{len(tests)} cron tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
