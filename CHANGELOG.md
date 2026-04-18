# Changelog

## v0.19.3 - 2026-04-18

### Fixed

- Parse X `poll_choice_images` card names as `PollCard` instead of logging them
  as unknown card types.
- Treat known transient X GraphQL errors (`ServiceUnavailable`, `Internal server
  error`, and `Timeout: Unspecified`) as typed `ServiceUnavailableError`
  failures without cooling healthy accounts as unknown API failures.
