# command-stats

Skills for working with Redis Enterprise debuginfo support packages.

## redis-commandstats-report

Builds a per-database report of `INFO commandstats`, and screens package pairs
for counter resets before any before/after comparison is attempted.

The reason this is a skill rather than a snippet of tar-and-awk is that the data
has traps which change the answer, not just the presentation:

- Counters are cumulative since each shard last started, and shards do not
  restart together. A single capture routinely holds shards covering 8 hours
  beside shards covering 6 days, so summing raw counters adds up windows of
  different lengths. Everything is divided by each shard's own uptime.
- Shards belong to databases. Filtering by role alone pulls in another
  database's shards, so the database must be named.
- Replicas execute the replication stream and count it, so including both roles
  double-counts every write. Masters only by default.
- Percentiles are not additive, so `INFO Latencystats` p99s are reported as a
  spread across shards rather than a fabricated aggregate.
- Counters reset on restart, so cross-capture subtraction can produce negative
  "growth" that reads as a finding. `run_id` per shard settles whether a pair
  can be differenced at all.

### Output

```bash
python3 scripts/extract.py --package debuginfo.XXX.tar.gz --list
python3 scripts/extract.py --package debuginfo.XXX.tar.gz \
    --database DBNAME --html /tmp/report.html
```

One self-contained HTML document — inline SVG figures, no CDN, no webfonts, no
network — designed to be handed to Cowork's `create_artifact` and read inside
Claude, but equally openable as a file. Tables sort on a header click and each
exports its own CSV. It prints and exports to PDF.

**Standard library only.** No `pandas`, `numpy` or `matplotlib`; Python 3.7+.
Verified by running under `python3 -I -S`, where those imports are unavailable,
and by classifying every module loaded at runtime.

### Tests

```bash
./tests/run.sh
```

Real support packages cannot be committed, so `tests/make_fixture.py` fabricates
one containing every condition the report claims to detect — two uptime cohorts,
writes only in the long cohort, one hot shard, admin commands dominating CPU, a
p99 above the mean, errorstats that reconcile to the unit. `run.sh` asserts each
of those is found, checks the report is free of external references, and runs the
report's own JavaScript under a minimal DOM shim to catch bad numbers reaching
the figures. It writes to a temp directory and leaves the tree clean.

See `skills/redis-commandstats-report/references/data_model.md` for the package
layout, the rladmin tables, and the full list of failure modes.
