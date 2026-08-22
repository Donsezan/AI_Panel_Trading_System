"""Configuration as versioned rows, not as arguments to the composition root (DESIGN §6.1).

Three properties, and each exists because of a specific way configuration goes wrong:

* **An update writes a new version; nothing is overwritten.** A cycle records the versions it ran
  on, so "why did it buy that?" is answerable against the limits in force at the time rather than
  against whatever they were changed to afterwards. Overwriting would make the event log's pins
  dangle, which quietly destroys the audit trail the log exists to be.
* **Retirement is a version too.** Deleting a basket leaves every version behind it resolvable,
  because the cycles that ran it still point at one.
* **A document may never contain a secret.** Providers carry a `secret_ref` — an environment
  variable *name* — and `put` refuses any document a known secret value or a known key shape can
  be found in. The indirection is only a control if something enforces it (PLAN §3.2).

The row and its `CONFIG_CHANGED` event are written in **one transaction**: a configuration nobody
authorised, and an authorisation of a configuration that was never stored, are both worse than a
failed write.

Failure semantics: every read fails closed. A missing or unparseable document raises
`ConfigError` rather than falling back to a default — a bot that invents a risk policy when it
cannot read one is exactly the bot that trades past a limit somebody set.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Generic, TypeVar

from sqlalchemy import Connection, Engine, func, select

from tradebot.core.clock import Clock
from tradebot.core.config import Basket, ConfigRef, GlobalRiskPolicy, MaintenancePolicy
from tradebot.core.enums import ConfigKind
from tradebot.core.errors import ConfigError
from tradebot.core.events import EventFactory
from tradebot.core.logging import SECRETS, get_logger
from tradebot.core.schema import DomainModel, canonical_json
from tradebot.persistence.database import SingleWriter
from tradebot.persistence.schema import config_versions
from tradebot.persistence.store import EventStore

logger = get_logger(__name__)

T = TypeVar("T", bound=DomainModel)

#: The id every singleton document lives under, so a singleton kind has exactly one address.
SINGLETON_ID = "global"

#: Which model each kind stores. The map is the whole registry: adding a kind is a row here plus
#: an enum member, never a new store.
DOCUMENTS: dict[ConfigKind, type[DomainModel]] = {
    ConfigKind.BASKET: Basket,
    ConfigKind.GLOBAL_RISK: GlobalRiskPolicy,
    ConfigKind.MAINTENANCE: MaintenancePolicy,
}


@dataclass(frozen=True, slots=True)
class ConfigRecord(Generic[T]):
    """One stored version: what it is, which version, who published it, and when."""

    ref: ConfigRef
    document: T
    retired: bool = False
    actor: str = ""
    note: str = ""
    created_at: datetime | None = None

    @property
    def usable(self) -> bool:
        """Whether this version may be run. A retired document resolves but never starts."""
        return not self.retired


class ConfigStore:
    """Reads and publishes versioned configuration through the database's single writer."""

    def __init__(
        self, engine: Engine, writer: SingleWriter, store: EventStore, clock: Clock
    ) -> None:
        self._engine = engine
        self._writer = writer
        self._store = store
        self._clock = clock
        #: Guards a caller's own read-check-write around a publish. `SingleWriter` already
        #: serializes `put` itself, but a caller that reads configuration, decides something
        #: from it, and only then calls `put` needs that whole sequence to be one unit — otherwise
        #: two concurrent callers can each read the same pre-write state, each conclude their
        #: check passed, and both write. General-purpose and private to this class: what any
        #: particular caller checks is none of `ConfigStore`'s business — see `publishing`.
        self._publish_lock = asyncio.Lock()

    @asynccontextmanager
    async def publishing(self) -> AsyncIterator[None]:
        """Hold the publication lock for a read-check-write around one or more writes.

        One asyncio process (DESIGN §5), so an `asyncio.Lock` is enough — no cross-process
        concern. Only a caller that reads state, decides something from it, and would be wrong if
        that state changed before its write lands needs this; a bare `put()` or `retire()` is
        already atomic on its own.
        """
        async with self._publish_lock:
            yield

    # ------------------------------------------------------------------ writes

    async def put(
        self, config_id: str, document: DomainModel, *, actor: str, note: str = ""
    ) -> ConfigRecord[Any]:
        """Publish a new version of `config_id`. Never overwrites; always increments."""
        kind = _kind_of(document)
        return await self._write(
            kind, _resolve_id(kind, config_id), document, actor=actor, note=note, retired=False
        )

    async def retire(
        self, kind: ConfigKind, config_id: str, *, actor: str, reason: str = ""
    ) -> ConfigRecord[Any]:
        """Withdraw a configuration from service, keeping every version resolvable.

        The retiring version carries the last document rather than a tombstone with no content, so
        an operator reading the history sees *what* was retired without walking back a version.
        """
        resolved = _resolve_id(kind, config_id)
        current = self.latest(kind, resolved)
        if current is None:
            raise ConfigError(f"cannot retire {kind.value}:{resolved}: it has no versions")
        return await self._write(
            kind, resolved, current.document, actor=actor, note=reason, retired=True
        )

    async def _write(
        self,
        kind: ConfigKind,
        config_id: str,
        document: DomainModel,
        *,
        actor: str,
        note: str,
        retired: bool,
    ) -> ConfigRecord[Any]:
        payload = _assert_no_secrets(kind, config_id, document)
        created_at = self._clock.now()

        def work(connection: Connection) -> ConfigRecord[Any]:
            # The version is allocated *inside* the transaction that uses it. Reading the maximum
            # first and inserting afterwards would leave a window in which two publishers pick the
            # same number, and the loser's document would be the one that never happened.
            ref = ConfigRef(
                kind=kind, config_id=config_id, version=_next_version(connection, kind, config_id)
            )
            connection.execute(
                config_versions.insert().values(
                    kind=ref.kind.value,
                    config_id=ref.config_id,
                    version=ref.version,
                    document_json=payload,
                    retired=int(retired),
                    actor=actor,
                    note=note,
                    created_at=created_at,
                )
            )
            self._store.append_within(
                connection,
                EventFactory(
                    clock=self._clock, basket_id=_basket_id(ref), cycle_id="config"
                ).config_changed(ref, actor=actor, note=note, retired=retired),
            )
            return ConfigRecord(
                ref=ref,
                document=document,
                retired=retired,
                actor=actor,
                note=note,
                created_at=created_at,
            )

        record = await self._writer.run(work)
        logger.info(
            "configuration published",
            extra={
                "config": record.ref.key,
                "version": record.ref.version,
                "actor": actor,
                "retired": retired,
            },
        )
        return record

    # ------------------------------------------------------------------ reads

    def latest(self, kind: ConfigKind, config_id: str) -> ConfigRecord[Any] | None:
        """The newest version of one document, retired or not. `None` if it never existed."""
        return self._one(
            select(config_versions)
            .where(
                config_versions.c.kind == kind.value,
                config_versions.c.config_id == _resolve_id(kind, config_id),
            )
            .order_by(config_versions.c.version.desc())
            .limit(1)
        )

    def at(self, ref: ConfigRef) -> ConfigRecord[Any]:
        """Resolve an exact pinned version. Raises if the log points at something unreadable."""
        record = self._one(
            select(config_versions).where(
                config_versions.c.kind == ref.kind.value,
                config_versions.c.config_id == ref.config_id,
                config_versions.c.version == ref.version,
            )
        )
        if record is None:
            raise ConfigError(f"no stored configuration for {ref.key} version {ref.version}")
        return record

    def current(self, kind: ConfigKind) -> tuple[ConfigRecord[Any], ...]:
        """The newest, non-retired version of every document of one kind, by id."""
        newest = (
            select(
                config_versions.c.config_id,
                func.max(config_versions.c.version).label("version"),
            )
            .where(config_versions.c.kind == kind.value)
            .group_by(config_versions.c.config_id)
            .subquery()
        )
        return self._many(
            select(config_versions)
            .join(
                newest,
                (config_versions.c.config_id == newest.c.config_id)
                & (config_versions.c.version == newest.c.version),
            )
            .where(config_versions.c.kind == kind.value, config_versions.c.retired == 0)
            .order_by(config_versions.c.config_id)
        )

    def history(self, kind: ConfigKind, config_id: str) -> tuple[ConfigRecord[Any], ...]:
        """Every version of one document, oldest first — what the dashboard's audit view reads."""
        return self._many(
            select(config_versions)
            .where(
                config_versions.c.kind == kind.value,
                config_versions.c.config_id == _resolve_id(kind, config_id),
            )
            .order_by(config_versions.c.version)
        )

    def baskets(self) -> tuple[ConfigRecord[Basket], ...]:
        """Every basket in service. The set the supervisor runs."""
        return tuple(self.current(ConfigKind.BASKET))

    def global_risk(self) -> ConfigRecord[GlobalRiskPolicy] | None:
        """The Tier-2 policy in force, or `None` before one has been published."""
        record = self.latest(ConfigKind.GLOBAL_RISK, SINGLETON_ID)
        return record if record is not None and record.usable else None

    # ------------------------------------------------------------------ internals

    def _one(self, query: Any) -> ConfigRecord[Any] | None:
        with self._engine.connect() as connection:
            row = connection.execute(query).one_or_none()
        return None if row is None else _record(row)

    def _many(self, query: Any) -> tuple[ConfigRecord[Any], ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(query).all()
        return tuple(_record(row) for row in rows)


def _next_version(connection: Connection, kind: ConfigKind, config_id: str) -> int:
    """One past the highest version this document has ever had. Retired versions count."""
    highest = connection.execute(
        select(func.max(config_versions.c.version)).where(
            config_versions.c.kind == kind.value, config_versions.c.config_id == config_id
        )
    ).scalar_one_or_none()
    return 1 if highest is None else int(highest) + 1


def _record(row: Any) -> ConfigRecord[Any]:
    kind = ConfigKind(row.kind)
    return ConfigRecord(
        ref=ConfigRef(kind=kind, config_id=row.config_id, version=row.version),
        document=_parse(kind, row.config_id, row.version, row.document_json),
        retired=bool(row.retired),
        actor=row.actor or "",
        note=row.note or "",
        created_at=row.created_at,
    )


def _parse(kind: ConfigKind, config_id: str, version: int, document_json: str) -> DomainModel:
    """Validate a stored document through the same model the engine consumes.

    A stored document that no longer parses is a fatal condition, not a recoverable one: the
    alternatives are running on a default nobody chose, or running on a partially applied
    document. Both trade under limits that are not the ones on the row.
    """
    try:
        return DOCUMENTS[kind].model_validate_json(document_json)
    except ValueError as exc:
        raise ConfigError(
            f"stored configuration {kind.value}:{config_id} version {version} does not validate "
            f"against {DOCUMENTS[kind].__name__}: {exc}"
        ) from exc


def _kind_of(document: DomainModel) -> ConfigKind:
    for kind, model in DOCUMENTS.items():
        if type(document) is model:
            return kind
    raise ConfigError(
        f"{type(document).__name__} is not a stored configuration kind; "
        f"storable kinds: {', '.join(sorted(k.value for k in DOCUMENTS))}"
    )


def _resolve_id(kind: ConfigKind, config_id: str) -> str:
    """A singleton kind has exactly one address, whatever the caller passed."""
    return SINGLETON_ID if kind.is_singleton else config_id


def _basket_id(ref: ConfigRef) -> str:
    """Which basket a config event correlates to, so the log can be filtered by basket."""
    return ref.config_id if ref.kind is ConfigKind.BASKET else "global"


def _assert_no_secrets(kind: ConfigKind, config_id: str, document: DomainModel) -> str:
    """Serialize the document, refusing one that any secret can be found inside.

    `scrub` knows both the values registered from the environment at startup and the *shapes* of
    known key formats, so this catches a key pasted into a form field as well as one that leaked
    through a model. Providers reference secrets by env-var name; a document carrying a value
    would put a live key in the database, in a backup, and in every export of it.
    """
    payload = canonical_json(document)
    if SECRETS.scrub(payload) != payload:
        raise ConfigError(
            f"refusing to store {kind.value}:{config_id}: the document contains something that "
            "looks like a secret. Configuration references secrets by environment-variable name "
            "(`secret_ref`), never by value (PLAN §3.2)."
        )
    return payload
