"""Crawling a metered publisher as the operator's own logged-in subscriber (spec §6.3.1).

CoinDesk serves the article body to the first three requests of a session and withholds it from
every one after — measured on 2026-09-16, with `rw_remaining` counting 2→1→0 and `rw_allowed`
flipping as the text disappears. A 5 687-article archive therefore holds nine bodies.

The way *not* to solve that is to look like more than one visitor. Rotating identities, cycling
clients or spoofing a browser is paywall circumvention, and it is also what gets a subscription
banned. The way this module solves it is to present one real subscription, from one honest client:

* **A session cookie, never a password.** We never hold the credential that could change the
  account, and — load-bearing — we never request `/auth/`, which CoinDesk's `robots.txt`
  disallows. A crawl that logged itself in would have to disregard the file `FeedFetcher` exists
  to honour, on its very first act.
* **The `User-Agent` is unchanged.** `AuthenticatedFetcher` adds a cookie and nothing else, so we
  remain identifiable to the publisher as the research client we are.
* **The cookie is environment-only and redactor-registered**, like every other credential in this
  system: never a CLI argument (shells keep history), never a file, never the database.

Failure semantics: an absent, blank or unparseable cookie raises `ConfigError` *before* any
request. Falling back to an anonymous client would spend thousands of requests to gain three
bodies and then report that the subscription does not lift the meter — a wrong answer arrived at
expensively, which is exactly what failing closed is for.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Final

import httpx

from tradebot.core.clock import Clock
from tradebot.core.errors import ConfigError
from tradebot.core.logging import SECRETS
from tradebot.news.http import DEFAULT_TIMEOUT, FeedFetcher

#: Where the session cookie comes from. Only CoinDesk meters the body, so there is one variable
#: rather than a per-source scheme; a second metered publisher is the time to generalise.
COOKIE_VARIABLE: Final = "DECISION_LAB_COINDESK_COOKIE"


def cookies_in(header: str) -> dict[str, str]:
    """A pasted `Cookie:` header to the pairs it names.

    Split on the **first** `=` per pair, never on every one: session tokens are base64 and
    routinely carry `=` padding, and a greedy split silently truncates the credential into
    something the publisher reads as an expired session.
    """
    pairs = {}
    for part in header.split(";"):
        candidate = part.strip()
        if not candidate or "=" not in candidate:
            continue
        name, _, value = candidate.partition("=")
        if name.strip():
            pairs[name.strip()] = value.strip()
    if not pairs:
        raise ConfigError(
            f"the cookie carries no name=value pair. Copy the whole `Cookie:` request header "
            f"from a browser already signed in to the publisher, and set it as "
            f"{COOKIE_VARIABLE}."
        )
    return pairs


def cookie_from_environment(environ: Mapping[str, str] | None = None) -> str:
    """The session cookie, refused by name when it is not there.

    Registered with the redactor on the way out, so no log line, report or exception rendered
    afterwards can carry the session — the rule ADR 0019 sets for alert destinations, applied to
    the one credential this package holds.
    """
    source = os.environ if environ is None else environ
    cookie = (source.get(COOKIE_VARIABLE) or "").strip()
    if not cookie:
        raise ConfigError(
            f"{COOKIE_VARIABLE} is not set, and an authenticated pass will not quietly fall back "
            f"to an anonymous one — that would spend thousands of requests to collect three "
            f"bodies. Sign in to the publisher in a browser, copy the whole `Cookie:` request "
            f"header from its developer tools, and set it as {COOKIE_VARIABLE}."
        )
    SECRETS.register(cookie)
    return cookie


class AuthenticatedFetcher(FeedFetcher):
    """`FeedFetcher` carrying a session cookie, and differing from it in nothing else.

    A subclass rather than a parameter on `build_fetcher`, because nothing under `tradebot/` may
    be changed for this tool's benefit: the archive is `decision_lab`'s, and
    `git diff --stat main -- tradebot/` staying empty is a slice exit criterion.
    """

    def __init__(self, client: httpx.AsyncClient, clock: Clock, *, cookie: str) -> None:
        super().__init__(client, clock)
        self._cookies = cookies_in(cookie)
        client.cookies.update(self._cookies)

    @property
    def cookie_names(self) -> tuple[str, ...]:
        """The names only. The values are the credential and are never exposed for logging."""
        return tuple(sorted(self._cookies))

    @property
    def user_agent(self) -> str:
        return self._user_agent


def authenticated_fetcher(clock: Clock, *, cookie: str) -> AuthenticatedFetcher:
    """A fetcher on a real HTTP client that presents `cookie`. The composition root only."""
    client = httpx.AsyncClient(timeout=DEFAULT_TIMEOUT, http2=False)
    return AuthenticatedFetcher(client, clock, cookie=cookie)
