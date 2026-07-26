"""The CLI: mode safety, and the operator controls only a human may use.

`--mode` has no default anywhere, and every control that clears a safety state demands the typed
phrase. Until the dashboard takes the job in Phase 6, this is the whole operator surface, so its
refusals are as load-bearing as its actions.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tradebot.__main__ import main, parse_args
from tradebot.core.enums import Mode
from tradebot.risk.state import REARM_PHRASE


@pytest.fixture
def data_dir(tmp_path: Path) -> list[str]:
    return ["--data-dir", str(tmp_path)]


class TestModeSafety:
    def test_run_requires_a_mode(self) -> None:
        """No environment variable, config default or typo can select a mode (PLAN §2.4)."""
        with pytest.raises(SystemExit):
            parse_args(["run", "--once"])

    def test_every_risk_subcommand_requires_a_mode(self) -> None:
        for action in ("status", "rearm", "unhalt"):
            with pytest.raises(SystemExit):
                parse_args(["risk", action])

    def test_a_mode_outside_the_enum_is_rejected(self) -> None:
        with pytest.raises(SystemExit):
            parse_args(["run", "--mode", "prod", "--once"])

    def test_live_refuses_to_run(self, data_dir: list[str]) -> None:
        assert main(["run", "--mode", "live", "--once", *data_dir]) == 1

    def test_paper_refuses_to_run(self, data_dir: list[str]) -> None:
        assert main(["run", "--mode", "paper", "--once", *data_dir]) == 1

    def test_live_refuses_even_with_the_confirmation_phrase(self, data_dir: list[str]) -> None:
        """Live ships disabled: there is no wiring to reach, confirmation or not (Phase 8)."""
        exit_code = main(
            [
                "run",
                "--mode",
                "live",
                "--once",
                "--confirm",
                "I ACCEPT REAL MONEY RISK",
                *data_dir,
            ]
        )
        assert exit_code == 1

    def test_continuous_scheduling_is_refused_rather_than_faked(self, data_dir: list[str]) -> None:
        assert main(["run", "--mode", "sim", *data_dir]) == 2


class TestRunCommand:
    def test_a_sim_cycle_runs_and_exits_clean(self, data_dir: list[str]) -> None:
        assert main(["run", "--mode", "sim", "--once", *data_dir]) == 0

    def test_a_second_run_recovers_from_the_first_ones_log(self, data_dir: list[str]) -> None:
        assert main(["run", "--mode", "sim", "--once", *data_dir]) == 0
        assert main(["run", "--mode", "sim", "--once", *data_dir]) == 0


class TestRiskCommands:
    def test_status_reports_without_changing_anything(self, data_dir: list[str]) -> None:
        main(["run", "--mode", "sim", "--once", *data_dir])

        assert main(["risk", "status", "--mode", "sim", *data_dir]) == 0

    def test_rearming_demands_the_typed_phrase(self, data_dir: list[str]) -> None:
        main(["run", "--mode", "sim", "--once", *data_dir])

        assert main(["risk", "rearm", "--mode", "sim", "--confirm", "yes", *data_dir]) == 1

    def test_rearming_with_the_phrase_succeeds(self, data_dir: list[str]) -> None:
        main(["run", "--mode", "sim", "--once", *data_dir])

        exit_code = main(["risk", "rearm", "--mode", "sim", "--confirm", REARM_PHRASE, *data_dir])

        assert exit_code == 0

    def test_un_halting_demands_the_typed_phrase(self, data_dir: list[str]) -> None:
        main(["run", "--mode", "sim", "--once", *data_dir])

        assert main(["risk", "unhalt", "demo", "--mode", "sim", "--confirm", "ok", *data_dir]) == 1

    def test_un_halting_with_the_phrase_succeeds(self, data_dir: list[str]) -> None:
        main(["run", "--mode", "sim", "--once", *data_dir])

        exit_code = main(
            ["risk", "unhalt", "demo", "--mode", "sim", "--confirm", REARM_PHRASE, *data_dir]
        )

        assert exit_code == 0

    def test_a_halted_process_refuses_to_run_a_cycle(self, data_dir: list[str]) -> None:
        """A tripped switch survives the restart, and the exit code says so."""
        main(["run", "--mode", "sim", "--once", *data_dir])
        _trip(Path(data_dir[1]))

        assert main(["run", "--mode", "sim", "--once", *data_dir]) == 3


def _trip(data_root: Path) -> None:
    """Trip the persisted switch directly, as a real breach in a previous process would have."""
    import sqlite3

    with sqlite3.connect(data_root / "sim.db") as connection:
        connection.execute("UPDATE risk_state SET kill_switch = 'tripped', reason = 'test'")


def test_each_mode_uses_its_own_database_file() -> None:
    from tradebot.app import database_path

    assert len({str(database_path(mode)) for mode in Mode}) == len(Mode)
