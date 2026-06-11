# Changelog

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
