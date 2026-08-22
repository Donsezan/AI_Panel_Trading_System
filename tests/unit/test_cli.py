"""The CLI: mode safety, and the operator controls only a human may use.

`--mode` has no default anywhere, and every control that clears a safety state demands the typed
phrase. The CLI and the dashboard are two doors onto the same controls, so its refusals are as
load-bearing as its actions.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from tests.unit.test_migration_backup import BEHIND, database_at, revision_of

from tradebot.__main__ import _race, main, parse_args
from tradebot.app import Application
from tradebot.core.enums import Mode
from tradebot.dashboard.auth import TOKEN_ENV
from tradebot.persistence.database import create_database
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

    def test_live_refuses_to_run_unarmed(self, data_dir: list[str]) -> None:
        assert main(["run", "--mode", "live", "--once", *data_dir]) == 1

    def test_paper_will_not_price_a_sim_basket_off_a_real_venue(self, data_dir: list[str]) -> None:
        """The seeded basket names venue `sim`; paper wires real Binance data.

        The mismatch is now caught by the startup reference-data check, which names it — "this
        process is wired to 'binance'" — and halts the basket, rather than letting it surface a
        step later as a data fault inside a cycle (ADR 0025). `--once` still reports it to the
        shell: a run that cycled nothing must not look like a run that went fine.
        """
        assert main(["run", "--mode", "paper", "--once", *data_dir]) == 3

    def test_live_refuses_with_the_phrase_but_an_unarmed_database(
        self, data_dir: list[str]
    ) -> None:
        """The phrase is transient by design; an armed row in *this* database is not (ADR 0012).

        Live is wired as of Phase 8, and ships disarmed: the wiring is reachable only by someone
        who has also armed the database, set a cap, and put live keys in the environment.
        """
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

    def test_live_refuses_the_simulated_broker(self, data_dir: list[str]) -> None:
        """`--broker sim` defaults everywhere else; in live it is a contradiction, not a default."""
        assert main(["run", "--mode", "live", "--once", "--broker", "sim", *data_dir]) == 1


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


class TestTheAlertTailDoesNotEndTheProcess:
    """Alerting is off unless a destination is configured, and off must mean *quiet*, not *over*.

    `AlertDispatcher.run` returns immediately when nothing is configured — the default for sim and
    paper. Racing it against the long-lived tasks made that return the end of the process: `serve`
    exited before a browser could reach it and `run` exited before a basket cycled.
    """

    async def test_a_disabled_tail_does_not_stop_what_it_runs_beside(
        self, sim_application: Application
    ) -> None:
        assert not sim_application.alerts.enabled
        served = asyncio.Event()
        race = asyncio.create_task(_race(sim_application, ("long-lived", served.wait())))

        for _ in range(5):  # more than enough turns for the tail to log and return
            await asyncio.sleep(0)

        assert not race.done(), "the disabled alert tail ended the process"
        served.set()
        await race

    async def test_the_first_long_lived_task_to_finish_still_stops_the_rest(
        self, sim_application: Application
    ) -> None:
        """The other half of the contract: a stopped server must not leave a supervisor cycling."""
        survivor = asyncio.Event()

        await _race(sim_application, ("first", asyncio.sleep(0)), ("second", survivor.wait()))

        assert not survivor.is_set()


class TestValidationCommands:
    """`backtest` and `report` — the Phase 7 surfaces. Both write a file and never print."""

    def test_a_backtest_is_a_simulation_and_refuses_any_other_mode(
        self, data_dir: list[str], tmp_path: Path
    ) -> None:
        """There is no such thing as a paper backtest: it would mean replaying into a venue."""
        for mode in ("paper", "live"):
            exit_code = main(
                ["backtest", "run", "--mode", mode, "--data", str(tmp_path), *data_dir]
            )
            assert exit_code == 2

    def test_a_directory_that_is_not_a_dataset_refuses(
        self, data_dir: list[str], tmp_path: Path
    ) -> None:
        exit_code = main(["backtest", "run", "--mode", "sim", "--data", str(tmp_path), *data_dir])

        assert exit_code == 1

    def test_a_recorded_dataset_replays_and_writes_a_report(
        self, data_dir: list[str], tmp_path: Path
    ) -> None:
        history = _record_history(tmp_path / "history")
        out = tmp_path / "backtest.md"

        exit_code = main(
            [
                "backtest",
                "run",
                "--mode",
                "sim",
                "--data",
                str(history),
                "--out",
                str(out),
                *data_dir,
            ]
        )

        assert exit_code == 0
        report = out.read_text(encoding="utf-8")
        assert "NOT ALPHA EVIDENCE" in report
        assert "# Backtest report" in report

    def test_fetching_history_never_defaults_to_a_venue_window(self) -> None:
        """Both edges are required: a recorder with a default window is a recorder that
        downloads something nobody asked for."""
        with pytest.raises(SystemExit):
            parse_args(["backtest", "fetch", "--symbol", "BTC/USDT"])

    def test_a_fresh_database_fails_the_promotion_gates_and_says_so_in_its_exit_code(
        self, data_dir: list[str], tmp_path: Path
    ) -> None:
        main(["run", "--mode", "sim", "--once", *data_dir])
        out = tmp_path / "promotion.md"

        exit_code = main(["report", "promotion", "--mode", "sim", "--out", str(out), *data_dir])

        assert exit_code == 5
        report = out.read_text(encoding="utf-8")
        assert "**Automatic gates: FAILED.**" in report
        assert "## Sign-off" in report

    def test_a_soak_that_clears_every_gate_still_only_asks_for_a_human(
        self, data_dir: list[str], tmp_path: Path
    ) -> None:
        """`--min-cycles 1` is how the gate is exercised without a four-week soak."""
        main(["run", "--mode", "sim", "--once", *data_dir])
        out = tmp_path / "promotion.md"

        exit_code = main(
            [
                "report",
                "promotion",
                "--mode",
                "sim",
                "--min-cycles",
                "1",
                "--out",
                str(out),
                *data_dir,
            ]
        )

        assert exit_code == 0
        assert "**Automatic gates: PASSED.**" in out.read_text(encoding="utf-8")

    def test_a_shadow_report_over_a_soak_that_ran_no_challenger_says_so(
        self, data_dir: list[str], tmp_path: Path
    ) -> None:
        """The seeded basket has no `shadow_panel`, and an empty report must explain itself."""
        main(["run", "--mode", "sim", "--once", *data_dir])
        out = tmp_path / "shadow.md"

        exit_code = main(["report", "shadow", "--mode", "sim", "--out", str(out), *data_dir])

        assert exit_code == 0
        report = out.read_text(encoding="utf-8")
        assert "# Shadow A/B comparison" in report
        assert "**No shadow evaluation ran in this window.**" in report


def _record_history(directory: Path) -> Path:
    """A small recorded dataset on disk, produced by the real recorder."""
    import asyncio
    from datetime import UTC, datetime, timedelta
    from decimal import Decimal

    from tradebot.core.clock import ManualClock
    from tradebot.core.enums import AssetClass
    from tradebot.core.instrument import Instrument
    from tradebot.marketdata.recorder import record
    from tradebot.marketdata.replay import ReplayMarketData, synthetic_candles

    start = datetime(2026, 1, 1, tzinfo=UTC)
    bars = 400
    instrument = Instrument(
        symbol="BTC/USDT",
        venue="binance",
        asset_class=AssetClass.CRYPTO,
        base_currency="BTC",
        quote_currency="USDT",
        lot_size=Decimal("0.00001"),
        tick_size=Decimal("0.01"),
        min_qty=Decimal("0.00001"),
        min_notional=Decimal("10"),
    )
    clock = ManualClock(start + timedelta(hours=bars))
    source = ReplayMarketData(
        {
            (instrument.key, "1h"): synthetic_candles(
                start=start, timeframe="1h", count=bars, open_price=Decimal("50000")
            )
        },
        clock,
    )
    asyncio.run(
        record(
            source,
            (instrument,),
            ("1h",),
            start=start,
            end=start + timedelta(hours=bars),
            directory=directory,
            clock=clock,
            source="synthetic fixture",
        )
    )
    return directory


class TestMaintenanceCommands:
    """Backups on demand — from a command that may be run while the bot is cycling.

    Which is why it builds no `Application`: a second one would open a second writer against the
    same file. It opens the database without migrating it for the mirror-image reason — the whole
    point of copying `live.db` before a release is to have a rollback point *for* that release.
    """

    def test_a_backup_writes_a_copy_that_opens_as_a_database(
        self, data_dir: list[str], tmp_path: Path
    ) -> None:
        create_database(tmp_path / "sim.db")

        assert main(["maintenance", "backup", "--mode", "sim", *data_dir]) == 0

        (copy,) = (tmp_path / "backups" / "sim").glob("*.db")
        with sqlite3.connect(copy) as connection:
            names = connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            assert "events" in {row[0] for row in names}

    def test_a_refused_backup_exits_rather_than_raising(
        self, data_dir: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A supervisor script has to be able to tell "no room" from "crashed"."""
        create_database(tmp_path / "sim.db")
        monkeypatch.setattr("tradebot.maintenance.backup.free_bytes", lambda _: 1024)

        assert main(["maintenance", "backup", "--mode", "sim", *data_dir]) == 6

    def test_the_backup_dir_flag_overrides_the_default_location(
        self, data_dir: list[str], tmp_path: Path
    ) -> None:
        create_database(tmp_path / "sim.db")
        elsewhere = tmp_path / "elsewhere"

        exit_code = main(
            ["maintenance", "backup", "--mode", "sim", "--backup-dir", str(elsewhere), *data_dir]
        )

        assert exit_code == 0
        assert list(elsewhere.glob("*.db"))

    def test_a_backup_never_migrates_the_database_it_copies(
        self, data_dir: list[str], tmp_path: Path
    ) -> None:
        """The rollback point must be of the schema being left, not the one being moved to."""
        database_at(tmp_path / "sim.db", BEHIND)

        assert main(["maintenance", "backup", "--mode", "sim", *data_dir]) == 0

        engine = create_engine(f"sqlite:///{tmp_path / 'sim.db'}", future=True)
        assert revision_of(engine) == BEHIND

    def test_status_reports_without_writing_anything(
        self, data_dir: list[str], tmp_path: Path
    ) -> None:
        create_database(tmp_path / "sim.db")

        assert main(["maintenance", "status", "--mode", "sim", *data_dir]) == 0
        assert not (tmp_path / "backups").exists()

    def test_a_database_that_is_not_there_is_a_misuse_not_a_crash(
        self, data_dir: list[str], tmp_path: Path
    ) -> None:
        assert main(["maintenance", "status", "--mode", "sim", *data_dir]) == 2


class TestMaintenanceCompact:
    """A deliberate manual pass, for an operator who does not want to wait for the daily tick."""

    def test_a_manual_pass_runs_and_reports(self, data_dir: list[str]) -> None:
        main(["run", "--mode", "sim", "--once", *data_dir])

        assert main(["maintenance", "compact", "--mode", "sim", *data_dir]) == 0

    def test_a_window_the_model_refuses_is_a_misuse_not_a_crash(self, data_dir: list[str]) -> None:
        """`--older-than 0` would compact cycles that are still running, so the model refuses it.

        The flags go through `MaintenancePolicy` exactly like the form does — the CLI restates no
        rule, so a window it accepts is one the daily tick would accept too.
        """
        main(["run", "--mode", "sim", "--once", *data_dir])

        assert (
            main(["maintenance", "compact", "--mode", "sim", "--older-than", "0", *data_dir]) == 2
        )

    def test_inverted_windows_are_refused_by_the_same_model_rule(self, data_dir: list[str]) -> None:
        main(["run", "--mode", "sim", "--once", *data_dir])

        exit_code = main(
            [
                "maintenance",
                "compact",
                "--mode",
                "sim",
                "--older-than",
                "90",
                "--keep-days",
                "30",
                *data_dir,
            ]
        )

        assert exit_code == 2

    def test_an_override_is_recorded_on_the_event_as_an_override(
        self, data_dir: list[str], tmp_path: Path
    ) -> None:
        """So "why did that get deleted" survives a pass that did not use the published policy."""
        main(["run", "--mode", "sim", "--once", *data_dir])

        main(
            [
                "maintenance",
                "compact",
                "--mode",
                "sim",
                "--older-than",
                "45",
                "--keep-days",
                "120",
                *data_dir,
            ]
        )

        with sqlite3.connect(tmp_path / "sim.db") as connection:
            (payload,) = connection.execute(
                "SELECT payload_json FROM events WHERE type = 'MAINTENANCE_RAN'"
            ).fetchone()
        recorded = json.loads(payload)
        assert recorded["compact_after_days"] == 45
        assert recorded["archive_keep_days"] == 120
        assert recorded["overridden"] is True
