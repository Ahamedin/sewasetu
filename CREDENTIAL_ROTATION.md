# Credential rotation checklist

This checklist is procedural only. No production credentials were rotated by
this change.

## Database credential

- Consumed by PostgreSQL and by the app and scheduler through
  `SEWASETU_DB_PASSWORD`.
- The Compose variable is not the PostgreSQL role password after the database
  has initialized. The existing `pgdata` volume retains the role password;
  changing only `.env` makes new containers use a value that PostgreSQL will
  reject.
- In an approved maintenance window, take and verify a backup, generate a
  replacement in a protected secret manager, and connect to the database
  through an approved administrative channel. Change only the `sewasetu`
  role password there, without placing the value in SQL history, logs, or chat.
- Stage the same replacement in the app secret store and scheduler secret
  store. Validate the app with the scheduler profile still disabled, then
  restart only the app during the window and perform a read-only health check
  and normal authentication check.
- Keep the scheduler stopped until the app and database connection are
  verified. If the scheduler is later authorized, update its environment from
  the same secret version before a separately approved start.
- Roll back by restoring the previous protected secret and role password only
  if the old value remains valid. Otherwise use the approved database
  break-glass procedure; never recover credentials from logs or source.

## Flask session secret

- Consumed by Flask as `SEWASETU_FLASK_SECRET_KEY`.
- Generate a long random value in protected storage.
- Stage it for the app and restart during an approved window.
- Existing Flask sessions will be invalidated when the value changes. Verify
  login and logout behavior after the switch.
- Rollback requires the prior protected value and will restore the prior
  session-signing state; do not disclose it.

## Administrator credential

- The username is `SEWASETU_ADMIN_USERNAME`; the password is represented only
  by `SEWASETU_ADMIN_PASSWORD_HASH`.
- Generate a strong administrator password locally, derive a Werkzeug-compatible
  password hash without printing either value, and store only the hash in
  protected configuration.
- Stage the hash, verify administrator login in a controlled test window, and
  retain a separately controlled break-glass procedure.
- Existing administrator access is not changed until the staged hash is
  explicitly approved and the app is restarted.

## Citizen password migration

- New accounts now receive a random temporary password through the existing
  post-commit SMS flow, rather than using date of birth.
- Existing SHA-256 hashes remain login-compatible only for migration. After a
  successful login, the password is immediately replaced with a Werkzeug
  adaptive hash in the same database transaction.
- Failed logins do not upgrade or modify hashes. Accounts that never log in
  require a separately approved reset campaign; do not bulk-change accounts.
- Password reset now requires a six-digit code sent by SMS, a five-minute
  expiry, and a new password of at least 12 characters. The code is deleted
  in the same transaction as the password update, making reuse fail.
- The existing PostgreSQL `pgdata` volume is persistent: changing
  `SEWASETU_DB_PASSWORD` in Compose does not change the password of an already
  initialized PostgreSQL role. A controlled rotation must first create a
  verified backup, change the role password in a controlled database session,
  stage the same new value for the app and scheduler, verify app connectivity
  while keeping the scheduler stopped, and only then approve a coordinated
  scheduler cutover.
- Reset-code throttles are process-local because this schema-preserving change
  cannot add a shared rate-limit store. A multi-worker deployment needs Redis,
  a gateway limiter, or an approved schema migration before claiming
  cluster-wide abuse prevention.
- Reset-code digests are compact HMAC-derived values because the existing
  `otps.code VARCHAR(6)` column cannot hold a full password-hash record.
  Before production rollout, approve a schema migration to a dedicated reset
  challenge table with a full-length hash, attempt counter, expiry, and
  consumption timestamp.

## Other sensitive values

- Review OTP, gateway, backup, and deployment-provider credentials separately.
- Rotate any provider credential that was exposed in source or history.
- Removing a value from current files does not make historical exposure safe.

## Reset-code pepper

- `SEWASETU_RESET_CODE_PEPPER` is consumed only by the app when deriving
  reset-code digests. Generate a new random value in protected storage and
  stage it with the app during a planned reset-token cutover.
- Changing the pepper invalidates outstanding reset codes. Announce the
  controlled effect to support staff, verify new reset requests and SMS
  delivery in a non-production environment, and keep the previous value
  available only for rollback until all old challenges have expired.
- Never log, print, or place the pepper in Compose files, source, or test
  fixtures.

## Shared reset limiter

- The app requires `SEWASETU_REDIS_URL` and uses the internal Redis service for
  atomic mobile/IP request limits, cooldowns, and verification-attempt limits.
- Provision the Redis URL through protected runtime configuration. Do not
  expose Redis publicly or reuse a production credential in tests.
- Verify Redis reachability and limiter behavior with an isolated test Redis
  or mocked client before an approved rollout. A Redis outage must fail the
  reset operation safely rather than silently reverting to process-local
  limits.

## Safe generation examples

Use a protected local shell or secret manager. Redirect output directly to
protected storage; never print generated values:

```powershell
py -3 -c "import secrets; open('.sewasetu-flask-secret','w').write(secrets.token_urlsafe(48))"
py -3 -c "from werkzeug.security import generate_password_hash; open('.sewasetu-admin-hash','w').write(generate_password_hash(input('Admin password: ')))"
```

Delete temporary files only after securely transferring their contents to the
approved secret store. Do not commit them.
