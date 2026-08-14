#!/usr/bin/env python3
"""Decide whether two support packages can legitimately be differenced.

`INFO commandstats` counters reset when a shard's process restarts, so a
before/after subtraction is only meaningful for shards that ran continuously
across the interval. `run_id` settles that definitively -- it is regenerated on
every start -- and `uptime_in_seconds` says how far back a shard's counters
actually reach.

Inferring resets from decreasing counters is much weaker: a shard can restart
and still show higher counts for some commands, so the arithmetic check both
misses real resets and flags things that are not resets.
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import parse  # noqa: E402


def _meta(path):
    out = {}
    with open(path, encoding='utf-8', errors='replace') as fh:
        for line in fh:
            m = re.match(r'^(run_id|uptime_in_seconds|process_id):(.+)$', line.strip())
            if m:
                out[m.group(1)] = m.group(2).strip()
            if len(out) == 3:
                break
    return out


def _side(package, database, role):
    b = parse.open_bundle(package, tempfile.mkdtemp(prefix='debuginfo-'))
    db_id, db_name = parse.resolve_database(b, database)
    files, in_db, want = parse.select_shards(b, db_id, role)
    node_of = {int(r.shard): int(r.node) for r in in_db.itertuples()}
    role_of = {int(r.shard): r.role for r in in_db.itertuples()}
    return dict(
        label=os.path.basename(package), when=b.generated_at, db_id=db_id, db_name=db_name,
        shards={sid: dict(_meta(p), node=node_of[sid], role=role_of[sid])
                for sid, p in files.items()})


def main(argv=None):
    ap = argparse.ArgumentParser(
        description='Check whether two support packages form a differenceable pair.')
    ap.add_argument('--before', required=True)
    ap.add_argument('--after', required=True)
    ap.add_argument('--database', required=True)
    ap.add_argument('--role', default='all', choices=['master', 'slave', 'all'])
    a = ap.parse_args(argv)

    A, B = _side(a.before, a.database, a.role), _side(a.after, a.database, a.role)
    try:
        gap = (dt.datetime.fromisoformat(B['when']) - dt.datetime.fromisoformat(A['when'])).total_seconds()
    except ValueError:
        gap = float('nan')

    print(f"before : {A['label']}  {A['when']}  ({len(A['shards'])} shards)")
    print(f"after  : {B['label']}  {B['when']}  ({len(B['shards'])} shards)")
    print(f"database: {B['db_name']} (db:{B['db_id']})")
    if gap == gap:
        print(f"interval: {gap:,.0f}s ({gap/86400:.2f} days)\n")

    both = sorted(set(A['shards']) & set(B['shards']))
    only_a = sorted(set(A['shards']) - set(B['shards']))
    only_b = sorted(set(B['shards']) - set(A['shards']))
    same = [s for s in both if A['shards'][s].get('run_id') == B['shards'][s].get('run_id')]
    restarted = [s for s in both if s not in same]

    print(f"{'shard':>7} {'nodeA':>6} {'nodeB':>6} {'run_id':>9} {'uptimeA':>10} {'uptimeB':>10} {'reaches back':>13}")
    for s in both:
        ua, ub = float(A['shards'][s]['uptime_in_seconds']), float(B['shards'][s]['uptime_in_seconds'])
        ok = A['shards'][s].get('run_id') == B['shards'][s].get('run_id')
        reach = 'yes' if (gap == gap and ub > gap) else 'no'
        print(f"{s:>7} {A['shards'][s]['node']:>6} {B['shards'][s]['node']:>6} "
              f"{'same' if ok else 'CHANGED':>9} {ua:>10,.0f} {ub:>10,.0f} {reach:>13}")

    print(f"\nshards present in both      : {len(both)}")
    print(f"  ran continuously (run_id)  : {len(same)}")
    print(f"  restarted (counters reset)  : {len(restarted)}")
    if only_a or only_b:
        print(f"  only in before / only in after: {only_a} / {only_b}")

    if B['shards']:
        min_up = min(float(v['uptime_in_seconds']) for v in B['shards'].values())
        print(f"\nmax usable look-back from the later capture: {min_up:,.0f}s "
              f"({min_up/3600:,.1f}h) -- the minimum shard uptime.")
        if gap == gap:
            print(f"the interval between these captures is    : {gap:,.0f}s ({gap/3600:,.1f}h)")

    print()
    if not both:
        print('VERDICT: not comparable -- no shard appears in both captures.')
        return 1
    if len(same) == len(both):
        print(f'VERDICT: comparable. All {len(both)} shards ran continuously across the '
              f'interval, so deltas are meaningful.')
        return 0
    if same:
        print(f'VERDICT: partially comparable. {len(same)} of {len(both)} shards ran '
              f'continuously; deltas are valid for those only ({same}). The other '
              f'{len(restarted)} reset and must be excluded, which makes any database-wide '
              f'total incomplete.')
        return 2
    print(f'VERDICT: not comparable. All {len(both)} shards restarted between captures, so '
          f'every counter reset and no difference is meaningful. Use two single-snapshot '
          f'reports instead -- share-based figures stay comparable because they are '
          f'uptime-invariant. For a valid pair, capture again with a gap shorter than the '
          f'minimum shard uptime above.')
    return 3


if __name__ == '__main__':
    sys.exit(main())
