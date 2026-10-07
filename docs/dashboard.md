# Web dashboard

A read-only page at `https://<app-fqdn>/dashboard` that lists projects, their
status and the open / in-progress / blocked tasks ("issues"), plus the most
recent completed ones. It auto-refreshes every 60 seconds and works on a phone.

## What it shows (and does not)

- Project name, slug, status, purpose and task counts; task title, status,
  priority, claimant, blocker text and update time.
- **Never** shown: memory, decision, message, session or task bodies.
- Projects whose slug starts with a hidden prefix (default `personal-`,
  `COLLAB_DASHBOARD_HIDDEN_PREFIXES`) are filtered on the server and never
  rendered.
- Archived projects are collapsed under "Archived".

## Security

- Sign-in uses the same Entra login app as the connector OAuth flow (PKCE,
  managed-identity client assertion, no client secret). Only identities in
  `COLLAB_PRINCIPALS` get a session; the check is repeated on every request.
- The login is bound to the browser by a short-lived cookie (login CSRF
  protection); the one-time state row is consumed on first use.
- Session: random 256-bit token, stored only as a SHA-256 hash in the table,
  `__Host-` cookie (`Secure`, `HttpOnly`, `SameSite=Lax`), 8 h by default
  (`COLLAB_DASHBOARD_SESSION_HOURS`). Sign out deletes the row.
- Server-rendered HTML, no JavaScript; strict CSP (`default-src 'none'`),
  `frame-ancestors 'none'`, `no-store`, `noindex`. All values are HTML-escaped.
- No write endpoint exists except sign-out.

## Enable

Terraform variable `enable_dashboard` (default `true`, requires `enable_oauth`)
adds `https://<app-fqdn>/dashboard/callback` to the login app and sets
`COLLAB_DASHBOARD_ENABLED`. Then deploy the new image (`docs/runbook.md` §7).
Disable by setting `enable_dashboard = false` and applying.
