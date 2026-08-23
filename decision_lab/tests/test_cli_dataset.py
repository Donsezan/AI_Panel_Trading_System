"""`dataset verify` writes the sidecar and answers with an exit code (spec §13).

Exit codes are the contract a script acts on, so they are asserted rather than described: 3 means
"the dataset is not fit to build on", and it is the same 3 whether the sidecar is missing, the
data is holed beyond repair, or no day set has been pinned.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from decision_lab import cli
from decision_lab.calibration_days import read as read_days
from decision_lab.dataset import read_audit
from decision_lab.tests import factories as f


def test_a_clean_dataset_verifies_and_writes_the_sidecar(tmp_path: Path) -> None:
    inst = f.instrument()
    f.write_dataset(tmp_path, {(inst, "1h"): f.walk([str(100 + i) for i in range(48)])})

    code = cli.main(["dataset", "verify", "--data", str(tmp_path)])

    assert code == cli.EXIT_OK
    audit = read_audit(tmp_path)
    assert audit.is_clean
    assert audit.series["binance:BTC/USDT|1h"].present == 48


def test_a_holed_dataset_refuses_with_the_dataset_code(tmp_path: Path) -> None:
    """The sidecar is still written — the operator needs to see *what* is holed."""
    inst = f.instrument()
    f.write_dataset(tmp_path, {(inst, "1h"): f.drop_bars(f.walk(["100"] * 48), at=20, count=6)})

    code = cli.main(["dataset", "verify", "--data", str(tmp_path)])

    assert code == cli.EXIT_DATASET
    assert not read_audit(tmp_path).is_clean


def test_a_missing_dataset_refuses_with_the_dataset_code(tmp_path: Path) -> None:
    code = cli.main(["dataset", "verify", "--data", str(tmp_path / "nowhere")])
    assert code == cli.EXIT_DATASET


def test_an_unknown_command_is_misuse() -> None:
    with pytest.raises(SystemExit) as exit_:
        cli.main(["nonsense"])
    assert exit_.value.code == cli.EXIT_MISUSE


def pin_a_day_set(directory: Path) -> int:
    """Sixty days with the shock counts `test_calibration_days` explains, verified and pinned."""
    bars = f.shocked_walk(days=60, shock_up=(3, 11, 19, 27), shock_down=(7, 15, 23))
    f.write_dataset(directory, {(f.instrument(), "1h"): bars})
    assert cli.main(["dataset", "verify", "--data", str(directory)]) == cli.EXIT_OK
    return cli.main(["dataset", "days", "--data", str(directory)])


def test_days_pins_nine_days_and_writes_the_file(tmp_path: Path) -> None:
    assert pin_a_day_set(tmp_path) == cli.EXIT_OK

    days = read_days(tmp_path)
    assert len(days.all_days) == 9
    assert days.dataset_digest == read_audit(tmp_path).dataset_digest


def test_a_second_run_reports_the_pinned_set_rather_than_redrawing_it(tmp_path: Path) -> None:
    """The file is the authority. Redrawing on every invocation would make it decoration."""
    pin_a_day_set(tmp_path)
    first = read_days(tmp_path)

    assert cli.main(["dataset", "days", "--data", str(tmp_path), "--seed", "999"]) == cli.EXIT_OK

    assert read_days(tmp_path) == first


def test_reselect_is_what_moves_the_digest(tmp_path: Path) -> None:
    pin_a_day_set(tmp_path)
    first = read_days(tmp_path)

    code = cli.main(["dataset", "days", "--data", str(tmp_path), "--seed", "999", "--reselect"])

    assert code == cli.EXIT_OK
    assert read_days(tmp_path).dayset_digest != first.dayset_digest


def test_pinning_a_day_over_an_existing_set_needs_reselect_too(tmp_path: Path) -> None:
    """`--pin` moves `dayset_digest` exactly as `--reselect` does, so it may not do it quietly."""
    pin_a_day_set(tmp_path)
    first = read_days(tmp_path)

    code = cli.main(["dataset", "days", "--data", str(tmp_path), "--pin", "2024-01-08"])

    assert code == cli.EXIT_DATASET
    assert read_days(tmp_path) == first


def test_days_refuses_an_unverified_dataset(tmp_path: Path) -> None:
    """The pools are drawn from a distribution a hole would have moved (§4.4)."""
    f.write_dataset(tmp_path, {(f.instrument(), "1h"): f.shocked_walk(days=60)})

    assert cli.main(["dataset", "days", "--data", str(tmp_path)]) == cli.EXIT_DATASET


def test_verify_without_repair_never_reaches_the_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--repair` reaches a venue. It must never be reachable by a default or a typo.

    Proved by making the provider impossible to construct: a run that touched it would raise
    rather than pass, so this cannot go quiet the way an assertion on a call count can.
    """

    def forbidden(*args: object, **kwargs: object) -> object:
        raise AssertionError("verify built a venue provider without --repair")

    monkeypatch.setattr(cli, "_history_provider", forbidden)
    f.write_dataset(tmp_path, {(f.instrument(), "1h"): f.walk(["100"] * 48)})

    assert cli.main(["dataset", "verify", "--data", str(tmp_path)]) == cli.EXIT_OK
