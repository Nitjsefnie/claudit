// The provider-neutral cache-write split, the browser mirror
// of backend/parse_common.resolve_cache_writes (SV-PARSER-SPEC). A
// request's input splits into three neutral categories on every lane:
// read from cache (cache_read_input_tokens), written to cache
// (cache_write), and the uncached tail. An automatic-caching lane keeps
// its implicit writes inside fresh (they bill at the fresh rate), so the
// neutral count is a separate, NON-priced value the parsers attach as
// `cache_write` on each assistant_usage meta entry.
// An explicit-caching lane reports its write (cache_creation > 0), trusted
// verbatim; otherwise the write is the NEXT record's cache-read growth,
// capped by this record's fresh input; a file's last record has no
// successor and counts 0. Applied over the assistant_usage entries in file
// order, BEFORE any cross-file dedup (the backend derives per file).
// Idempotent: it reads only fresh / cache_creation / cache_read.
window.resolveCacheWrites = function resolveCacheWrites(meta) {
  const recs = meta.filter(m => m && m.type === 'assistant_usage');
  for (let i = 0; i < recs.length; i++) {
    const u = recs[i].usage || {};
    const create = u.cache_creation_input_tokens || 0;
    if (create > 0) {
      recs[i].cache_write = create;
      continue;
    }
    const nxt = i + 1 < recs.length ? recs[i + 1].usage || {} : null;
    if (nxt === null) {
      recs[i].cache_write = 0;
    } else {
      const growth = Math.max(0,
        (nxt.cache_read_input_tokens || 0) - (u.cache_read_input_tokens || 0));
      recs[i].cache_write = Math.min(u.input_tokens || 0, growth);
    }
  }
};
