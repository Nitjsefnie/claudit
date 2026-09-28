# Privacy: exporting and erasing one person's data

This is an operator runbook for a self-hosted claudit deployment. It
documents the mechanism — which stores hold a person's data, how to
export it, how to erase it — as procedure, not legal advice. Whether an
erasure or access request legally applies to you depends on your
jurisdiction and role; ask a specialist. There is no self-service UI:
every step below is an operator action against the bucket, the
databases or the service.

## How a person appears in the data

Nothing in the schema names a person. Two identifiers matter:

- **Account data** is keyed by the numeric `user_id` of the external
  auth DB's `users` table (the id a person signs in with).
- **Transcript data** is keyed by project and session, never by user:
  `projects.project_id` is the slug of the project's directory path
  (every non-alphanumeric character replaced by `-`, so a POSIX slug
  starts with `-`) and `files.session_id` is the session id from the
  object key. Directory paths commonly embed a username
  (`-home-subject-example-app`), which is how you find one person's
  projects.

The link between an account and its transcripts is an operational fact
(the operator knows whose machine wrote which sessions), not something
the database stores. Establish it first; every step below depends on
it.

## Inventory: where a person's data can live

| Store | What it holds | Erase route |
|---|---|---|
| R2 bucket objects | The transcripts themselves — the only store holding full message text | Operator deletes the objects (no delete API in the app) |
| `projects`, `files`, `records`, `tool_uses` (app DB) | Parsed rows derived from those transcripts; cascade from `projects` → `files` → `records`/`tool_uses` | Ingest orphan sweep, after the objects are gone |
| `lane_markers` (app DB) | The directory path each lane project marker named | Dropped when the marker object leaves the bucket and an ingest runs |
| Derived rollups (`usage_rollup`, `tool_rollup`, `tool_error_rollup`, `dispatch_rollup`, `dispatch_brief_rollup`, `ctx_cost_rollup`, `agent_rollup`, `latency_rollup`) | Pre-summed aggregates rebuilt from `records`/`files`/`tool_uses` at every ingest | Automatic: rebuilt from what remains after the sweep |
| `ingest_runs`, `ingest_derived_state` (app DB) | Run metadata — trigger, counts, a count-only error summary. No transcript content | Nothing to erase |
| `user_session` (app DB) | Per-user session secret, generation counter, credential fingerprint | Operator `DELETE` (step 4 below) |
| External auth DB `users` | `user_id` + `config` JSONB holding the PBKDF2 `web_password_hash` and `web_password_salt`. Read-only to claudit | Deleted by your user-management process, outside this app |
| In-process caches | Raw transcript bytes (LRU keyed by etag), aggregate API responses, short-lived login-rate-limit and auth-config entries | Orphan sweep (transcript LRU), ingest invalidation, process restart, and 60 s / 5 min expiry |
| Server and proxy logs, backups | Request lines (journald for the service, nginx access logs, Cloudflare edge), DB and bucket backups | Operator policy — outside this repo |

The databases hold statistics and metadata, not message text — with
three deliberate exceptions in `tool_uses` and `files`: `error_text`
(the leading 200 characters of a failed tool result), `read_targets` /
`write_targets` (file paths a call named), and the project slug /
marker `path` (directory paths). These are content fragments; they
cascade away with the `files` row.

## Exporting one person's data

### Find their projects and sessions

```sql
-- Candidate projects: the slug embeds the directory path.
SELECT project_id, display_name, first_seen_at, last_seen_at
FROM projects WHERE project_id LIKE '%subject.example%';

-- Their files, with the bucket-qualified keys and session ids.
SELECT file_key, session_id, is_main, r2_etag, r2_last_modified
FROM files WHERE project_id = '<project-slug>';
```

### Export the transcripts

Two routes; the first needs no claudit access at all:

- **Straight from the bucket.** Fetch the objects under the person's
  prefixes with any S3 client (`aws s3 cp`, `rclone copy`, the
  `wrangler r2 object get` commands), using the object keys from the
  `file_key` column minus the leading `<bucket>/` segment. Objects may
  be xz-compressed (`*.jsonl.xz`); the plain JSONL is inside.
- **Through the app.** `GET /api/sessions/{session_id}/transcript`
  returns a session's main transcript, and
  `GET /api/sessions/{session_id}/sidecar?path=<name>` fetches a
  sidecar beside it. Both require a signed-in non-guest session (a
  guest is refused on `/api/sessions*`). The transcript route serves
  the main file only; subagent files are separate sessions.

### Export the derived rows

`records` and `tool_uses` key on `file_key`, everything on the session
joins through `files`:

```sql
\copy (SELECT * FROM files    WHERE project_id = '<project-slug>')      TO 'files.csv'    CSV HEADER
\copy (SELECT * FROM records  WHERE file_key LIKE '<bucket>/<project-slug>/%') TO 'records.csv'  CSV HEADER
\copy (SELECT * FROM tool_uses WHERE file_key LIKE '<bucket>/<project-slug>/%') TO 'tool_uses.csv' CSV HEADER
```

The rollups are pure recombinations of `records`/`tool_uses`/`files`
and are not exported separately. Run `psql` against the app DB
(`DATABASE_URL_VIZ` in the service's environment).

## Erasing one person's data

Do the steps in order. Steps 1–3 erase the transcript data; steps 4–5
erase the account data.

### 1. Delete their objects from every configured bucket

Delete the transcripts, sidecars and any
`sessions/<project-slug>/project.json` marker under the person's
prefixes, in **every** bucket named in `R2_BUCKET` (a deploy may serve
several, joined by `+`). claudit has no delete API and never writes to
the bucket, so this is an operator action in the bucket itself.
Deleting an object does not by itself remove its database rows — they
persist until the sweep — so run step 2 promptly after step 1.

### 2. Run an ingest so the orphan sweep cascades the rows

```bash
curl -X POST http://127.0.0.1:8000/admin/ingest \
  -H "X-Admin-Token: $ADMIN_TOKEN" \
  -H "Origin: http://127.0.0.1:8000"
```

Restarting the service also kicks a fresh ingest. The run lists every
configured bucket, and the orphan sweep deletes every `files` row whose
bucket-qualified key the listing did not show. The foreign keys cascade
(`records` and `tool_uses` reference `files`), emptied `projects` rows
are dropped, and each deleted file's `r2_etag` is evicted from the
in-process transcript cache, so a deleted transcript cannot stay
readable from the cache after its rows are gone. The rollup tables are
then rebuilt from what remains.

### 3. Verify nothing remains

```sql
-- All of these must return 0.
SELECT count(*) FROM files
  WHERE file_key LIKE '<bucket>/<project-slug>/%';
SELECT count(*) FROM records
  WHERE file_key LIKE '<bucket>/<project-slug>/%';
SELECT count(*) FROM tool_uses
  WHERE file_key LIKE '<bucket>/<project-slug>/%';
SELECT count(*) FROM projects WHERE project_id = '<project-slug>';
SELECT count(*) FROM lane_markers
  WHERE marker_key LIKE '<bucket>/sessions/<project-slug>/%';
```

`records` and `tool_uses` cannot outlive `files` (the FKs cascade), so
a zero on `files` plus a non-empty rollup would mean a stale derived
build — rerun the ingest.

### 4. Delete their session rows

```sql
DELETE FROM user_session WHERE user_id = <user-id>;
```

This invalidates their cookies immediately (session resolution finds no
row) and removes the stored secret, generation counter and credential
fingerprint. Guest sessions have no rows anywhere — they are signed
with a per-process secret and die on restart.

### 5. Delete the user in the external auth DB

The `users` table is managed by your user-management process, outside
this application (claudit holds only a read-only connection to it).
Delete the user's row there. Any session that somehow still resolves
fails within about 60 seconds: session resolution rechecks the stored
credential fingerprint against the auth DB through a 60-second cache,
and a deleted or changed credential no longer matches.

### 6. Logs and backups

Access logs (journald for the service unit, nginx access logs, the
Cloudflare edge) can carry the transcript URLs a browser or API client
requested, and backups of the app DB, the auth DB and the buckets carry
pre-erasure copies. Both are operator-managed and outside this
repository: apply your retention and backup-rotation policy to them.

## What legitimately survives

- **Rebuilt aggregates never outlive their sources.** Every rollup is
  DELETE+INSERT-rebuilt from `records`/`files`/`tool_uses` on each
  ingest, so once the source rows are gone the next rebuild cannot
  contain the person.
- **Cached aggregate responses.** The in-process response cache marks
  its entries stale at the end of an ingest but keeps serving them
  until refreshed, and each entry's hard TTL is one hour — so a
  dashboard response may still show pre-erasure aggregate numbers for
  up to an hour after step 2. A request during that window triggers the
  background refresh, which recomputes from the rebuilt tables.
  Restarting the service drops every in-process cache outright.
- **Login rate-limit histories** hold user ids, IP addresses and
  timestamps for five-minute windows in process memory and die with
  the process.
- **Logs and backups survive** until your operator policy says
  otherwise (step 6).
