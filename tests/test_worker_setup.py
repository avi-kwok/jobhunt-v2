"""deploy/v2: crontab rendering/merging in worker-setup.sh, backup retention, shellcheck."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parent.parent / "deploy" / "v2"
SETUP = DEPLOY / "worker-setup.sh"
HOME_DIR = "/home/ubuntu/jobhunt-v2"


def _bash(snippet: str, stdin: str = "") -> str:
    r = subprocess.run(
        ["bash", "-c", f'source "{SETUP}"; {snippet}'],
        input=stdin, capture_output=True, text=True, check=True,
    )
    return r.stdout


def _merge(existing: str) -> str:
    return _bash(f'merge_cron "{HOME_DIR}"', existing)


def test_sourcing_does_not_run_main():
    assert _bash("echo sourced") == "sourced\n"


def test_block_has_three_jobs_with_required_shape():
    out = _bash(f'render_cron_block "{HOME_DIR}"')
    lines = out.splitlines()
    assert lines[0].startswith("# BEGIN jobhunt-v2") and lines[-1] == "# END jobhunt-v2"
    jobs = [ln for ln in lines if not ln.startswith("#")]
    assert len(jobs) == 3
    scan, beat, backup = jobs
    assert scan.startswith("*/30 * * * * cd " + HOME_DIR)
    assert "flock -n" in scan and "timeout 1800" in scan and "jobhunt scan" in scan
    assert scan.endswith(">> logs/worker.log 2>&1")
    assert beat.startswith("0 16 * * * ") and "jobhunt heartbeat" in beat
    assert backup.startswith("0 9 * * * ") and "deploy/v2/backup.sh" in backup


def test_merge_into_empty_crontab():
    out = _merge("")
    assert out.count("# BEGIN jobhunt-v2") == 1 and out.count("# END jobhunt-v2") == 1


def test_merge_is_idempotent_and_keeps_foreign_lines():
    foreign = "MAILTO=me@example.com\n0 1 * * * /usr/bin/true\n"
    once = _merge(foreign)
    twice = _merge(once)
    assert once == twice
    assert once.count("jobhunt scan") == 1
    assert once.startswith(foreign)


def test_merge_replaces_stale_block():
    stale = _merge("").replace("*/30", "*/5").replace("timeout 1800", "timeout 60")
    out = _merge("0 2 * * * keep-me\n" + stale)
    assert "*/5 " not in out and "timeout 60" not in out
    assert "keep-me" in out and out.count("# BEGIN jobhunt-v2") == 1


@pytest.mark.skipif(not shutil.which("shellcheck"), reason="shellcheck not installed")
def test_shellcheck_clean():
    r = subprocess.run(["shellcheck", str(SETUP), str(DEPLOY / "backup.sh")],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout


def test_backup_keeps_seven_newest(tmp_path):
    repo, bindir, dest = tmp_path / "repo", tmp_path / "bin", tmp_path / "backups"
    (repo / ".venv" / "bin").mkdir(parents=True)
    bindir.mkdir()
    dest.mkdir()
    # Stand-ins: a "python" that prints a URL and a "pg_dump" that writes its --file target.
    py = repo / ".venv" / "bin" / "python"
    py.write_text('#!/bin/sh\necho postgresql://u:p@h:5432/db\n')
    dump = bindir / "pg_dump"
    dump.write_text('#!/bin/bash\nfor a in "$@"; do case $a in --file=*) echo data > "${a#--file=}";; esac; done\n')
    for f in (py, dump):
        f.chmod(0o755)
    for i in range(9):  # pre-existing dumps, oldest first
        f = dest / f"jobhunt-2026010{i + 1}-000000.dump"
        f.write_text("old")
        os.utime(f, (1_000_000 + i, 1_000_000 + i))
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", "BACKUP_DIR": str(dest)}
    r = subprocess.run(["bash", str(DEPLOY / "backup.sh")], cwd=repo, env=env,
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    left = sorted(p.name for p in dest.glob("jobhunt-*.dump"))
    assert len(left) == 7
    assert "jobhunt-20260101-000000.dump" not in left  # oldest pruned
    assert not list(dest.glob("*.tmp"))
    assert "postgresql://" not in r.stdout + r.stderr


def test_backup_failure_leaves_no_partial_file(tmp_path):
    repo, bindir, dest = tmp_path / "repo", tmp_path / "bin", tmp_path / "backups"
    (repo / ".venv" / "bin").mkdir(parents=True)
    bindir.mkdir()
    py = repo / ".venv" / "bin" / "python"
    py.write_text('#!/bin/sh\necho postgresql://u:p@h:5432/db\n')
    dump = bindir / "pg_dump"
    dump.write_text("#!/bin/bash\nexit 1\n")
    py.chmod(0o755)
    dump.chmod(0o755)
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", "BACKUP_DIR": str(dest)}
    r = subprocess.run(["bash", str(DEPLOY / "backup.sh")], cwd=repo, env=env,
                       capture_output=True, text=True)
    assert r.returncode == 1
    assert not list(dest.glob("*"))
