# Authoritative DB specification

`specs.rs` is the exact specification used by the DB + Verus experiment. It defines the abstract
database state, Unicode key ordering, and the `impl_db!` contracts for `new`, `get`, `put`, `scan`,
and `sort`, including their atomicity obligations.

Imported byte-for-byte from the user-provided `~/db-specs/specs.rs`; no contracts were translated
or changed. Its SHA-256 at import is:

```text
f69a614b8a67dedfcd0e804961c5ee44949c37071b5dbb66263c7561028c72df
```

`run.py prepare` uses this file by default, copies it into the run's `specification/specs.rs`, and
records its hash in the experiment manifest and provenance. The run's copy is frozen. An explicit
`--spec` can select a different compatible contract for a separately prepared experiment.
