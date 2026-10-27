# Memory v1.4: complete text, paged recall and portable snapshots

New text observations retain the complete sanitized message. Prompt budgets remain
separate from storage; no embedding service is introduced. Friend archive text
segments no longer have the former 20,000-character cutoff. Previously lost text
cannot be reconstructed by this update; archived media and recorded history still
have their existing availability limits.

Recall search now returns a snippet near a lexical match plus a character `offset`.
`companion_memory_read` accepts `offset`, optional `query` and `context` (0 or 1),
and returns `next_offset`, `has_more` and `total_chars`. Follow `next_offset` to
continue reading the stored sanitized text. Each call rechecks sender identity,
friend status, sleep and suppression rules. Optional neighbor excerpts never cross
contacts. The limits remain four recall/read calls per event, 1,800 UTF-8 bytes per
reply and 4,200 bytes combined; a new event may be needed to continue a long text.

Date bounds apply before the FTS candidate limit. Entries use the time of their
source evidence, falling back to their update time when no evidence time exists.
The query candidate count and model-call quotas are unchanged.

The seven daily SQLite snapshots now refresh at most hourly while the background
worker is awake. They use the online backup API, integrity checks, closed connections
and atomic replacement. Sleeping does not guarantee an hourly snapshot.

## Portable private backup

The explicit owner command `/备份记忆` creates a verified archive in the memory
plugin's private `portable-backups` folder. It respects sleep gating and only reports
the private server path; it does not send the archive or make it public. No backup
operation is exposed as a model tool. Keep copies outside the server as well.

The dependency-free `portable.py` also supports offline maintenance:

```text
python portable.py backup --data-root /path/to/astrbot/data --output /private/backups
python portable.py verify /private/backups/memory-example.zip
python portable.py restore /private/backups/memory-example.zip --destination /new/recovery-folder
```

Included data: the complete memory database (evidence, revisions, suppression,
indexes and cursors), shared profile, selected native persona and conversation
tables, recorded friend chats, sleep inbox, approvals and cached media, diaries,
experiences, activity, rhythm and quota state. Registered native attachments are
included only from supported folders within the data root. Unavailable/external
native attachments are counted in the manifest.

Provider configuration, native authentication/configuration tables, QQ login, ADB
pairing, dashboard credentials and old backups are excluded. This is a private
memory recovery package, not a ready-to-run authenticated server image.

Each database is a consistent online snapshot. Other files are checked for
concurrent modification; the entire archive is not one global transaction. The
manifest records its time range and SHA-256 values. Restore validates the complete
file set, safe paths, checksums and SQLite integrity, writes only a previously
nonexistent directory, and starts no service. Review the restored `data/` and
`RESTORE.json` before arranging any application-specific import.
