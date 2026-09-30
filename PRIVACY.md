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
  projects. Only the Claude layout carries the slug in its object
  keys; the lane layout's keys are hash-segmented
  (`sessions/<hash>/...`), and the slug comes from the path named in
  the lane project's `project.json` marker at ingest time.

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
| `suppressed_models`, `project_aliases` (app DB) | Operator config, ships empty. `project_aliases` rows (pattern and fold target) can carry a person's slug where an alias is configured | Operator `DELETE` of the rows naming them (step 3 below) |
| `user_session` (app DB) | Per-user session secret, generation counter, credential fingerprint | Operator `DELETE` (step 6 below) |
| External auth DB `users` | `user_id` + `config` JSONB holding the PBKDF2 `web_password_hash` and `web_password_salt`. Read-only to claudit | Deleted by your user-management process, outside this app |
| In-process caches | Raw transcript bytes (LRU keyed by etag), aggregate API responses, short-lived login-rate-limit and auth-config entries | Orphan sweep (transcript LRU), ingest invalidation, process restart, and 60 s / 5 min expiry |
| Server and proxy logs, backups | Request lines (journald for the service, nginx access logs, Cloudflare edge), DB and bucket backups | Operator policy — outside this repo |

The databases hold statistics and metadata, not message text — with a
few deliberate fragments. `tool_uses.error_text` (the leading 200
characters of a failed tool result), `tool_uses.read_targets` /
`write_targets` (file paths a call named) and `tool_uses.dispatch_name`
(the name a dispatch gave its agent) cascade away with their `files`
row; `projects.project_id` / `display_name` (the directory-path slug
and, for a lane project, the path itself as the display name) go when
the emptied project row is dropped; and `lane_markers.path` (the
directory path a lane project marker named) goes when the marker object
leaves the bucket.

## Exporting one person's data

### Find their projects and sessions

```sql
-- Candidate projects. The slug replaces every non-alphanumeric
-- character of the directory path with '-', so /home/subject.example/app
-- is stored as -home-subject-example-app: search the SLUG form here
-- ('subject-example'), not the raw path form ('subject.example') — a
-- dot is a literal in SQL LIKE and matches nothing on the slug.
-- display_name carries the raw directory path for a lane project, so
-- search the raw form there too. Both arms return candidates to
-- confirm by eye, not exact ids.
SELECT project_id, display_name, first_seen_at, last_seen_at
FROM projects
WHERE project_id LIKE '%subject-example%'
   OR display_name LIKE '%subject.example%';

-- Their files by OBJECT KEY, which keeps the directory slug even when
-- an operator alias has folded the person's project under a shared
-- project id (the fold re-keys rows, never the bucket objects).
-- Claude-layout keys embed the slug; lane keys are hash-segmented and
-- are found through the marker query in the erasure section instead.
SELECT file_key, project_id, session_id, is_main
FROM files WHERE file_key LIKE '%subject-example%';

-- Operator aliases fold one project's rows under another project id.
-- Check every slug the person's directory paths produce (replace every
-- non-alphanumeric character with '-'): a row whose PATTERN matches a
-- slug is what folded that project, and its target id is where the
-- files now sit; a row whose target IS the person's slug folds other
-- projects onto them. There may be no projects row for a folded slug
-- at all; the object-key query above still finds the files themselves.
SELECT pattern, project_id, note
FROM project_aliases
WHERE '<project-slug>' LIKE pattern OR project_id = '<project-slug>';
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
  `GET /api/sessions/{session_id}/sidecar?path=<name>` fetches any file
  beside it — a subagent transcript (`subagents/agent-<id>.jsonl`) or
  its meta sidecar. Both require a signed-in non-guest session (a
  guest is refused on `/api/sessions*`). Subagent transcripts share the
  session id and sit in `files` as their own rows with
  `is_main = FALSE`; list them with `SELECT file_key FROM files WHERE
  session_id = '<session-id>'`.

### Export the derived rows

`records` and `tool_uses` key on `file_key`, everything on the session
joins through `files`:

```sql
\copy (SELECT * FROM files     WHERE project_id = '<project-slug>') TO 'files.csv'     CSV HEADER
\copy (SELECT * FROM records   WHERE file_key LIKE '<file-key-prefix>/%') TO 'records.csv'   CSV HEADER
\copy (SELECT * FROM tool_uses WHERE file_key LIKE '<file-key-prefix>/%') TO 'tool_uses.csv' CSV HEADER
```

`<file-key-prefix>` is the actual `file_key` prefix the files query
returned: `<bucket>/<project-slug>` for a Claude-layout project,
`<bucket>/sessions/<hash>` for a lane project (lane keys are
hash-segmented, so a slug-shaped pattern would match none of them).

The rollups are pure recombinations of `records`/`tool_uses`/`files`
and are not exported separately. Run `psql` against the app DB
(`DATABASE_URL_VIZ` in the service's environment).

## Erasing one person's data

Do the steps in order. Steps 1–4 erase the transcript data and the
config rows naming the person; steps 5–6 erase the account data.

### 1. Delete their objects from every configured bucket

Delete every object under the person's prefixes — transcripts,
subagent files, sidecars — in **every** bucket named in `R2_BUCKET`
(a deploy may serve several, joined by `+`). The prefixes come from the
export section's object-key query: `files.file_key` keeps the directory
slug even when an alias has folded the person's project under a shared
project id, so the keys are the fold-proof inventory of their objects.
For a lane project, also delete its `sessions/<hash>/project.json`
marker object: lane bucket keys are hash-segmented, so the marker sits
under no slug-derivable prefix, and the hash segment prefixes every
object of that lane project (`sessions/<hash>/...`). Find the exact
key from the path the marker named:

```sql
SELECT marker_key FROM lane_markers WHERE path LIKE '%subject.example%';
```

(Claude-layout projects have no marker objects.) The marker key names
the lane project's whole subtree: from
`<bucket>/sessions/<hash>/project.json`, delete everything under
`<bucket>/sessions/<hash>/` — wire files, subagent transcripts,
sidecars, the marker. This is also the route for a lane project an
alias has folded under a shared project id: the hash-segmented keys
carry no slug, but the marker still names the person's path. claudit
has no delete API and never writes to the bucket, so this is an
operator action in the bucket itself. Deleting an object does not by
itself remove its database rows — they persist until the sweep — so
run step 2 promptly after step 1.

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

### 3. Remove the config rows naming them

`project_aliases` can fold the person's directories — as the pattern
that matched their slug and/or as a fold target. A pattern stores
wildcards where the slug had separators, so substring-searching it for
the slug finds nothing; test the slug AGAINST the pattern, which is
the fold's own matching rule. Run once per slug of theirs, then delete
the rows naming them — the pattern is itself personal data:

```sql
DELETE FROM project_aliases
WHERE '<project-slug>' LIKE pattern OR project_id = '<project-slug>';
```

(The next ingest takes a full derived rebuild over the alias-table
change; harmless, since the person's rows are already gone.)

### 4. Verify nothing remains

```sql
-- All counts must be 0. <prefix> is each bucket-qualified prefix you
-- deleted in step 1 (e.g. <bucket>/-home-subject-example-app, or
-- <bucket>/sessions/<hash> for a lane project), one run per prefix.
-- The proof keys off what was actually deleted, not off the find
-- queries — an alias fold can hide a person's rows from those.
SELECT
  (SELECT count(*) FROM files     WHERE file_key LIKE '<prefix>/%') AS files,
  (SELECT count(*) FROM records   WHERE file_key LIKE '<prefix>/%') AS records,
  (SELECT count(*) FROM tool_uses WHERE file_key LIKE '<prefix>/%') AS tool_uses;

SELECT count(*) FROM projects
WHERE project_id LIKE '%subject-example%' OR display_name LIKE '%subject.example%';
SELECT count(*) FROM lane_markers WHERE path LIKE '%subject.example%';
SELECT count(*) FROM project_aliases
WHERE '<project-slug>' LIKE pattern OR project_id = '<project-slug>';
```

`records` and `tool_uses` cannot outlive `files` (the FKs cascade), so
a zero on `files` plus a non-empty rollup would mean a stale derived
build — rerun the ingest.

### 5. Delete the user in the external auth DB

The `users` table is managed by your user-management process, outside
this application (claudit holds only a read-only connection to it).
Delete the user's row there **before** touching `user_session`: while
the auth user exists, a sign-in writes a fresh `user_session` row, so
deleting the session rows first leaves a surviving row behind if
anyone signs in between the two steps. With the auth user gone, no new
sign-in can succeed (every credential lookup fails), and any session
that somehow still resolves fails within about 60 seconds: session
resolution rechecks the stored credential fingerprint against the auth
DB through a 60-second cache, and a deleted or changed credential no
longer matches.

### 6. Delete their session rows

```sql
DELETE FROM user_session WHERE user_id = <user-id>;
SELECT count(*) FROM user_session WHERE user_id = <user-id>;  -- must return 0
```

This invalidates their cookies immediately (session resolution finds no
row) and removes the stored secret, generation counter and credential
fingerprint. Guest sessions have no rows anywhere — they are signed
with a per-process secret and die on restart.

### 7. Logs and backups

Access logs (journald for the service unit, nginx access logs, the
Cloudflare edge) can carry the transcript URLs a browser or API client
requested, and backups of the app DB, the auth DB and the buckets carry
pre-erasure copies. Both are operator-managed and outside this
repository: apply your retention and backup-rotation policy to them.

## A person who only appears inside other people's transcripts

Someone who never signed in can still appear inside sessions other
people wrote: their name or address in message text (which lives only
in the bucket objects), in `tool_uses.error_text` (the leading 200
characters of a failed tool result), or in `tool_uses.read_targets` /
`write_targets` (the file paths a call named). None of it is keyed by
a user id, and the per-person steps above — keyed by project slug or
user id — never reach it. Find it by content:

```sql
-- Mentions in the database fragments. The needle is whatever
-- identifies the person (here a hypothetical third.person address and
-- their notes directory). Both queries return candidates to confirm
-- by opening the transcript, not exact matches.
SELECT file_key, line_num, idx, error_kind, error_text
FROM tool_uses WHERE error_text LIKE '%third.person%';

SELECT file_key, line_num, idx, read_targets, write_targets
FROM tool_uses
WHERE EXISTS (SELECT 1 FROM unnest(read_targets) AS r(t)
              WHERE t LIKE '%third.person%')
   OR EXISTS (SELECT 1 FROM unnest(write_targets) AS w(t)
              WHERE t LIKE '%third.person%');
```

The message text itself is only in the objects, so an exhaustive
search mirrors the bucket and greps:

```bash
aws s3 cp s3://<bucket>/ ./mirror/ --recursive    # or any S3 client
find ./mirror -type f -name '*.jsonl' \
  -exec grep -l 'third.person' {} +
find ./mirror -type f -name '*.jsonl.xz' -exec sh -c \
  'xz -dc "$1" | grep -q "third.person" && echo "$1"' _ {} \;
```

Resolve a hit to its host session and project:

```sql
SELECT file_key, session_id, project_id
FROM files WHERE file_key = '<the file_key of a hit>';
```

### What erasing such a mention takes

claudit has no transcript-editing feature and this runbook does not
invent one. A mention lives inside the host session's objects, so the
options are the operator's:

- **Delete the host session's objects** (step 1 of the erasure
  section, using the host session's own prefixes). This removes the
  mention — and with it the whole host session. The host is a separate
  data subject; whether their consent or cooperation is needed is an
  operational question, not a technical one.
- **Edit the host session's objects** to remove the mention and put
  each object back under the same key. The next ingest sees the
  changed etag and reparses, which replaces the database rows from the
  edited content. Edit whole JSONL lines only: the parsers skip a
  malformed line silently, so a broken line quietly disappears from
  the statistics instead of raising an error.

Both change another person's session. Whether erasure of such mentions
is required at all is a question for a specialist; this runbook only
documents where the mentions live and how to find them.

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
- **Mentions inside other people's transcripts survive** until the
  host session's objects are deleted or edited (see the previous
  section): the per-person find and verify steps are keyed by project
  slug or user id and never reach them.
- **Logs and backups survive** until your operator policy says
  otherwise (step 7).
