# Changelog

## v0.19.8 - 2026-07-29

### Fixed

- Update X GraphQL operation IDs to the current upstream v0.19.2 values.
- Support current `x-web`/Vite signing bundles and classify transaction-ID
  account and parser failures separately for safe account rotation.
- Require complete `auth_token` and `ct0` cookie sessions before activating an
  account.
- Avoid emitting nested retweet originals as duplicate top-level tweets while
  preserving originals that have explicit timeline entries.

### Maintenance

- Keep CI on the Ruff 0.15 rule defaults until the expanded Ruff 0.16 rules are
  adopted explicitly.

## v0.19.7 - 2026-07-19

### Fixed

- Treat X `DeadlineExceeded` responses as transient backend failures instead of
  cooling healthy accounts for 15 minutes.

## v0.19.6 - 2026-06-11

### Fixed

- Retry transient X API failures without cooling healthy accounts as unknown
  errors.
- Use account metadata and verified X assets when generating client transaction
  IDs.

## v0.19.5 - 2026-05-28

### Fixed

- Reconcile the fork with upstream v0.18.1 X compatibility fixes for current
  GraphQL payload shapes, including `legacy = null` tweets/users and moved user
  fields.
- Treat `Dependency: Unspecified` as a transient X backend failure.
- Continue SearchTimeline pagination past bounded empty pages while preserving
  repeated cursor/page stall protection.
- Harden current-shape user/tweet flattening against malformed nested fields.

## v0.19.4 - 2026-05-20

### Fixed

- Resolve X client transaction ID script URLs from numeric chunk name/hash maps
  when `ondemand.s` is no longer exposed as a direct mapping.

## v0.19.3 - 2026-04-18

### Fixed

- Parse X `poll_choice_images` card names as `PollCard` instead of logging them
  as unknown card types.
- Treat known transient X GraphQL errors (`ServiceUnavailable`, `Internal server
  error`, and `Timeout: Unspecified`) as typed `ServiceUnavailableError`
  failures without cooling healthy accounts as unknown API failures.
