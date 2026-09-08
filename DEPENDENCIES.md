# Dependency policy

Dependencies must have a clear purpose, a compatible open-source license, and
an actively maintained upstream. Default features should be disabled when they
pull in unused protocol, native TLS, signing, storage, or execution surfaces.

The workspace commits lockfiles for reproducible applications and CI. Review
dependency updates before merging, including transitive changes and MSRV
impact. Git dependencies and unrecognized registries are denied. Duplicate
versions are warnings because network stacks can temporarily require them.

Automated checks use `cargo deny` for advisories, licenses, and sources. The
license allowlist is intentionally explicit in `deny.toml`; additions require
review rather than silently broadening policy.

## Python implementation

Python has two runtime dependencies: `httpx` for public HTTP GET requests and read-only JSON-RPC POSTs and
`websockets` for public subscriptions and heartbeats. Book arithmetic, parsing,
records and the CLI use the standard library. No venue SDK is needed.

`python/requirements-dev.txt` pins runtime, test, lint and build dependencies for
CI. `python/pyproject.toml` declares compatible runtime ranges for library users.
Review both files together when changing dependencies, including Python 3.11
compatibility and transitive licenses. HTTPX's CA bundle dependency, `certifi`,
uses MPL-2.0; retain its notices when redistributing dependencies. Other runtime
dependencies use MIT, BSD-3-Clause or PSF-2.0 licenses. Built wheels do not vendor them.

Dependabot checks the Python directory weekly. Python CI tests both pinned and
minimum supported transport versions and builds the distributable package.

The Rust five-minute adapters use `chrono` (without clock/timezone database
features) to parse explicit UTC contract windows and `num-bigint` to preserve
256-bit public pool amounts and signed oracle integers. Both use compatible
MIT/Apache-2.0 licensing. `serde_json` arbitrary precision prevents numeric
Limitless prices from passing through binary floating point.
