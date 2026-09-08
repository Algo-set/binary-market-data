# Contributing

Contributions should preserve the packages' narrow, read-only security
boundaries and keep each package usable independently.

## Development setup

Install Rust 1.88 or newer, then run from the repository root:

```bash
cargo fmt --all --check
cargo test --workspace --locked
cargo clippy --workspace --locked --all-targets --all-features -- -D warnings
cargo deny check advisories licenses sources
```

Network integration tests or probes must remain opt-in and must not require
private credentials. Unit tests should use synthetic identifiers and arbitrary
values rather than real accounts, endpoints, or captured production payloads.

## Pull requests

- Keep changes focused and document user-visible behavior.
- Add tests for parsing, validation, and failure behavior.
- Do not add signing, order placement, cancellation, transaction construction,
  approval, or broadcast capabilities.
- Do not commit generated state, build output, credentials, personal paths,
  account identifiers, or private endpoints.
- Explain new dependencies and prefer narrowly configured feature sets.

By contributing, you agree that your contribution is licensed under the MIT
License included in this repository.
