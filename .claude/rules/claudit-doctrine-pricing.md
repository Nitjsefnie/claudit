---
paths:
  - "**/src/pricing.json"
  - "**/src/pricing_notices.json"
  - "**/src/rates.js"
  - "**/backend/pricing.py"
  - "**/backend/pricing_load.py"
  - "**/backend/rate_fingerprint.py"
  - "**/backend/rate_boundaries.py"
  - "**/backend/ingest_reprice.py"
  - "**/backend/long_context.py"
  - "**/scripts/ci/refresh_*.py"
  - "**/scripts/ci/backfill_provider_rates.py"
  - "**/scripts/ci/collapse_oscillating_rates.py"
  - "**/scripts/ci/price_band.py"
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
entry `openrouter.models[key]` carries `id` and — when the key prices a
first-party vendor model — `vendor_host`: the host whose provider row
`providers[key][vendor_host]` holds that model's history, which the
vendor's BARE first-party id resolves through. A bare id prices from
that row's dated windows, including its web-search rate, and without its
provider-specific schedule. The host's schedule applies only to requests
THROUGH it. A transcript id
spelled with a tracked vendor prefix whose remainder is a tracked bare
form folds to that form and resolves through the bare rows. A normalized
prefixed id whose remainder is not a tracked bare form stays unchanged and
follows the existing fallback: a Claude-family match gets its tier rate;
otherwise it gets the default, and no prefixed vendor row is selected. The
bare forms are a validated namespace: one bare form per tracked vendor
entry, and never one that is also a models-table key — a transcript id
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

Each row's history is append-only, oldest first. Its effective vector is
five finite non-negative token rates (`fresh`, `create_5m`, `create_1h`,
`read`, `output`). `fresh`, `read`, and `output` are always present;
`create_5m` and `create_1h` are present only when each differs from
`fresh`, and each absent cache-write rate defaults to `fresh`. An entry
also carries an optional string `note`, an optional `schedule`, and — on
a provider entry only — an optional `band`. Every schedule window has the
same five effective rates and the same absent-means-`fresh` rule. A band
maps a subset of the five rate fields to `[min, max]` pairs of finite
non-negative numbers with `min <= max`; either cache-write pair may be
omitted when it equals the band's `fresh` pair, and then defaults to that
pair. A `band` is a record of what the host moved inside, never a price:
the five effective rate fields beside it stay the priced rates, and both
loaders check and fold its shape without using it for pricing
(SV-RATE-REFRESH). Nothing writes a
`band` beside a `schedule`, and the loaders do not refuse one if it
appears: the band is read for its shape only, and the schedule still
decides which hours price what. The leading entry's `from` defaults to
`null` when absent. A model row therefore covers all time. A provider
row's first may name its start instant; before it, that host's records
price by the model alone, and the start joins `RATE_EPOCHS`. Every later
`from` is exactly
`YYYY-MM-DDTHH:MM:SS` plus `Z` or `±HH:MM`, every field in range (a
real calendar day, hour 0-23, minute/second 0-59, offset under 24:00),
strictly after its predecessor. The newest entry is the list price;
each earlier one applies until its successor's `from` (SV-DATED-RATES).

A price change APPENDS `{"from": T, ...}`. An entry is never edited or
removed, except to correct a misstated price or instant (a wrong seeded
price; sampled entries dated at detection where OpenRouter's log has
the real change points — the SV-RATE-REFRESH backfill), and only in a
human commit that bumps `PRICING_VERSION`, which reprices from stored
columns with no reparse. Both loaders refuse a rule-breaking file,
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

The file keeps the `json.dumps(doc, indent=2, sort_keys=True)` layout,
so any writer reproduces it and a one-rate change is a one-line diff.
File order never decides key matching — the LONGEST matching key wins;
order only breaks a family-fallback tie between equal versions (first
key wins).

## Provider rates refresh from OpenRouter (SV-RATE-REFRESH)

`.github/workflows/refresh-pricing.yml` runs
`scripts/ci/refresh_provider_rates.py` hourly (identically on dispatch)
and commits to `master` as `github-actions[bot]` only after the full
suite passes on the new data. A hand edit to a provider row follows the
same rules:

- A moved price is an APPENDED entry, never an edit or deletion. A
  log-backed row dates it at OpenRouter's recorded change point; a
  sampled row at detection time. A first-seen sampled host gets a row
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
  price is 0. A listed `input_cache_write_1h` is the `create_1h` rate,
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
    data-region filter or tag pin selects exactly ONE endpoint. The
    row's token history is that endpoint's series. `web_search` is not
    in the log and does not block this join. Anything else is
    ambiguous, and the host is sampled, never guessed.
  - **Separate web-search sampling.** Every log-backed token entry carries
    forward the previous `web_search` rate. If the listing's search rate
    moved, append an entry at detection time with the predecessor's token
    rates and the newly sampled search rate. A search-only move creates a
    resolve epoch without changing token prices.
  - **Change points to entries.** A series' state at an instant is each
    field's newest value at or before it, and exists once `input` and
    `output` both have a point. Each instant at which the five rates
    change — normalised like the listing: cache read null/absent is 0,
    cache write null/absent/0 is the input rate, values rounded to 10
    decimal places — is one entry, `from` truncated to whole seconds
    (points in one second collapse to its last state). A discount-only
    change is not an entry; `note` is the discount in force at `from`.
    A `null` `input` or `output` is not a price: the host is sampled.
  - **Hourly append.** For a log-backed host with a row, every entry
    whose `from` is after the row's newest `from` and whose rates
    differ from its predecessor is appended, oldest first, so a flip
    between runs is recorded as its two moves. After those states are
    accounted for, the resulting row's token rates at fetch time must
    match its selected endpoint's listed price, or that price must lie
    inside the row's band. Otherwise the listed state enters the same
    log-path append machinery at detection time, under the existing
    band-formation and escape rules; the changed host is named with the
    reason that its row was out of step with its endpoint's listed price.
    Its `web_search` follows the separate sampling rule. An in-step row
    with no token or search move stays silent and writes nothing. A first-seen host
    imports from the history floor as above. The hourly run never rewrites
    an entry.
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
- **The one-time backfill.**
  `scripts/ci/backfill_provider_rates.py --as-of <instant>` replaces
  every log-backed row's history with the log's entries up to
  `--as-of`, keeping the row's start: when the old first `from` (`null`
  included) precedes the log's first change point, the new first entry
  takes it. Unbacked rows are left untouched and listed. It is an
  SV-RATE-DATA correction, landing only in a human-reviewed commit that
  bumps `PRICING_VERSION` and names the invocation and each row's added
  entries.
- **Alternating prices are reported, not appended — sampled rows
  only.** On a sampled row, when neither the listing nor the newest
  entry has a schedule and the listed rates equal a non-newest entry
  whose `from` is within 7 days of the detection time (never wall
  clock), the run reports an "alternating price" notice naming host and
  entry, and appends and bumps nothing. A first move away never
  matches; the flip back does. A genuine return to an older price is
  hand-appended, and the next run compares against it.
- **An oscillating host is recorded once, as a BAND (issues #640, #664).**
  A provider entry carries an optional `band`: each rate field it names
  maps to a `[min, max]` pair of FINITE non-negative numbers with
  `min <= max`, the range the host actually moved inside; a field it does
  not name is unconstrained, and a bound that is not finite is refused,
  because `x <= inf` is true of everything and the row would go
  permanently silent. The five rate fields beside a band stay the PRICED
  rates — the time-weighted mean over the window that formed the band, the
  classifier's trailing `days`: the level in force at its open — the last
  entry dated before it, or the row's leading undated entry when nothing
  dated precedes the window, which is the classifier's baseline too (issue
  #836) — plus every entry in it, the mean weighting
  that window so a level predating it weighs only from the window's open
  (issues #663, #665) — so no rate
  math reads the band; the
  loaders accept and check it (`pricing_load.check_band`, mirrored by
  parser.js) and ignore it. Nothing writes a `band` beside a `schedule`,
  and the loaders do not refuse one if it appears: the band is read for
  its shape only, and the schedule still decides which hours price what.
  The hourly run forms a row's band itself, on the log path (issue #664):
  an unbanded row whose window classifies TOGGLE or BAND appends its new
  states plus ONE band entry dated at the detection instant — every
  earlier level stays an actual entry, so a record priced before the
  detection instant prices at the level that held there. A banded row the
  run later SAMPLES falls back to appending every move: the alternation
  rule and the band are independent, and only the log path carries the
  band. `collapse_oscillating_rates.py` remains the one-time,
  human-reviewed whole-history pass: it collapses each row
  `price_band.classify` calls TOGGLE or BAND to the ONE band entry its
  window leaves, leaves STEP and STABLE rows byte-identical, and reports
  the distinct host names it collapsed so the reviewer can run the
  records query over exactly those before merging (a mean is only safe
  while no record has been priced through that host).
- **An escape re-forms the band or follows the step (issue #665).** On the
  LOG path, a row whose newest entry carries a band is appended to at
  most once per run: every new state inside the band makes NO Move at all
  (a Move with no entries in it would still bump the version, write both
  files and commit), and the states outside it append ONE entry. The run
  classifies the window with the new states folded in. Still oscillating,
  the entry RE-FORMS the band — the range the window's levels span,
  priced by their time-weighted mean, dated at the last escape's change
  point, carrying the banded entry's note and schedule. The window no
  longer oscillating (STEP or STABLE), the entry appends the state now in
  force instead, dated at its own change point, so the row follows the
  price.
  The mean's window ends at the detection instant, where the level in
  force holds until it, so a re-formed row tracks the price it names.
  The entry is APPENDED, so an undated banded entry keeps its
  `from: None` and the row keeps covering every record: only a row whose
  FIRST entry names an instant stops existing before it.
- **Unmodelled pricing refuses the host:** an override kind the script
  does not model, or any other pricing key at a nonzero price. `web_search`
  is a PRICED rate, validated as finite non-negative USD per search and
  stored in the provider entry, not in its note. Two endpoints differing
  only in that rate remain different listings. Its absence from the token
  price log leaves token history log-backed while search changes are
  sampled at detection.
- A run that appends bumps `PRICING_VERSION` to one past the value in
  `backend/constants.py` — never a literal — in the same commit
  (records at or after a new `from` ingested before the deploy were
  priced at the old rate), and moves `provider_rates_fetched`. A run
  that appends nothing writes nothing. A new entry adds a rate epoch
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
  nothing, so the baseline can only under-suppress. A run that appends
  nothing publishes no commit, so it does not move the baseline.
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
  only itself: every other move is appended, tested and committed with the
  bump, then the run exits nonzero, naming each refused host. A log-backed
  row out of step with its selected endpoint's listed price is also
  REFUSED when detection is not after its newest `from`. At an equal
  instant, the host is refused by name, its row stays untouched and other
  hosts' moves can proceed. A retained newest `from` after detection has
  a pre-existing limitation: if another provider move advances
  `provider_rates_fetched` to detection, final validation rejects the
  retained future entry. The whole run then exits nonzero and writes
  nothing, before rendering the normal named-refusal report. An ordinary
  move blocked by timing keeps the existing untouched-with-notice
  behaviour, subject to the same final validation.

- Four automatic rules resolve the mechanical multi-price shapes with no
  human decision, beneath an explicit resolution (which keeps precedence,
  refusals included): a bare namespace endpoint beside its service tiers
  takes the bare one; the one quantization endpoint beside throughput
  tiers takes that one; the configured data region takes its own endpoint
  (the region filter, and under `global` the namespace's global endpoint —
  an explicit `<ns>/global`, else the bare `<ns>` — over a region spelling
  the vocabulary does not carry); and one quantization's price twins take
  the cheaper, ordered by cache read, then input, then output — the twins
  are taken to be one offering priced per region, so the rule compares
  prices and not the identity fields a recorded `cheapest` pin compares
  (that pin's `ignore` exists for a listing artifact, which is why the
  hosts whose twins differ in a limit keep their pin). The rules normally
  fire where no resolve entry exists. After a tag-only pin narrows to
  multiple prices of one known quantization, rule 4 instead takes the
  cheapest within that tag, naming the pin and rule in its report. Single
  endpoint pins and other explicit resolutions keep precedence; every
  other ambiguous tag-only pin refuses. Twin-switch rises are reported,
  and ties or flips refuse. The report records every automatic choice as
  rule-resolved. A multi-price
  shape none of them fits still refuses, as does a shape whose tags name
  two quantizations, two prices under one tag, or a region spelling with
  no global endpoint to prefer. The rules live in
  `scripts/ci/refresh_selection_rules.py`, and none of them extends to the
  price log's own endpoint selection: a host the log would otherwise back
  needs its base pinned to stay log-backed.
- The fix is a human decision recorded in
  `openrouter.models.<model>.resolve.<host>`, with a `why`:
  - `{"tag": ...}` narrows to that exact tag, whatever its region. If it
    leaves one endpoint, the pin keeps precedence. If it leaves multiple
    price groups sharing one known quantization suffix, rule 4 takes the
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
- **An already-tracked table entry is never re-added or rewritten.** The
  listing of a tracked key is still fetched and selected for its meter
  membership and factor fold, but the tracked table entry stands byte for
  byte. Entries with no vendor source (a models-table key such as
  bonsai-2-27b, or a tracked entry without vendor_host — aliases, the
  delisted) stand untouched, and a quiet source writes nothing.
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
  This includes weekly schedules, unknown override kinds or nonzero pricing
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
- **A weekly schedule refuses the vendor pass:** the price the pass
  compared is a window price at fetch time, the same reason the provider
  pass refuses a first-seen scheduled host inside one; the model is not
  tracked until the pass can select an actual default.
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
