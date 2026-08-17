# What's inside a Redis Enterprise debuginfo package

Read this when the data doesn't look like the script expects, when you need a
field the report doesn't surface, or when a package is laid out unusually.

## Contents

1. [Package layout](#package-layout)
2. [The rladmin tables](#the-rladmin-tables)
3. [Shard files: which INFO sections matter](#shard-files-which-info-sections-matter)
4. [Fields worth reading beyond commandstats](#fields-worth-reading-beyond-commandstats)
5. [Failure modes](#failure-modes)
6. [Extending the report](#extending-the-report)

## Package layout

A `debuginfo.<HEX>.tar.gz` is a flat set of directories, one per cluster node
and one per database:

```
node_26/node_26.rladmin      cluster state as seen from this node
node_26/redis_63.txt         INFO output for shard 63, which lives on node 26
node_26/redis_71.txt
node_27/node_27.rladmin      same cluster state, captured a fraction later
...
database_4/database_4.rladmin   per-database config dump
```

Two things catch people out:

**Every node's rladmin holds the whole cluster's tables**, not just that node's
shards, so any one of them is enough to map shards to databases. They're captured
a few hundred milliseconds apart; taking the earliest line-2 timestamp as the
capture time is arbitrary but consistent.

**Shard files live under the node that hosted the shard at capture time.** Shards
move, so `redis_63.txt` can be under `node_26` in one package and `node_31` in
the next. Never assume a path — find the file by its `redis_<n>.txt` name.

The `database_<id>.rladmin` file is a config dump with no shard list and no
timestamp on line 2. It's the wrong place to read database facts from; use the
`DATABASES` table in a node rladmin, which is captured in the same pass as the
shard list.

## The rladmin tables

`node_*.rladmin` opens with a title line and an ISO timestamp:

```
Redis Enterprise Node Information
2026-08-13 15:17:27.754847+00:00
```

Then a series of blocks, each introduced by an ALL-CAPS heading ending in a
colon, with a header row of column names. The two that matter:

```
DATABASES:
DB:ID NAME      TYPE        STATUS SHARDS MEMORY_SIZE PLACEMENT REPLICATION PERSISTENCE ENDPOINT ...
db:4  pers-3950 redis/flash active 18     900GB       sparse    enabled     disabled    redis-...

SHARDS:
DB:ID NAME      ID        NODE    ROLE   SLOTS     USED_MEMORY USED_RAM USED_FLASH RAM_FRAG ...
db:4  pers-3950 redis:63  node:26 master 0-909     2.82GB      1.95GB   896.24MB   221.47MB ...
```

`SHARDS` is what makes per-database scoping possible: the first two columns give
the database id and name for every shard. `MEMORY_SIZE` in `DATABASES` is the
configured limit and does change between captures — read it from the package
being analysed rather than carrying it over.

Note that `SHARDS` lists both roles, so a database with `SHARDS 18` in
`DATABASES` and replication enabled produces 36 `SHARDS` rows. That is the
expected reconciliation, not a discrepancy.

## Shard files: which INFO sections matter

`redis_<n>.txt` is `INFO` output, `# Section` headings and `key:value` lines.
Sections present in a typical Redis Enterprise 7.4 capture: Server, Clients,
Client-Compression, Memory, Persistence, Bigredis, Bigredis-Driver,
Bigredis-Stats, Stats, Replication, CPU, Modules, Commandstats, Errorstats,
Latencystats, Cluster, Keyspace, Bigredis-Keyspace, Sharding.

**Commandstats** — one line per command, cumulative since the process started:

```
cmdstat_hmget:calls=38,usec=83,usec_per_call=2.18,rejected_calls=0,failed_calls=0
```

Command names contain `|` for container commands (`config|get`, `client|setname`)
and `.` for module commands (`json.set`, `ts.add`), so split on the first colon
rather than assuming a word character class. Recompute `usec_per_call` from
`usec / calls` — the file value is rounded to two decimals, which is lossy on
sub-microsecond commands.

**Latencystats** — real percentiles, per command, per shard:

```
latency_percentiles_usec_hset:p50=2.007,p99=14.015,p99.9=23.039
```

This is the antidote to commandstats' biggest weakness. A mean of 5µs is
consistent with "everything takes 5µs" and with "almost everything takes 1µs and
one call in a hundred takes 400µs", and those want different responses. The
percentiles distinguish them. They cannot be summed or averaged across shards.

**Errorstats** — named error replies:

```
errorstat_LOADING:count=25
```

These reconcile with the commandstats failure columns: error kinds that are
rejections before execution sum to `rejected_calls`, and errors during execution
to `failed_calls`. Because the two sections are populated by different code paths
in Redis, agreement between them is a genuine check that the parse is sound —
if they match to the unit, no shard file was read twice, skipped, or mis-parsed.

## Fields worth reading beyond commandstats

From `# Server`:

- `uptime_in_seconds` — the denominator that makes counters comparable, and the
  single most important field in the package after the counters themselves.
- `run_id` — regenerated on every process start. The only reliable reset test.
- `process_id` — corroborates `run_id`.

From `# Stats`:

- `total_commands_processed` — an independent cross-check. Dividing it by uptime
  should reproduce the sum of per-command rates almost exactly; it comes from a
  different counter than commandstats, so agreement validates the whole parse.
- `instantaneous_ops_per_sec` — the only current-rate figure available. A large
  gap against the lifetime mean means the workload isn't steady.
- `expired_keys`, `evicted_keys` — eviction pressure. `evicted_keys` at zero on
  a memory-limited database means the limit isn't being hit.
- `total_net_input_bytes` / `total_net_output_bytes` — bandwidth, if asked.

Absent in these captures despite being standard `INFO` fields: `keyspace_hits`
and `keyspace_misses`. Don't promise a hit ratio without checking.

## Failure modes

**Cumulative counters across unequal windows.** The one that silently ruins
analyses. Shards restart independently, so a capture can hold shards covering
8 hours beside shards covering 6 days. Summing raw `calls` adds up different
lengths of time. Always divide by each shard's own uptime.

**Cross-capture subtraction after a restart.** Counters reset to zero on
restart, so a later capture can show smaller numbers than an earlier one. The
resulting negative "growth" looks like a finding and isn't. Check `run_id` per
shard before differencing anything.

**Double-counting writes.** Replicas execute the replication stream and count it
in their own commandstats, so including both roles inflates every write command.
Masters only, unless comparing roles is the actual question.

**Cross-database contamination.** Filtering shard files by role alone pulls in
other databases' shards. Filter by database first.

**Averaging percentiles.** A p99 is not a mean and cannot be combined across
shards. Report the spread instead.

**Uptime cohorts masquerading as key skew.** When a command's rate differs
sharply between shards, there are two very different explanations. If the split
follows the uptime cohorts, it's temporal — the traffic happened in a window only
some shards cover. If it varies *within* a cohort, where shards share a window,
it's genuine key distribution. Confining skew comparisons to one cohort is what
separates the two, and getting this backwards turns "your writes stopped two days
ago" into "shard 7 is hot".

## Extending the report

Everything numeric lives in `scripts/extract.py`, standard library only. It is
importable, so for a one-off question building the model and working with plain
dicts is usually faster than adding a section to the report:

```python
import sys, tempfile
sys.path.insert(0, 'scripts')
import extract

b = extract.open_bundle('debuginfo.XXX.tar.gz', tempfile.mkdtemp())
db_id, db_name = extract.resolve_database(b, 'pers-3950')
files, in_db, want = extract.select_shards(b, db_id, 'master')
M = extract.build(b, db_id, db_name, 'master', files, in_db, want)

M['per_cmd']      # one dict per command: rate, cpu_frac, shares, percentiles, fail_rate
M['per_shard']    # one dict per shard: rate, uptime, cohort, run_id, iops
M['totals']       # database-wide scalars
M['checks']       # the integrity table, as (check, result, detail) triples
```

The pieces worth knowing about:

- `open_bundle` / `resolve_database` / `select_shards` — reading the tar and
  scoping to one database. Only the rladmin files and the shard files for the
  selected database and role are ever extracted.
- `parse_shard_file` — splits one `redis_NN.txt` into commandstats,
  latencystats, errorstats and meta. Add a field to `_META_KEYS` to surface it.
- `build` — everything derived. Nothing downstream recomputes a quantity.
- `ADMIN_COMMANDS` and `WRITE_PREFIXES` classify commands; extend them for
  module-heavy workloads, where the defaults will not know your command names.
- `auto_findings` — the derived prose, and the thresholds that decide which
  findings apply. Callers can replace the wording via `--findings`; they cannot
  replace the thresholds, which is deliberate.

`assets/report_template.html` is presentation only — CSS, a small SVG chart kit,
and one function per figure. Adding a figure means adding a function there and
calling it from `render()`; it never needs to compute anything, because the model
already carries it.

Cohort detection splits on a ratio between consecutive sorted uptimes
(default 1.5×), on the reasoning that shards restarted in the same wave differ by
seconds while separate waves differ by hours. A cluster with genuinely staggered
rolling restarts may need a different threshold — pass `split_ratio` to
`detect_cohorts`.
