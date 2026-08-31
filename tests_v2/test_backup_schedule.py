"""The backup schedule: who runs the dump, when, and which copies survive.

`v2_postgres_backup.sh` was already correct and already tested; what it never
had was anything to start it. These cover the two halves that were missing --
the systemd units that run it unattended, and the retention rule that decides
what the run is allowed to delete.
"""
from __future__ import annotations

import importlib.util
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
BACKUP = ROOT / "scripts/v2_postgres_backup.sh"


# --- the backup schedule ------------------------------------------------------

BACKUP_SERVICE = ROOT / "deploy/systemd/nemsei-v2-backup.service"
BACKUP_TIMER = ROOT / "deploy/systemd/nemsei-v2-backup.timer"


def directives(unit: Path) -> list[str]:
    """The unit's actual settings, with comments dropped."""
    return [
        line.strip()
        for line in unit.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith(("#", ";", "["))
    ]


def test_the_backup_service_runs_the_script_that_already_exists() -> None:
    """A second backup mechanism is the thing not to build."""
    settings = directives(BACKUP_SERVICE)
    exec_start = next(line for line in settings if line.startswith("ExecStart="))
    assert exec_start.endswith(f"scripts/{BACKUP.name}")
    assert "Type=oneshot" in settings


def test_the_backup_service_does_not_depend_on_a_personal_account() -> None:
    """`codex` owns the data root today; a backup must not hinge on that."""
    settings = directives(BACKUP_SERVICE)
    assert "User=root" in settings
    assert not any("codex" in line for line in settings)


def test_the_backup_units_carry_no_credentials() -> None:
    """Unit files are world-readable; the database URL is a Compose secret."""
    for unit in (BACKUP_SERVICE, BACKUP_TIMER):
        for line in directives(unit):
            if line.startswith("Environment="):
                assert not re.search(r"(?i)(password|secret|token|url)=", line), line


def test_the_backup_service_resolves_its_root_without_git() -> None:
    settings = directives(BACKUP_SERVICE)
    assert "WorkingDirectory=/opt/server/apps/Nem-sei-v2" in settings
    assert "Environment=NEMSEI_V2_REPO_ROOT=/opt/server/apps/Nem-sei-v2" in settings


def test_the_timer_is_daily_off_hours_and_recovers_a_missed_run() -> None:
    settings = directives(BACKUP_TIMER)
    calendar = next(line for line in settings if line.startswith("OnCalendar="))
    assert calendar.startswith("OnCalendar=*-*-* ")
    hour = int(calendar.split()[-1].split(":")[0])
    assert hour < 6, "the dump should not land inside the working day"
    # Without this a server that was off at 03:30 simply skips the day.
    assert "Persistent=true" in settings
    assert "Unit=nemsei-v2-backup.service" in settings
    assert "WantedBy=timers.target" in settings


def retention():
    """Load the retention rule, which lives in scripts/ like the other helpers."""
    path = ROOT / "scripts/v2_backup_retention.py"
    spec = importlib.util.spec_from_file_location("v2_backup_retention", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def dumps(*stamps):
    return [f"nemsei-v2-{stamp}T030000Z.dump" for stamp in stamps]


def test_seven_consecutive_days_are_all_kept() -> None:
    names = dumps(*(f"202608{day:02d}" for day in range(25, 32)))
    assert retention().expired(names) == []


def test_an_eighth_day_falls_out_of_the_daily_window() -> None:
    """...but only if a newer dump in its week already represents it."""
    names = dumps(*(f"202608{day:02d}" for day in range(24, 32)))
    assert retention().expired(names) == ["nemsei-v2-20260824T030000Z.dump"]


# A full week of daily dumps, so the weekly and monthly windows are what decide
# anything older rather than the daily one covering the whole archive.
LAST_WEEK = tuple(f"202608{day:02d}" for day in range(25, 32))


def test_one_dump_per_week_survives_past_the_daily_window() -> None:
    module = retention()
    # 0817 and 0810 are outside the seven days but are their weeks' newest.
    # 0803 is a fifth week, and nothing else keeps it.
    names = dumps(*LAST_WEEK, "20260817", "20260810", "20260803")
    assert module.expired(names) == ["nemsei-v2-20260803T030000Z.dump"]


def test_the_oldest_month_beyond_the_monthly_window_is_released() -> None:
    module = retention()
    # August, July and June each keep their newest; May and April do not.
    names = dumps(*LAST_WEEK, "20260817", "20260810", "20260715", "20260615", "20260515", "20260415")
    assert module.expired(names) == [
        "nemsei-v2-20260415T030000Z.dump",
        "nemsei-v2-20260515T030000Z.dump",
    ]


def test_the_windows_count_dumps_not_calendar_days() -> None:
    """A server that was off for a month comes back with its history intact."""
    module = retention()
    names = dumps("20250101", "20250102", "20250103")
    assert module.expired(names) == []


@pytest.mark.parametrize(
    "name",
    ["NOTES.txt", "nemsei-v2-lixo.dump", "nemsei-v2-.dump", "nemsei-v2-20261332T030000Z.dump"],
)
def test_a_name_the_rule_cannot_read_is_never_deleted(name) -> None:
    """Deleting a backup for having an unfamiliar name is the one unacceptable outcome."""
    module = retention()
    names = [*dumps(*(f"202608{day:02d}" for day in range(1, 32))), name]
    assert name not in module.expired(names)


def test_retention_deletes_only_what_it_reports(tmp_path: Path) -> None:
    """The end-to-end contract the backup script depends on."""
    doomed = "nemsei-v2-20260415T030000Z.dump"
    for name in dumps(*LAST_WEEK, "20260817", "20260810", "20260715", "20260615", "20260415"):
        (tmp_path / name).write_text("x", encoding="utf-8")
    before = {path.name for path in tmp_path.iterdir()}
    listed = subprocess.run(
        ["python3", str(ROOT / "scripts/v2_backup_retention.py"), "--directory", str(tmp_path)],
        capture_output=True, text=True, check=True,
    ).stdout.split()
    assert {Path(line).name for line in listed} == {doomed}
    # Listing alone removes nothing.
    assert {path.name for path in tmp_path.iterdir()} == before

    subprocess.run(
        ["python3", str(ROOT / "scripts/v2_backup_retention.py"),
         "--directory", str(tmp_path), "--delete"],
        capture_output=True, text=True, check=True,
    )
    assert {path.name for path in tmp_path.iterdir()} == before - {doomed}


def test_the_backup_script_applies_the_retention_rule_and_needs_no_git() -> None:
    script = BACKUP.read_text(encoding="utf-8")
    assert "v2_backup_retention.py" in script
    assert "--delete" in script
    # The old rule kept exactly seven days and deleted every older copy.
    assert "-mtime" not in script
    # A system timer runs this as root, where git refuses a repository it does
    # not own; the backup must not depend on resolving the root through git.
    assert "NEMSEI_V2_REPO_ROOT" in script
