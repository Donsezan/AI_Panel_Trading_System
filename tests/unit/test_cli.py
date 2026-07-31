"""The CLI: mode safety, and the operator controls only a human may use.

`--mode` has no default anywhere, and every control that clears a safety state demands the typed
phrase. The CLI and the dashboard are two doors onto the same controls, so its refusals are as
load-bearing as its actions.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tradebot.__main__ import main, parse_args
from tradebot.core.enums import Mode
from tradebot.dashboard.auth import TOKEN_ENV
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

    def test_paper_will_not_price_a_sim_basket_off_a_real_venue(self, data_dir: list[str]) -> None:
        """The seeded basket names venue `sim`; paper wires real Binance data.

        The mismatch is refused rather than papered over, and `--once` reports the failed cycle to
        the shell instead of exiting clean — a supervised run would retry it, a single-shot one has
        nothing to retry.
        """
        assert main(["run", "--mode", "paper", "--once", *data_dir]) == 4

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


class TestRunCommand:
    def test_a_sim_cycle_runs_and_exits_clean(self, data_dir: list[str]) -> None:
        assert main(["run", "--mode", "sim", "--once", *data_dir]) == 0

    def test_a_second_run_recovers_from_the_first_ones_log(self, data_dir: list[str]) -> None:
        assert main(["run", "--mode", "sim", "--once", *data_dir]) == 0
        assert main(["run", "--mode", "sim", "--once", *data_dir]) == 0


class TestConfigCommand:
    def test_listing_shows_the_seeded_basket_and_policy(self, data_dir: list[str]) -> None:
        main(["run", "--mode", "sim", "--once", *data_dir])

        assert main(["config", "list", "--mode", "sim", *data_dir]) == 0

    def test_history_reads_every_version_of_one_document(self, data_dir: list[str]) -> None:
        main(["run", "--mode", "sim", "--once", *data_dir])

        assert main(["config", "history", "basket", "demo", "--mode", "sim", *data_dir]) == 0

    def test_a_kind_outside_the_enum_is_rejected(self) -> None:
        with pytest.raises(SystemExit):
            parse_args(["config", "history", "panels", "demo", "--mode", "sim"])


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


class TestServe:
    """`serve` refuses before it wires anything: a refusal must cost nothing and say why."""

    def test_serve_requires_a_mode(self) -> None:
        with pytest.raises(SystemExit):
            parse_args(["serve"])

    def test_serve_binds_loopback_and_supervises_by_default(self) -> None:
        args = parse_args(["serve", "--mode", "sim"])
        assert args.host == "127.0.0.1"
        assert args.port == 8000
        assert args.observe is False
        assert args.allow_remote is False

    def test_serve_refuses_without_a_dashboard_token(
        self, data_dir: list[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The same refusal live mode gives for a missing precondition (ADR 0014)."""
        monkeypatch.delenv(TOKEN_ENV, raising=False)
        assert main(["serve", "--mode", "sim", *data_dir]) == 1

    def test_serve_refuses_a_remote_bind_without_the_flag(
        self, data_dir: list[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A `--host` typo must not put the kill switch on a LAN (PLAN §3.3)."""
        monkeypatch.setenv(TOKEN_ENV, "a-token-long-enough-to-pass")
        assert main(["serve", "--mode", "sim", "--host", "0.0.0.0", *data_dir]) == 1
