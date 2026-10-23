# Deemed-approval scheduler

The scheduler is read-only unless it is invoked with the explicit `--apply`
flag. The installed cron entry never supplies that flag. The Compose scheduler
service is also behind the `scheduler` profile, so the normal application stack
does not start it.

## Policy assumption requiring confirmation

The current implementation uses `submitted_at` plus the configured
`pension.sla_days` value as a preliminary operational cutoff. This is not a
finding that the statutory deadline is calculated from `submitted_at`, nor does
it establish eligibility for a pension or deemed approval. The department must
confirm the legal clock, exclusions, holidays, completeness requirements,
competent authority, and effective date before authorizing any `--apply` run.

## Read-only incident recovery plan

1. Preserve the current database instance. Take and independently verify a
   PostgreSQL custom-format backup before any correction. Store the backup and
   its checksum in controlled, access-logged storage; do not restore over the
   source database.
2. In a separate read-only session, identify the incident batch using the
   reported decision timestamp and `decided_by = 'RTPS-AUTO'`. Compare those
   rows with earlier automated decisions using `decided_at`, `decided_by`,
   `submitted_at`, and the primary key. Export only restricted identifiers to
   the review workspace.
3. For every candidate, have the authorized case-review team validate the
   legally applicable deadline, application completeness, age and scheme
   criteria, intervening manual decisions, and any hold or appeal. Do not infer
   validity from the preliminary cutoff alone.
4. If a correction is authorized, record the decision, authority, reason,
   before/after values, timestamp, and operator in an append-only audit system
   before making a narrowly scoped correction. This repository has no existing
   application audit table, so adding one requires a separately approved
   migration and retention/access policy.
5. Check application logs and the SMS gateway's delivery records for the same
   batch. Reconcile attempted, accepted, failed, and unknown notifications
   without sending test messages. Treat an accepted gateway request as
   evidence of submission, not proof of handset delivery.

No recovery query or correction is part of the scheduler change.
