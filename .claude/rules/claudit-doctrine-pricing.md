---
paths:
  - "**/src/pricing.json"
  - "**/src/pricing_notices.json"
  - "**/src/rates.js"
  - "**/backend/pricing.py"
  - "**/backend/pricing_load.py"
  - "**/backend/pricing_document.py"
  - "**/backend/pricing_rows.py"
  - "**/src/pricing-loader.js"
  - "**/src/pricing-rows.js"
  - "**/src/hhmm-spelling.js"
  - "**/backend/rate_fingerprint.py"
  - "**/backend/rate_boundaries.py"
  - "**/backend/ingest_reprice.py"
  - "**/backend/long_context.py"
  - "**/scripts/ci/refresh_*.py"
  - "**/scripts/ci/backfill_provider_rates.py"
---

# claudit Doctrine — pricing and rates

## Rates are a function of (model, timestamp) (SV-DATED-RATES)

`pricing.rate_for(model, ts)`: a model may carry dated overrides in
`DATED_RATES`, `(end_exclusive_utc, rates)` windows per exact model key,
derived from its history in `src/pricing.json` (SV-RATE-DATA). Price at
the request's timestamp, never render time: `parse.py` passes each
record's own `ts`; omitting `ts` yields LIST price (never a silent
discount). The read-time fold agrees: `rate_epoch_sql` maps a NULL ts
to epoch -1, whose representative instant is None, pricing exactly as
persist and reprice did. The cache view's range predicate admits a
NULL-ts record in every range, so the fold decomposes the same record
its stored cost describes.

An expired window is NEVER dropped. Every `PRICING_VERSION` bump
reprices every record through the windows (SV-REPRICE; a record stamped
by a newer binary is skipped), so removing a window reprices its history
at list. The machinery is tested through `tests/conftest.py`'s
`synthetic_dated_rate` fixture, not only live entries, so it cannot rot
while the table is empty.

A read path that RE-DERIVES rates from summed tokens must group each
record by ITS OWN rate epochs — the instants where
`pricing.resolve(model, ts, provider)` can change for its (model,
provider), listed by `rate_boundaries` — AND by `COALESCE(long_context,
FALSE)` (the Codex meter multiplies the whole input side by 2 and output
by 1.5; a fold ignoring it drifts from `SUM(cost_usd)`) AND by the
stored long-context input/output factor pair. Reprice batches commit
independently, so records can carry different pairs during an interrupted
reprice; `records.long_context_input_mult` and
`records.long_context_output_mult` preserve the pair that priced each
record. A NULL pair on a metered row is a legacy row priced at the global
defaults. Totals always come from stored `cost_usd`; never recompute them at read time. The
bare first-party id of a tracked vendor row resolves through that row
(SV-RATE-DATA), so its epochs are the vendor row's: dated-window ends
plus the row's start when it has one.

Epochs are per (model, provider), never the global `pricing.RATE_EPOCHS`:
a log-backed provider row adds thousands of boundaries, and a fold over
the global list splits every record at every other row's changes. So
the cache view reads the distinct (model, provider) pairs from
`usage_rollup`, joins each record to its pair's boundary array, and
takes `width_bucket` over it. A pair `usage_rollup` does not list yet
(ingested before its rollup rebuild) falls back to the global list:
exact, only slower.

## Rates are data in one file (SV-RATE-DATA)

Every rate lives in `src/pricing.json`: `models` (normalised key →
history), `providers` (normalised model → provider → history),
`provider_rates_fetched`, and `openrouter` — the account's data region,
each provider-table model's OpenRouter id (SV-RATE-REFRESH), the vendor
configuration, and the tracked table. `openrouter.vendor.prefixes` lists
the namespaces whose first-party list pricing the file tracks; a tracked
entry `openrouter.models[key]` carries `id`, optional model-wide
`context_length`, and — when the key prices a
first-party vendor model — `vendor_host`: the host whose provider row
`providers[key][vendor_host]` holds that model's history, which the
vendor's BARE first-party id resolves through. A bare id prices from
that row's dated windows, including its web-search rate, and without its
provider-specific schedule. The host's schedule applies only to requests
THROUGH it. A transcript id
spelled with a tracked vendor prefix whose remainder is a tracked bare
form folds to that form and resolves through the tracked key's provider
rows. Every tracked entry with a derivable bare form joins the fold,
including entries without `vendor_host`: a key under a tracked prefix
derives its remainder, and an already-bare key derives itself. Other
namespaced keys stay outside the fold. Only entries carrying `vendor_host`
join the first-party host table; id-only entries supply no first-party
fallback when no provider row matches. A normalized
prefixed id whose remainder is not a tracked bare form stays unchanged and
follows the existing fallback: a Claude-family match gets its tier rate;
otherwise it gets the default, and no prefixed vendor row is selected. The
bare forms are a validated namespace: one bare form per fold entry,
including id-only entries, and never one that is also a models-table key — a transcript id
would otherwise resolve two ways, so the loaders refuse the file naming
both rows.
The grouped `long_context_meters` field is the sole long-context membership
and threshold source. Each group has exactly `threshold` and `models`:
`threshold` is a positive integer unique across groups, and `models` is a
non-empty list. A model key appears at most once across the field, names a
models-table key or tracked key, and is either a bare string (global
multipliers) or a one-key object mapping the key to an object with optional
`input_mult` / `output_mult`. Unknown fields, malformed entries, invalid
thresholds or multipliers, duplicate thresholds, empty groups, repeated
keys, and keys naming neither table are refused. The removed
`long_context_models` field is refused if present. The loaders fold groups
to the in-memory `key -> {threshold, input_mult?, output_mult?}` map;
membership is exactly its key set, so every member has a threshold.
`backend/pricing.py` and `src/rates.js` hold logic only and both read
it — the backend at import through `backend/pricing_load.py`, which
`pricing.py` re-exports, the browser synchronously before first use
(node reads it beside the module). Nothing the deployed app reads may
redirect which document the backend loads; the suite-cost bench's
bounded document is injected on the bench side only (SV-CI-RATCHETS). The browser fetches the URL in the
pricing-loader tag's `data-pricing` attribute in `public/index.html`,
cache-busted like every `/src` asset; the file sits in `src/` because
that is what the app serves. No rate literal belongs in either source
file.

Beside the tables sits one grouped `long_context_meters` list. Each group
has a positive integer `threshold` (unique across groups) and a non-empty
`models` list. Each model entry is either its dashed string key, which uses
global multipliers, or a one-key object mapping that key to an object with
optional `input_mult` / `output_mult`. The key must name a models-table or
tracked key, and it may appear only once across all groups. Unknown fields,
bad group/model shapes, duplicate thresholds, empty groups, repeated keys,
and invalid thresholds or multipliers are refused; the old
`long_context_models` field is refused if present. Optional multipliers must
be finite positive numbers and override `pricing.LONG_CONTEXT_INPUT_MULT`
and `LONG_CONTEXT_OUTPUT_MULT` independently; each member's explicit
threshold overrides `pricing.LONG_CONTEXT_THRESHOLD`. Both loaders fold the
list to `key -> {threshold, input_mult?, output_mult?}` and membership is
exactly the map's keys. The complete entries ride the reprice pass's
rate_fingerprint; the browser exposes them as `window.longContextMeters`.

The stored document carries `_format: "pricing-rows/3"`. `models`
histories retain object entries. Each `providers[model][host]` series is
a non-empty list of positional rows: `[T, F, O, R]`, `[T, F, O, R, N]`,
`[T, F, O, R, {rest}]`, or `[T, F, O, R, N, {rest}]`. F/O/R are the
`fresh`/`output`/`read` rates, preserving their JSON int/float types. The
optional note N is a number only when rendering it as `n% off` restores
the identical string in both loaders; otherwise it is the original
string. The optional rest object carries other entry fields:
`create_5m`, `create_1h`, `web_search`, `context_length`, and `schedule`.
It cannot override `from`, `fresh`, `output`, `read`, or `note`.
`context_length` is not a rate, never enters a cost or the rate fingerprint,
and its move appends a detection-dated entry with unchanged token rates;
it creates no price window only when all other state, including `note`, is unchanged.

T is mandatory: null means the undated leading entry, an integer means
one instant, and a non-empty integer list means that state's several
instants. The first dated row's first instant is absolute Unix seconds
UTC; every subsequent row's first instant is a delta from the previous
row's FIRST instant, not its last occurrence. Within a T-list, subsequent
numbers are deltas from the preceding element. Rows ascend by first
instant and lists ascend strictly; instants across the series are
distinct. Both loaders refuse negative, non-integer, duplicate, or
out-of-range deltas, malformed slots and rest objects, misplaced nulls,
missing/wrong format markers, and object-shaped stored provider entries,
naming the provider row where applicable. A fractional or exponent
spelling of a timestamp number is refused before JavaScript loses its
numeric spelling.

`backend/pricing_rows.py` and `src/pricing-rows.js` decode to object
entries. T-lists become `from` lists of canonical whole-second UTC
`YYYY-MM-DDTHH:MM:SSZ` strings; a scalar remains scalar. An undated
leading decoded entry explicitly carries `from: null`. Encoding and
`migrate_pricing_doc` accept either absent or null leading `from` and
store T=null. The losslessness bar is exact typed equality on this
explicit-null, canonical UTC decoded shape:
`decode(encode(doc)) == doc`, including int/float distinctions. Migration
converts legacy object histories after a rebase as well as at initial
conversion. Expanding/compacting an editable document preserves grouped
instants. At raw boundaries the order is decode, check stored shape,
expand defaults, then validate; no boundary decodes twice.

For lookup tables, flatten all grouped instants into chronological order.
The state owning instant j applies until instant j+1; the state owning
the latest instant is the list price, even when its stored row precedes
another row. Schedules follow each occurrence of their owning state. Every
instant is bounded by `provider_rates_fetched`. The host's
start and its fallback before that start keep their existing meaning;
a null-first state covers all earlier time. Complete log histories stay
stored; timestamp lists carry oscillation without substituting schedules.
Schedules from `pricing.overrides` retain their weekly UTC behavior.

Each decoded row's history is append-only except for corrections and
SV-RATE-REFRESH's automatic log-backed rewrites. Its effective vector is
five finite non-negative token rates (`fresh`, `create_5m`, `create_1h`,
`read`, `output`). `fresh`, `read`, and `output` are always present;
`create_5m` and `create_1h` are present only when each differs from
`fresh`, and each absent cache-write rate defaults to `fresh`. An entry
also carries an optional string `note` and, on a provider entry only,
an optional `schedule`. Every schedule window has the same five effective
rates and the same absent-means-`fresh` rule. Both loaders refuse a `band`
key, naming the row. The leading model entry's `from` defaults
to `null` when absent. A model row therefore covers all time. A provider
row's first may name its start instant; before it, that host's records
price by the model alone, and the start joins `RATE_EPOCHS`. Every later
model-history `from` is exactly
`YYYY-MM-DDTHH:MM:SS` plus `Z` or `±HH:MM`, every field in range (a
real calendar day, hour 0-23, minute/second 0-59, offset under 24:00),
strictly after its predecessor. A model history's newest entry is its
list price; each earlier one applies until its successor's `from`
(SV-DATED-RATES). Provider histories use the flattened order above.

A sampled price change APPENDS an editable `{"from": T, ...}` entry, then
the shared serializer encodes provider storage rows. Entries are never
edited or removed by hand except to correct a misstated price or instant
in a human commit that bumps `PRICING_VERSION`. The refresh automatically
rewrites log-backed provider histories from the listed-pricing log each
run (SV-RATE-REFRESH), including closed windows: those windows are the
log's to rewrite. A rewrite that reprices stored usage bumps
`PRICING_VERSION`, repricing from stored columns with no reparse. Hand
corrections remain an additional path with the same bump requirement.
Both loaders refuse a rule-breaking file,
naming the row; the browser throws an error naming `pricing.json` on
any load failure.

A correction that moves a web-search price out of a legacy note and into
`web_search` is a human SV-RATE-DATA edit: keep any discount note, remove
the web-search sentence, and bump `PRICING_VERSION` in the same commit.

A provider history entry may also carry `web_search`, a finite
non-negative USD-per-search rate. Absent or zero means no search billing.
The field is independent of token schedules and price-log points; a
search-rate move is sampled from the listing at detection time and appended
as a normal dated entry whose token rates equal its predecessor. The
resulting instant is a `resolve()` epoch. Old `web_search` note text is
ordinary note text and never contributes a `request_fee`.

A provider entry may carry a weekly UTC `schedule`: a list of windows
`{days?, start?, end?, rates}`.

- `days`: distinct lowercase weekday names; absent means every day.
- `start`/`end`: HHMM JSON integers 0–2359, both or neither (neither is
  the whole day), end-exclusive, wrapping past midnight when `start` is
  later than `end`. A fraction or exponent spelling (`1400.0`) is
  refused on both sides. A wrapped window's `days` are the record's own
  UTC weekday, not the day the window opened.
- `rates`: the five rates.

Both loaders price a record by its UTC weekday and time: the first
window it falls in, else the entry's own rates (also the price with no
timestamp). Windows repeat weekly, so they are not rate epochs. The
browser prices each record exactly. The read-time fold (`api_common`)
cannot: a scheduled row's buckets take their split from the rates at
the epoch's representative time, scaled to stored `cost_usd`. The total
is always exact; the split is exact when every window scales all five
rates alike, as the live schedules do.

`note` is human annotation such as a discount; neither loader interprets
web-search note text as a price. `web_search` is the PRICED USD-per-search
field and resolves from the record's own `ts`, through the same history
and epoch machinery as token rates. `pricing.compute_cost` adds
`web_search_requests * web_search` to token cost. The legacy
`records.request_fee_usd` column remains additive and nullable, but ingest
does not write it and repricing sets it to NULL. `rate_fingerprint`
covers the per-search rate. The `/api/cache` fold groups by its epochs and
adds a separate `web_search` bucket, so the token TTL split stays intact
and the bucket sum reconciles with stored `cost_usd`. A lane without a
transcript search count stores NULL and bills no search.

Provider history rows render as one compact JSON line each. Outside
`providers`, the file retains sorted keys and two-space indentation.
Every writer uses the shared `serialize_pricing_doc`, keeping the file
valid JSON and byte-stable across writers.
File order never decides key matching — the LONGEST matching key wins;
order only breaks a family-fallback tie between equal versions (first
key wins).

## Provider rates refresh from OpenRouter (SV-RATE-REFRESH)

`.github/workflows/refresh-pricing.yml` runs
`scripts/ci/refresh_provider_rates.py` hourly (identically on dispatch)
and commits to `master` as `github-actions[bot]` only after the full
suite passes on the new data. Hand corrections also require a human commit
and `PRICING_VERSION` bump (SV-RATE-DATA).

- Log-backed histories are rewritten from OpenRouter's recorded change
  points; sampled price moves APPEND entries at detection time. A
  first-seen sampled host gets a row
  beginning then; a first-seen log-backed host gets its log from the
  last state in force at `HISTORY_FLOOR` (`refresh_provider_rates.py`,
  the account's first OpenRouter-lane record): states the account's
  OpenRouter-lane records predate price nothing the meters hold, and
  the state in force at the floor prices the floor onward. A
  host no longer listed keeps its row untouched and is reported.
- Normalisation: USD per token becomes USD per million. The listed
  price already has any promotional discount applied; the discount goes
  in `note` (`N% off`), never a rate. Cache writes take the listed
  write price when nonzero, else the input rate; an unlisted cache-read
  price is the input rate: a host that does not list a cache-read price
  bills cache reads at its fresh rate. A listed `input_cache_write_1h` is the `create_1h` rate,
  the one number the TTL split turns on (SV-COST-SPLIT); absent or
  zero, the 5m tier is it too, so a listing that does not split them
  changes nothing. A host whose listing splits them is sampled, never
  log-backed: the log's five fields carry no 1h tier, so no series can
  be joined to it. `web_search` is already USD per search; it remains a
  separate rate sampled from the listing at detection, independently of
  token log change times. Endpoints of one host at one price are one row.
- The account is billed only by endpoints in its data region,
  `openrouter.data_region`: `global` or a lowercase region code.
  - An endpoint tag is `host` or `host/<suffix>[/<suffix>...]`. A
    suffix is a region when it is a known region code (`global`, `us`,
    `eu`, `uk`, `ca`, `ap`, `asia` and the others the script lists),
    alone or qualified as `<region>-<area>`, `<region>-<area><digits>`,
    or `<region>-<area>-<digits>`, in any case (`us-east5`,
    `europe-west4`, `us-east-1`). A suffix that is neither a region nor
    a known quantization (`fp4`, `fp8`, `nvfp4`, `bf16`, …) is logged in
    the run's notices and refuses nothing.
  - The filter admits endpoints with no recognized region suffix and
    endpoints whose suffix names the configured region. Thus `global`
    admits `azure/global` and `google-vertex/global`, as well as a bare
    endpoint, and filters `google-vertex/europe`; `us` admits `/us` and
    bare endpoints and filters `/europe`.
  - A host whose endpoints all lie outside the region is not listed for
    the account, so it is reported as vanished.
- Endpoints are grouped by host first, so a malformed endpoint refuses
  only its host.
- **Schedules.** A host's `pricing.overrides` (weekly `utc_days` /
  `utc_start` / `utc_end` windows with the prices they override) become
  its entry's `schedule`. A change to the default rates or to the
  schedule (compared whole) is a move, and the whole entry is appended.
  - **The default.** OpenRouter lists a scheduled host's top-level
    price as the window active at fetch time. So the default comes from
    the top-level price only when the fetch is outside every window;
    inside one, the stored default is kept and only the schedule is
    compared (a whole-week schedule never moves its default). A
    first-seen host fetched inside a window is REFUSED unless its
    schedule covers every instant of the week — then the default prices
    nothing, so the row starts with the listed top-level price and a
    notice reports the seeding; otherwise the next fetch outside every
    window starts the row.
  - A price a window does not name is the entry default (top-level
    outside every window, the kept default inside one).
  - An appended entry whose windows do not each scale all five default
    rates by one factor is reported as a "non-uniform schedule" notice:
    the fold's Token Breakdown split is then approximate (SV-RATE-DATA).
- **Log-backed rows are dated by OpenRouter's own change log**, a
  per-endpoint price history its model pages read and its documented
  API lacks:
  `https://openrouter.ai/api/frontend/v1/stats/listed-pricing?permaslug=<canonical_slug>&variant=standard&shape=v4&range=all`
  (`canonical_slug` from `/api/v1/models`). Per listed endpoint it
  returns `endpointId`, `providerName`, `providerSlug` and, per field
  (`input`, `output`, `cacheRead`, `cacheWrite`, `discount`), change
  points `{at, value}` in USD per million, discount applied, since
  first listing. Every run fetches it with `range=all` for every
  tracked model, beside the endpoints listing.
  - **Joining series to endpoints.** A series carries no tag. A host is
    log-backed only when: all its endpoints share one tag prefix
    (before the first `/`) no other host of the model uses; the log has
    exactly one series per such endpoint; each series' current state
    (newest rates, normalised below) equals exactly one endpoint's
    listed price, no two series matching one endpoint; no endpoint or
    series carries a schedule (`pricing.overrides` or a series
    `schedule`); the host has no `cheapest` resolution; and the
    host's full endpoint selection — an explicit pin or the automatic
    rules of `refresh_selection_rules` — selects exactly ONE endpoint
    (an equal-price group with interchangeable tags remains ambiguous). The
    row's token history is that endpoint's series. `web_search` is not
    in the log and does not block this join. Anything else is
    ambiguous, and the host is sampled, never guessed.
  - **The listed-pricing log is the source of truth.** Every row the log
    can back is log-backed, including hand and stale rows. The refresh
    replaces any differing recorded rates even when stored costs change.
    Listed zeros do not exempt a row: the log's normalization is the record,
    including zero/absent cache writes normalized to input.
  - **Separate web-search sampling.** Every log-backed token entry carries
    forward the previous `web_search` rate. If the listing's search rate
    moved, append an entry at detection time with the predecessor's token
    rates and the newly sampled search rate. A search-only move creates a
    resolve epoch without changing token prices.
  - **Change points to entries.** A series' state at an instant is each
    field's newest value at or before it, and exists once `input` and
    `output` both have a point. Each instant at which the five rates
    change — normalised like the listing: cache read null/absent is the input rate,
    cache write null/absent/0 is the input rate, values rounded to 10
    decimal places — is one entry, `from` truncated to whole seconds
    (points in one second collapse to its last state). A discount-only
    change is not an entry; `note` is the discount in force at `from`.
    A `null` `input` or `output` is not a price: the host is sampled.
  - **Hourly rewrite and append.** Each run rewrites existing log-backed
    rows from the complete log through the fetch instant, retaining every
    logged oscillation at its own change points and overlaying sampled
    search/context history as below. An out-of-step stored row is corrected
    by replacement. First-seen log-backed hosts keep the history-floor
    import above. Sampled rows stay append-only at detection time under
    the alternation rule; alternation never applies to log rewrites.
  - **The log must match the listing.** The series' state at the fetch
    instant must equal the listed price fetched in the same run: the
    run reads each series truncated at that instant, so a point dated
    after it — an announced change not yet in force — never backs a row
    or enters its history, and lands when a later fetch instant passes
    it (the log is refetched in full each run). Otherwise the host is
    sampled this run, with a notice.
  - **Sampled rows.** A host the log does not back, and every host of a
    model whose log fetch fails (HTTP error, timeout, `canonical_slug`
    missing from `/api/v1/models`, unrecognised shape), refreshes by
    sampling: a move is an entry dated at detection, under the
    alternation rule below. A failed log never guesses or refuses: a
    "listed-pricing log unavailable" notice names the model and reason,
    and the run stays green. A host the log does not back goes in the
    run's report with the reason, not its notices.
- **Automated full-history rewrite.** Each refresh run replaces every
  existing log-backed row's history with the log's COMPLETE entries through
  the run's fetch instant, keeping the row's start: when the old first `from` (`null`
  included) precedes the log's first change point, the new first entry
  takes it. The rewrite merges identical dated states into one entry
  whose `from` lists ALL their instants; rows without repeated states
  stay list-free. Flattening those lists reconstructs every change point
  and its rates losslessly. Entries merge only when their full state
  except `from` matches: different notes or other state fields remain
  separate, and a null start stays scalar (it cannot share a timestamp
  list). The rewrite overlays the row's sampled `web_search` and
  `context_length` history at sampled instants strictly before the fetch
  instant, carrying their leading state forward. The listing's current
  sampled state then follows the existing detection-time insert/defer rule.
  A candidate whose net priced and sampled state is identical to stored
  publishes no move and writes nothing. Annotation-only or grouping-only
  drift keeps the stored row and emits a notice. A priced rewrite carries
  the log's note/context corrections with it. The shared codec writes the
  stored row format. First-seen hosts retain the history-floor import;
  unbacked hosts follow sampling and alternation. The standalone
  `scripts/ci/backfill_provider_rates.py --as-of <instant>` remains a manual
  invocation of the same rewrite core through its supplied cutoff. Its
  source document must be band-free: the loaders refuse a `band` key,
  naming the row, before any fetch or write.
- **Alternating prices are reported, not appended — sampled rows
  only.** On a sampled row, when neither the listing nor the newest
  entry has a schedule and the listed rates equal a non-newest entry
  whose `from` is within 7 days of the detection time (never wall
  clock), the run reports an "alternating price" notice naming host and
  entry, and appends and bumps nothing. A first move away never
  matches; the flip back does. A genuine return to an older price is
  hand-appended, and the next run compares against it.
- **Unmodelled pricing refuses the host:** an override kind the script
  does not model, or any other pricing key at a nonzero price. `web_search`
  is a PRICED rate, validated as finite non-negative USD per search and
  stored in the provider entry, not in its note. Two endpoints differing
  only in that rate remain different listings. Its absence from the token
  price log leaves token history log-backed while search changes are
  sampled at detection.
- A run that reprices history or appends bumps `PRICING_VERSION` to one past the value in
  `backend/constants.py` — never a literal — in the same commit
  (records at or after a new `from` ingested before the deploy were
  priced at the old rate), and moves `provider_rates_fetched` to the later
  of its stored instant and detection. A run with no data move writes
  nothing. A new entry adds a rate epoch
  only to its own row's grouping (SV-DATED-RATES).
- The commit message omits a notice line the last committing run already
  carried, so a model still delisted — or a tag still unrecognised — is
  not reprinted every hour (issue #888). The baseline is
  `src/pricing_notices.json`, committed beside the pricing document: that
  run's FULL rendered notice set, the lines it showed AND the lines it
  suppressed. It must hold the suppressed lines — a baseline read from the
  previous commit's message would lose them and re-emit them in the next
  commit, alternating forever. A notice that is new, or that changed,
  still appears; an absent, unreadable or malformed file suppresses
  nothing, so the baseline can only under-suppress. A run with no data
  move publishes no commit, so it does not move the baseline.
- The commit body also drops the standing blocks — the vanished-host set
  (rows kept) and the sampled host classifications — which reprinted
  byte-identically every run (issue #933). The run's own stdout report
  keeps both blocks, and a committing run records them in the same state
  file as `vanished` (model → hosts) and `sampled` (model → host → the
  display classification), so a changed set or classification shows as a
  diff of that file. The subject carries only this run's counts —
  changed, new, refused; the vanished count left the subject with the
  blocks (issue #933).
- Ambiguity is never guessed. Each of these refuses its host or model,
  leaving its rows untouched:
  - a host with multiple in-region prices that remain ambiguous after
    any explicit resolution, unpinned automatic rule, or eligible
    same-quantization rule within a tag-only pin;
  - a stale resolution: a pinned tag not listed, or `cheapest` twins
    that differ beyond price, tie in price order, or have flipped
    order;
  - a resolution keyed on anything else, a price included;
  - an unrecognised response shape;
  - a tracked model still in the catalog with no endpoints, or none in
    the data region. A model gone from /api/v1/models is a delisting,
    not a refusal: the run skips it with a notice, its provider row
    keeps pricing stored records, and it rejoins the refresh on its own
    when the catalog relists it. An unreadable catalog proves nothing,
    so the empty-endpoints refusal stands while it cannot be read.

  When the refreshed document passes final validation, a refusal blocks
  only itself: every other move is rewritten or appended, tested and
  committed with the bump, then the run exits nonzero, naming each refused
  host. An out-of-step log-backed row is corrected by the log rewrite,
  including when detection is not after its stored newest `from`.
  `provider_rates_fetched` keeps the later of its stored stamp and detection.
  An ordinary sampled move blocked by timing keeps the existing
  untouched-with-notice behaviour. The refreshed document still passes
  final validation before writing.

- Automatic rules resolve the mechanical multi-price shapes with no
  human decision, beneath an explicit resolution (which keeps precedence,
  refusals included): a bare namespace endpoint beside its service tiers
  takes the bare one; the one quantization endpoint beside throughput
  tiers takes that one; the configured data region takes its own endpoint
  (the region filter, and under `global` the namespace's global endpoint —
  an explicit `<ns>/global`, else the bare `<ns>` — over a region spelling
  the vocabulary does not carry). A base tag beside only its suffix
  extensions at every other price takes the base when every endpoint
  agrees in quantization suffixes and all non-tag identity fields
  (quantization, context_length, max_completion_tokens, max_prompt_tokens).
  Extensions may use any suffixes, with no tier-name vocabulary. A nested
  service tier is a distinct offering, never a quantization price twin:
  only the quantization and recognized region suffixes, including global,
  belong to that quantization's family. One quantization's price twins take
  the cheaper, ordered by cache read, then input, then output — the twins
  are taken to be one offering priced per region, so the rule compares
  prices and not the identity fields a recorded `cheapest` pin compares
  (that pin's `ignore` exists for a listing artifact, which is why the
  hosts whose twins differ in a limit keep their pin). The rules normally
  fire where no resolve entry exists. After the region filter, their order
  is bare namespace, unique quantization, tier variant base, same
  quantization, then stored price continuity. That last rule takes the
  first endpoint of exactly one price group matching the stored five
  RATE_FIELDS, rounded to ten decimals; search fees and schedules do not
  affect the match. Without a stored row it never fires. Equal-price tags
  remain interchangeable and are named in the report. An identity mismatch
  disqualifies the tier-base rule and falls through to continuity or refusal.
  After a tag-only pin narrows to
  multiple prices of one known quantization, the same-quantization rule takes the
  cheapest within that tag, naming the pin and rule in its report. Single
  endpoint pins and other explicit resolutions keep precedence; every
  other ambiguous tag-only pin refuses. Twin-switch rises are reported,
  and ties or flips refuse. The report records every automatic choice as
  rule-resolved. A multi-price
  shape none of them fits still refuses, including a first-seen host with
  unrelated tags at different prices, or a tracked host with no structural
  fit and no stored-price match. Twin ties and flips refuse before the
  continuity rule can run. The rules live in
  `scripts/ci/refresh_selection_rules.py`, and none of them extends to the
  price log's own endpoint selection: a host the log would otherwise back
  needs its base pinned to stay log-backed.
- The fix is a human decision recorded in
  `openrouter.models.<model>.resolve.<host>`, with a `why`:
  - `{"tag": ...}` narrows to that exact tag, whatever its region. If it
    leaves one endpoint, the pin keeps precedence. If it leaves multiple
    price groups sharing one known quantization suffix, the same-quantization rule takes the
    cheapest within that tag and reports `rule-resolved`, naming the pin
    and rule; possible twin-switch rises are reported, and ties or flips
    still refuse. Every other ambiguous tag-only pin refuses.
  - `{"select": "cheapest"}` takes the cheaper of endpoints identical in
    tag, quantization and limits, comparing cache read, then input,
    then output. Different-region suffixes remain filtered for this
    resolution; the automatic preference for a configured-region endpoint
    over a bare alias applies only without an explicit resolution. A tag
    resolution above selects its exact tag and bypasses the region filter,
    whatever its region. The order survives price moves and breaks only
    when it flips; twins have no identity but price, so a flip is seen when
    the twin still at the row's price is no longer cheaper, and is refused.
    A tracked twin crossing the other between runs is indistinguishable
    from a genuine move and shows as a rise (unless the other fell
    simultaneously), so every rise of a `cheapest` row is appended and
    reported as a "possible twin switch".
  - The two combine, the tag narrowing first, whatever the region. The
    choice may record `"ignore": [fields]`: non-tag identity fields
    (quantization, context_length, max_completion_tokens,
    max_prompt_tokens) that a recorded human decision has found to be
    one offering's listing artifact, so `cheapest` compares the rest;
    the tag, the price order and every flip and tie refusal stay.
    Without the record the artifact refuses like any other difference.
  - A log-backed host resolves through the same shapes: a pin carrying
    `select`: `cheapest` — alone, with a tag, or with a recorded
    ignore — has no stable endpoint identity and stays sampled; a bare
    tag pin selects by it, and must name exactly one endpoint.

  A resolution is never a price value: a price pin breaks the moment
  the price moves.

## First-party vendor rates refresh from OpenRouter (SV-VENDOR-RATES)

The same hourly run tracks the first-party vendor table and the grouped
`long_context_meters` field, selected by VENDOR PREFIX over OpenRouter's
catalog at the vendor's own first-party endpoint (the endpoint whose tag
prefix is the vendor's own namespace), never a third-party host. The prefix
list is config: `openrouter.vendor.prefixes` — the same list the loaders
validate and the bare-id resolution matches through — so adding or dropping
a vendor is a one-line pricing.json edit, no code change. The pass lives in
`scripts/ci/refresh_vendor_rates.py` and runs inside
`refresh_provider_rates.main()`: one report, one commit, refusals block only
their own model. PRICING_VERSION bumps once, one past the file, when either
pass moved.

- **Only text-output catalog entries are in scope.** The vendor pass considers
  an entry only when `architecture.output_modalities` is exactly `["text"]`.
  Missing `architecture` or `output_modalities`, and any other output modality
  list, are dropped silently before endpoint selection and auto-add.

- **Auto-add joins the tracked set.** A catalog id under a listed prefix
  (with no `:<suffix>` variant suffix) whose derived key is not yet tracked
  is ADDED to `openrouter.models` as `{"id": <catalog id>, "vendor_host":
  <providerName of the selected endpoint>}` — the entry shape the issue-851
  migration gave every tracked vendor row. The pass writes NO RATES: the
  provider pass carries the `(key, vendor_host)` row from the NEXT hourly
  run, so a newly added model prices nothing for one run (the one-run
  pickup delay is the design; the loaders admit a tracked entry ahead of
  its provider row, and resolve()'s bare path falls through until the row
  exists).
- **Variants are never entries.** A catalog id with a `:<suffix>` variant
  (a `:batch` discounted tier, a `:free` tier — priced at zero by resolve()
  before any table — or any other suffix) is skipped: the bare id is the
  model.
- **The vendor endpoint.** The BARE tag (namespace, no suffix) is the
  first-party price; the vendor's other tags are its own service tiers
  (fast, flex) or quantizations. With no bare tag, the vendor-prefix
  endpoints must agree on one price (equal prices collapse); several prices
  refuse, and a `{"tag": ..., "why": ...}` pin recorded in
  `openrouter.vendor.resolve.<derived key>` takes one. No vendor-prefix
  endpoint at all is a NOTICE + skip (the model is offered only through
  third-party hosts; any tracked entry's own state stands).
- **The derived key** is the slug, dot-folded (`openai/gpt-5.5` →
  `gpt-5-5`) — the normalisation resolve() applies to a transcript naming
  the bare first-party id. A transcript id with a tracked vendor prefix
  whose remainder is a tracked bare form folds to that bare form and
  resolves through its provider row; an untracked normalized remainder
  stays unchanged and follows the existing fallback (Claude-family tier
  when matched, otherwise default), never a prefixed vendor row.
- **An already-tracked table entry is never re-added, and its `id` and
  `vendor_host` are never rewritten.** Those fields stand byte for byte.
  The listing of a tracked key is still fetched and selected for its meter
  membership and factor fold; its model-wide `context_length` is refreshed
  when all vendor-prefix endpoints agree. When they disagree or the model
  has no vendor-prefix endpoint, the pass records a NOTICE + skip and the
  tracked entry stands untouched. Entries with no vendor source (a
  models-table key such as bonsai-2-27b, or a tracked entry without
  vendor_host — aliases, the delisted) stand untouched, and a quiet source
  writes nothing.
- **The long-context band folds to the meter at the band's own threshold
  and multipliers.** A `min_prompt_tokens` override yields its input/output
  factors from the band's fresh input and output prices (issue #878). Every
  cache read or cache write price explicitly listed in that band must also
  equal its base component multiplied by the inferred input factor, rounded
  to ten decimal places; an inconsistent component refuses the listing with
  its base price, band price, and implied factor. A representable band
  contributes NO rates:
  the band's own rates never enter anything the pass writes. Its threshold
  and factors land in the threshold group's `long_context_meters.models`
  list; a key is a bare string when both factors equal the global defaults,
  otherwise it is a one-key multiplier object. Membership, threshold, and
  factors follow the listing for vendor-tracked keys (a member requires its
  tracked entry, which the same run adds); non-vendor keys stand untouched.
  A threshold move moves the key between groups, creating the destination
  group and deleting the source group if it becomes empty. A factor move
  updates the model entry, and a band that disappears removes the key and
  deletes any group it empties.
- **An unmodelled vendor shape refuses the run.** The vendor pass refuses
  a first-party listing it cannot represent, leaving that model untouched.
  This includes scheduled meter bands, unknown override kinds or nonzero pricing
  keys, invalid web-search rates, multiple bands, bands that do not restate input
  and output, invalid thresholds, cache components that one input factor
  cannot represent, and bands mixed with UTC fields. The two
  notices remain a catalog the pass cannot read and a model with no
  first-party endpoint. Multi-price ambiguity without a pin, a stale or
  malformed pin, and broken or unrecognised fetches also refuse.
- **A web-search price is a provider rate.** A first-party listing's
  `web_search` value participates in endpoint selection and is validated
  as USD per search. The vendor pass keeps the selected `vendor_host` in
  `openrouter.models`; the provider pass writes the search rate to that
  `(key, vendor_host)` history, whose effective rates are `fresh`,
  `create_5m`, `create_1h`, `read`, and `output`. Either cache-write field
  is omitted when it equals `fresh`, and an absent field defaults to
  `fresh` (SV-RATE-DATA). Discount notes stay on provider entries.
- **A weekly schedule is ordinary listing state.** The vendor pass selects
  the first-party endpoint by its usual tags and pins; the provider pass
  stores its schedule losslessly in provider history and appends schedule
  changes as moves. A schedule combined with a `min_prompt_tokens` band
  refuses: a window price cannot establish the meter's factors. A band
  mixed with UTC fields on one override also refuses.
- **The loaders run on the would-be file** before anything is written; a
  failure writes nothing.

## The reprice pass recomputes stored prices in place (SV-REPRICE)

`backend/ingest_reprice.py` recomputes rate-derived STORED state for
records whose `pricing_version` differs from `constants.PRICING_VERSION`
(NULL is stale), from stored columns only — the same
`pricing.compute_cost` the parser runs, over each row's own tokens,
`web_search_requests`, and `ts` — so a rate change never refetches R2.
Each long-context row stores the input/output factor pair beside its cost.
The cost, flag, request-fee cleanup, and factor pair move together in one
batch write. Rows update in batched transactions. Each batch recomputes
its rows, writes rows whose cost or
flag moved in one set-based UPDATE, and re-stamps the rest with the
current version in one set-based UPDATE — a restamp advances the
staleness marker only, so the pass's count, and every gate that reads
it (full-rebuild promotion, response-cache invalidation, ingest_done),
tracks rows whose rate-derived data actually changed. A stored version
that parses as an int NEWER than the binary's is skipped, never
overwritten. It runs in `_rebuild_derived_state` between suppression
and the canonical pass; a reparse stamps the current version, so fresh
rows never reprice. If it changes any row (a cost or a flag moved — a
restamp changes none), the run takes a full derived rebuild, since
repricing can move rollups outside the dirty files. No endpoint, panel
or rollup reads `pricing_version`. The recomputed state is `cost_usd`
(token cost plus `web_search_requests * web_search`, SV-RATE-DATA), the
legacy `request_fee_usd` column set to NULL, and the long-context flag
(`records.long_context` — membership-keyed since issue #765: a MEMBER
row re-derives whatever its stored flag, so a refresh fold move
converting the pre-fold NULL rows lands exactly what a reparse stores;
a NON-MEMBER row keeps its stored flag — except a stored TRUE, which
only the Codex path's threshold test can have written, re-derived so a
lapsed member's TRUE unbills (issue #833)). The search count is reused
from the stored record column. The completion marker follows
SV-SCHEMA-AUTOAPPLY.

Pair-qualified staleness: before the keyset loop, the pass classifies
the stale `(model, provider)` pairs with one DISTINCT scan and
SQL-restamps, in one set-based UPDATE, every stale row whose stored
`rate_fingerprint` equals its pair's CURRENT fingerprint — the fp
covers every rate input `pricing.resolve()` consults (all three
resolution branches — provider row, models table, tracked vendor row —
with their windows, schedules, start, tier, default and free shape)
plus the pricing modules' source (`backend/rate_fingerprint.py`), so an
edited entry, a correction, a schedule change or a logic change all
move it while an untouched pair's stands still; the recomputation for
matching rows is the identity by construction and reads zero rows into
Python. A meter multiplier change moves the fingerprint, so those rows
take the reprice path and an interrupted run can leave mixed factor pairs
without collapsing their read-time buckets. The SQL restamp set is exactly `{1-9-digit plain-digit
versions <= V}`, a subset of the keyset path's (Python's `int()`
parses spellings the SQL cast refuses), and the guard still protects
newer-version rows on both paths. NULL fp is the conservative stale
shape (pre-feature rows, older binaries): one recompute, then clean.
An out-of-band mutation of cost-relevant columns sets
`rate_fingerprint` NULL alongside `DELETE FROM ingest_derived_state`.
A PRICING_VERSION bump that moved no pair's data restamps only.

## Rates may be keyed by serving host (SV-PROVIDER-RATES)

An OpenRouter record names its serving host in `message.provider`,
stored as `records.provider`. `pricing.resolve(model, ts, provider)`
uses `PROVIDER_RATES[(normalised model, provider)]` when that row
exists (a dated permaslug such as `-20260731` folds to its `-0731`
slug), else the model alone. A record with NO provider (every other
lane) always prices by the model alone: z.ai's GLM must never take an
OpenRouter host's rate. Free ids (`:free`, `stealth/`) stay zero ahead
of both.

Provider rows follow SV-DATED-RATES: windows in `PROVIDER_DATED_RATES`,
boundaries joining `RATE_EPOCHS`, and every re-deriving fold groups by
provider as well as epoch. Both sides read them from `src/pricing.json`
(`window.providerRates` in the browser).

## Model resolution flags estimates (SV-RATE-ESTIMATES)

`pricing.resolve()` returns `kind`: `exact` | `tier` | `default`.

- Ids are normalised first: provider/region prefixes stripped
  (`us.anthropic.`, `anthropic/`) and `.` → `-`, so `claude-opus-4.8`
  resolves like `claude-opus-4-8`.
- EXACT allows only a dated-snapshot (`-20250514`) or bracket (`[1m]`)
  suffix after a key. A short version suffix must NOT match a shorter
  key (billing `claude-opus-4-9` at a shorter key's retired rate is a
  silent 3x overcount).
- Unmatched Claude models fall back to their family's
  current-generation LIST rates as `tier`; anything else is `default`.
  Non-exact results surface as `estimated_rate` in the API, so a guess
  is never presented as fact.

Never invent a rate for an unpriced variant (e.g. `-fast`): let it fall
back and be flagged.
