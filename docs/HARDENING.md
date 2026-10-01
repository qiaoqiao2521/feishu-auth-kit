# Compatibility and state hardening

Public Python and TypeScript authentication method signatures and the token JSON
format remain compatible. Tenant tokens use bounded monotonic expiry and skew;
explicit zero/invalid TTL never becomes a default lifetime. A missing TTL uses a
conservative bounded 300-second fallback. Python refreshes are thread-serialized;
TypeScript concurrent refreshes share a Promise, cleared after success or failure.

New FileTokenStore files use 0600 and newly created storage directories use 0700.
Existing files with unsafe permissions, symlinks or malformed contents fail closed
without rewriting/chmod/deleting the original. An existing configured directory
must not be group/world writable. Legacy valid JSON record shapes remain readable.
The store is plaintext unless the host chooses its optional encrypted implementation;
private filesystem permissions do not constitute encryption.

Python and Node writers serialize the complete read-modify-write transaction with
the same `<token-filename>.lock` directory protocol. Writes use unique private
scratch files, fsync and atomic replacement, then directory fsync on Linux/POSIX.
Readers see an entire old or new file. A stopped/crashed writer can leave a lock:
waiting is bounded and fails closed; never auto-delete a lock while a writer could
still own it. Operators must fence the old writer and approve any real-state
migration/recovery separately. No code silently changes existing credential file
permissions, rebinds an identity or imports credentials from a host profile.

Transport CLI errors describe the current attempted request; historical delivery
status is returned separately as `operation_status`. A rejected changed body must
not report a new successful send. Unknown outcomes still block automatic replay.

WebSocket resources, locks and temporarily replaced SDK objects are cleaned on
credentials, construction and connection failures. Only tasks created by that
listener are cancelled. The listener still belongs in a dedicated worker process;
SDK lifecycle internals remain pinned and tested rather than assumed stable.

Product CI installs dependencies normally, then uses synthetic fixtures with
network blocked for product tests. Matrix: Python 3.10/3.13, Node 22/24 on Linux.
It checks both languages, the optional transport, typecheck/build, and base-auth
installation without transport. Scoped lint covers source/product tests. The 30
pre-existing upstream-watcher script/test lint findings remain outside this gate;
this is an explicit baseline exclusion, not a declaration that full lint is clean.

See [auditable source inventory](UPSTREAM-SOURCES.json). Unknown adoption SHAs
remain null. The recorded inspection commit is not an adoption baseline or an
assertion of package-version equivalence. Future upstream upgrades must identify
the intended source revision and pass contracts before release/deployment.
