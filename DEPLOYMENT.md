# VM/VPS deployment guide

This guide prepares the Compose stack for an operator-controlled VM or VPS.
It does not rotate credentials, start services, run migrations, or activate the
deemed-approval scheduler.

## Required host controls

Before deployment, the operator must provide:

- A supported Docker Engine and Compose installation.
- An encrypted, access-controlled location for the required environment values.
- A reverse proxy terminating TLS on the public interface.
- Host firewall rules allowing only the reverse proxy's public ports.
- Protected persistent storage for `pgdata`, `uploads`, and `applogs`.
- A monitored backup destination separate from the database volume.

Only the app port should be reachable from the reverse proxy. PostgreSQL,
Redis, and the simulated SMS gateway have no host port mappings in Compose.

## Environment and startup

Copy `.env.example` to a protected untracked environment file and replace every
placeholder through the approved secret-management process. Do not place secret
values in this repository, image layers, logs, or shell history.

Validate the rendered configuration without starting services:

```powershell
$env:SEWASETU_DB_PASSWORD = "placeholder"
$env:SEWASETU_FLASK_SECRET_KEY = "placeholder"
$env:SEWASETU_ADMIN_USERNAME = "placeholder"
$env:SEWASETU_ADMIN_PASSWORD_HASH = "placeholder"
$env:SEWASETU_RESET_CODE_PEPPER = "placeholder"
$env:SEWASETU_REDIS_URL = "redis://redis:6379/0"
docker compose config --quiet
```

The app starts with Gunicorn from the Dockerfile. The scheduler is behind the
explicit `scheduler` profile and must remain stopped until statutory policy,
incident review, image freshness, and activation approval are complete.

## Health and readiness checks

Compose healthchecks are intentionally local and secret-free:

- PostgreSQL uses `pg_isready`; it does not expose query results.
- Redis uses `redis-cli ping`; it does not expose credentials.
- The SMS gateway checks its local `/` page and sends no message.
- The app checks its local `/` page; that route does not require a database
  query or expose credentials.

These checks establish process and dependency readiness, not legal eligibility,
successful SMS delivery, backup validity, or end-to-end user acceptance.
The operator must verify health status with `docker compose ps` after an
approved staging start.

## TLS, proxy, and cookies

TLS termination and HTTP-to-HTTPS redirection belong at the approved reverse
proxy. Do not expose Gunicorn directly as a public internet endpoint.

The application currently does not enable `ProxyFix`, trusted proxy headers,
host allowlisting, or an application-level HTTPS redirect by default. The app
now supports explicit environment configuration without trusting proxy headers
unless `SEWASETU_TRUSTED_PROXY_COUNT` is a positive value.

For production, set `SEWASETU_ALLOWED_HOSTS` to the exact comma-separated
deployment hostnames and set `SEWASETU_TRUSTED_PROXY_COUNT` to the exact number
of trusted reverse-proxy hops. The reverse proxy must overwrite forwarded
headers, and the host firewall/network boundary must prevent clients from
reaching Gunicorn directly. Never set a proxy count merely because forwarded
headers are present.

Set the production cookie values as follows:

```text
SEWASETU_SESSION_COOKIE_SECURE=true
SEWASETU_SESSION_COOKIE_HTTPONLY=true
SEWASETU_SESSION_COOKIE_SAMESITE=Lax
SEWASETU_SESSION_LIFETIME_SECONDS=1800
SEWASETU_ALLOWED_HOSTS=portal.example.gov
SEWASETU_TRUSTED_PROXY_COUNT=1
```

The hostname and proxy count above are placeholders and require replacement
with approved deployment values. `Lax` preserves the existing browser form and
CSRF workflows while preventing most cross-site cookie sending. `SameSite=None`
is rejected unless secure cookies are also enabled.

For local HTTP development, leave secure cookies disabled, use
`SEWASETU_TRUSTED_PROXY_COUNT=0`, and set the allowlist to local hostnames or
leave it empty. The application does not perform an HTTPS redirect; the
approved reverse proxy must enforce HTTP-to-HTTPS redirection.

## Storage, shutdown, and recovery

The `uploads` volume stores uploaded documents and must use restricted host or
volume access. The `applogs` volume stores application and cron logs; apply
retention and access controls that prevent sensitive-data exposure.

The app has a 45-second Compose stop grace period so Gunicorn can drain
requests. Restart policies apply to the app and dependency services, but not
to the scheduler. Do not treat automatic restarts as a substitute for
monitoring or database recovery.

Before any approved maintenance or recovery action:

1. Preserve the live volumes and scheduler-disabled state.
2. Take a PostgreSQL custom-format backup to controlled storage.
3. Independently verify its checksum and restore it only in a separate
   disposable environment.
4. Record the backup identity, operator, time, and verification result.
5. Never restore over the source database as a routine rollback.

## Release verification

Run these checks only in an approved isolated/staging environment:

1. Confirm `docker compose config --quiet` succeeds with protected secrets.
2. Confirm `docker compose ps` reports healthy app, database, Redis, and SMS
   gateway services.
3. Verify HTTPS at the reverse proxy and confirm HTTP redirects to HTTPS.
4. Verify session cookies have `Secure`, `HttpOnly`, and the approved
   `SameSite` policy.
5. Verify requests with an unapproved `Host` header are rejected and that
   forwarded headers are honored only through the approved proxy boundary.
6. Verify citizen ownership, CSRF, reset-rate-limit, and admin transition
   tests without production data or real SMS.
7. Verify document upload and retrieval through the intended authenticated
   path.
8. Confirm no host port is published for PostgreSQL, Redis, or the SMS gateway.
9. Keep the scheduler profile disabled and confirm cron does not contain
   `--apply`.

Redis fail-open versus fail-closed behavior for password reset remains an
unresolved security-policy decision and is not changed by this guide.
