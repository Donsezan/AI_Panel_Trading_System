# 14. The dashboard is vendored, always authenticated, and refuses to start without a token

Date: 2026-07-31
Status: accepted

## Context

DESIGN §6.10 gives the dashboard three jobs — Configure, Monitor, Control — and ends with a
one-line auth requirement: single-user token/password is enough, but *mandatory* whenever the
dashboard binds to non-localhost. PLAN §4 fixes the stack as FastAPI + Jinja2 + HTMX with no JS
build step, and PLAN §3.3 adds that binding to a non-loopback interface requires auth *and* an
explicit flag.

Three questions had to be answered before writing a route.

**Where does the client-side library come from?** Every other dependency in this repo is
hash-pinned in `requirements.lock` for supply-chain reasons (PLAN §4). A `<script src="https://cdn…">`
tag would be the one unpinned, unaudited, network-dependent input in the system — and it would sit
on the page that holds the kill switch.

**Is localhost trusted?** DESIGN §6.10 only demands auth off-loopback. But the dashboard can trip
the kill switch, publish a risk policy, and close a position.

**How long does a session last?** With no server-side session store, a cookie is either
self-validating or worthless.

## Decision

**htmx is vendored into the repo**, at `tradebot/dashboard/static/htmx.min.js`. No CDN, no npm, no
build step. The page works with the network unplugged, and the file changes only when a commit
changes it.

| Field | Value |
|---|---|
| Version | htmx 2.0.7 |
| Source | `https://unpkg.com/htmx.org@2.0.7/dist/htmx.min.js` |
| Size | 51 076 bytes |
| SRI | `sha384-ZBXiYtYQ6hJ2Y0ZNoYuI+Nq5MqWBr+chMrS/RkXpNzQCApHEhOt2aY8EJgqwHLkJ` |
| SHA-256 | `60231ae6ba9db3825eb15a261122d5f55921c4d53b66bf637dc18b4ee27c79f9` |

`tests/unit/test_dashboard_static.py` re-derives the SRI hash from the file on disk and fails if it
has changed, so an edited or swapped asset is a failing build rather than a silent one. The
`<script>` tag carries the same `integrity` attribute; it is inert for a same-origin file, and it
is there so the recorded hash and the served hash are the same string in two places.

**Auth is mandatory always, including on loopback** — stricter than DESIGN §6.10. Anything that can
reach localhost otherwise gets the kill switch and config CRUD for free: a browser tab on a
malicious page, another user on a shared machine, a container sharing the network namespace. The
token is read from `TRADEBOT_DASHBOARD_TOKEN` and must be at least 16 characters; **the server
refuses to start without it**, the same way live mode refuses without its preconditions (PLAN
§2.4). There is no "auth disabled" flag, because the value of a control that can be turned off is
whatever the least careful invocation of it is worth.

**Enforcement is middleware, not a per-route dependency.** A dependency someone forgets to add to a
new route is an unauthenticated route; a middleware cannot be forgotten. Only `/login`,
`/logout` and `/static/*` are exempt, and a test walks every registered route to assert nothing
else is.

**Binding off-loopback needs `--allow-remote`** on top of auth, per PLAN §3.3. Refusing by default
means a `--host 0.0.0.0` typo cannot silently expose the kill switch to a LAN.

**The session cookie does not expire; logging out is what ends it.** Chosen deliberately over a
12-hour expiry: this is a single-user research tool whose operator watches a soak for weeks, and
being logged out mid-incident is its own hazard. The mitigation for the risk that buys is that
**the cookie's signing key is derived from the dashboard token** (`itsdangerous.Signer(token,
salt=…)`), so rotating `TRADEBOT_DASHBOARD_TOKEN` and restarting invalidates every live session at
once. That is the "log everyone out" lever a no-expiry session otherwise lacks. The cookie is
`HttpOnly`, `SameSite=Strict`, and `Secure` whenever the request arrived over HTTPS.

## Consequences

- The dashboard is offline-capable and its entire client surface is in the repo. Upgrading htmx is
  a commit that changes a file, a hash and this table — reviewable like any other dependency bump.
- `tradebot serve` refuses to start with no token, with a short token, or with a non-loopback host
  and no `--allow-remote`. Each refusal is a `ConfigError`, so the CLI reports it as exit code 1
  through the existing handler.
- A stolen cookie is valid until the token is rotated. Accepted, and the reason token rotation is
  documented as the revocation procedure rather than left implicit.
- Token comparison uses `hmac.compare_digest`, and a failed login is logged without the submitted
  value — a near-miss token in a log file is a token in a log file.
