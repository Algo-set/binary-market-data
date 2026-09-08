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
