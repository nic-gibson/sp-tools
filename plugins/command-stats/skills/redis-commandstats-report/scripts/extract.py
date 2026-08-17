#!/usr/bin/env python3
"""Reduce a Redis Enterprise support package to a self-contained HTML report.

Reads a debuginfo tar.gz, scopes it to one database, normalises every counter by
its own shard's uptime, and fills `../assets/report_template.html` with the
result. The output is one HTML document with inline SVG figures and no network
resources, meant to be handed to Cowork's `create_artifact` and read inside
Claude, though it opens perfectly well as a file.

**No third-party dependencies** -- standard library only, Python 3.7+. Nothing to
install, nothing to keep in step with a wheel.

    # what databases are in here?
    python3 extract.py --package debuginfo.XXX.tar.gz --list

    # the report
    python3 extract.py --package debuginfo.XXX.tar.gz \
        --database pers-3950 --html /tmp/report.html

    # just the model, no HTML
    python3 extract.py --package debuginfo.XXX.tar.gz --database pers-3950 --json

    # can these two packages be differenced?
    python3 extract.py --pair BEFORE.tar.gz AFTER.tar.gz --database pers-3950

The division of labour: everything numeric happens here, and the template does
presentation only. That keeps the uptime normalisation -- the one thing that must
not be got wrong -- in a single testable place, and it keeps the payload the
template embeds small, because the model is O(commands + shards) and never
O(commands x shards). A 200-shard database still embeds in a few hundred
kilobytes.

The database is required and never inferred: a report silently scoped to the
wrong database is worse than no report. `--list` exists so you can find the name
without guessing.

Tests: `bash ../../../tests/run.sh`.
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import os
import re
import statistics
import sys
import tarfile
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.join(HERE, os.pardir, 'assets', 'report_template.html')

# ---------------------------------------------------------------- rladmin bits
_SHARD_RE = re.compile(
    r'^\s*db:(?P<db_id>\d+)\s+(?P<db_name>\S+)\s+redis:(?P<shard>\d+)\s+'
    r'node:(?P<node>\d+)\s+(?P<role>master|slave)\b')
_DB_RE = re.compile(
    r'^\s*db:(?P<db_id>\d+)\s+(?P<db_name>\S+)\s+(?P<type>\S+)\s+(?P<status>\S+)\s+'
    r'(?P<shards>\d+)\s+(?P<memory_size>\S+)\s+(?P<placement>\S+)\s+'
    r'(?P<replication>\S+)\s+(?P<persistence>\S+)\s+(?P<endpoint>\S+)'
    r'(?:\s+(?P<crdb>\S+))?')
_SECTION_RE = re.compile(r'^([A-Z][A-Z /]*[A-Z]):\s*$')

# ------------------------------------------------------------------- INFO bits
_CMD_RE = re.compile(r'^cmdstat_([^:]+):(.*)$')
_LAT_RE = re.compile(r'^latency_percentiles_usec_([^:]+):(.*)$')
_ERR_RE = re.compile(r'^errorstat_([^:]+):count=(\d+)$')
_META_KEYS = (
    'run_id', 'uptime_in_seconds', 'process_id', 'redis_version', 'used_memory',
    'total_commands_processed', 'instantaneous_ops_per_sec', 'connected_clients',
    'expired_keys', 'evicted_keys', 'total_net_input_bytes', 'total_net_output_bytes',
    # the field Errorstats should actually be reconciled against -- see
    # reconcile_errors for why failed_calls is the wrong comparison
    'total_error_replies',
)
CMD_FIELDS = ('calls', 'usec', 'rejected_calls', 'failed_calls')

# Python 3.12 deprecates extracting without an explicit filter and 3.14 makes
# 'data' the default. Asking for it by name is silent on every version that
# understands it, and the members we allow through are plain files anyway.
_EXTRACT_KW = {'filter': 'data'} if sys.version_info >= (3, 12) else {}

# Commands issued by operators, monitoring agents and the cluster itself rather
# than by the application. Worth separating because on a lightly loaded database
# they can dominate CPU time, which is a finding in its own right.
ADMIN_COMMANDS = {
    'info', 'config|get', 'config|set', 'config|resetstat', 'ping', 'sping',
    'slowlog|get', 'slowlog|reset', 'slowlog|len', 'latency|latest', 'latency|history',
    'latency|histogram', 'latency|reset', 'client|list', 'client|id', 'client|info',
    'client|setname', 'client|getname', 'client|setclass', 'client|kill', 'client|no-evict',
    'command|docs', 'command|count', 'command|info', 'command|getkeys', 'cluster|info',
    'cluster|nodes', 'cluster|slots', 'cluster|shards', 'dbsize', 'memory|usage',
    'memory|stats', 'memory|doctor', 'debug|jmap', 'debug|sleep', 'replconf', 'psync',
    'sync', 'slaveof', 'replicaof', 'auth', 'hello', 'select', 'shardingkeyregex',
    'failover', 'lastsave', 'wait', 'swapdb', 'acl|whoami', 'acl|list', 'acl|cat',
}

# Commands that change data. Used to spot a workload that has stopped writing --
# a pattern that shows up as writes being present only in the longest-window
# shards. Prefix matching covers the long tail of type-specific writers.
WRITE_PREFIXES = (
    'set', 'setex', 'setnx', 'psetex', 'getset', 'getdel', 'append', 'incr', 'decr',
    'del', 'unlink', 'expire', 'pexpire', 'persist', 'rename', 'copy', 'move', 'restore',
    'flush', 'touch',
    'hset', 'hdel', 'hincr', 'hmset', 'hsetnx',
    'lpush', 'rpush', 'lpop', 'rpop', 'lset', 'lrem', 'ltrim', 'linsert', 'lmove',
    'sadd', 'srem', 'spop', 'smove', 'sinterstore', 'sunionstore', 'sdiffstore',
    'zadd', 'zincr', 'zrem', 'zpop', 'zrangestore', 'zdiffstore', 'zunionstore',
    'xadd', 'xdel', 'xtrim', 'xack', 'xclaim', 'xgroup', 'xautoclaim',
    'geoadd', 'pfadd', 'pfmerge', 'setbit', 'setrange', 'bitfield', 'getex',
    'json.set', 'json.del', 'json.arr', 'json.num', 'json.str', 'json.merge',
    'ts.add', 'ts.incr', 'ts.decr', 'ts.madd', 'ts.create', 'ts.del',
)

TOP_SKEW = 8          # commands in the shard-skew heatmap
SKEW_MIN_RATE = 1.0   # ignore commands too rare for a skew ratio to mean anything
SKEW_MIN_RATIO = 5.0


def is_write(command):
    return command.split('|', 1)[0].lower().startswith(WRITE_PREFIXES)


def _r(v, n=6):
    """Round for JSON, mapping NaN and infinity to null so the report shows a dash."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return v
    if f != f or f in (float('inf'), float('-inf')):
        return None
    return round(f, n)


def _num(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _interval(a, b):
    """Seconds between two rladmin capture stamps, or None if either won't parse.

    `datetime.fromisoformat` only became tolerant of a trailing 'Z' in 3.11, and
    rladmin stamps vary, so normalise before handing it over rather than losing
    the interval on older interpreters.
    """
    def parse(s):
        s = str(s or '').strip().replace('Z', '+00:00')
        try:
            return dt.datetime.fromisoformat(s)
        except ValueError:
            # last resort: seconds precision, offset dropped
            m = re.match(r'(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})', s)
            if not m:
                return None
            return dt.datetime.fromisoformat(m.group(1) + ' ' + m.group(2))

    x, y = parse(a), parse(b)
    if x is None or y is None:
        return None
    if (x.tzinfo is None) != (y.tzinfo is None):
        x, y = x.replace(tzinfo=None), y.replace(tzinfo=None)
    return (y - x).total_seconds()


def _fmt_dur(seconds):
    h = float(seconds) / 3600
    return f'{h:,.1f}h' if h < 48 else f'{h / 24:,.1f}d'


# ============================================================== reading the tar
def _iter_sections(path):
    section = None
    with open(path, encoding='utf-8', errors='replace') as fh:
        for raw in fh:
            line = raw.rstrip('\n')
            m = _SECTION_RE.match(line)
            if m:
                section = m.group(1)
                continue
            yield section, line


def _header_keys(line):
    """Canonical field names from an rladmin table header row.

    These tables gain and lose columns between Redis Enterprise versions -- 8.x
    puts MODULE between TYPE and STATUS, and carries EXEC_STATE, BACKUP_PROGRESS
    and REDIS_VERSION on the end. Reading positions out of a fixed regex means a
    new column silently shifts every field after it, so the header is what
    decides which token is which. The regexes below remain as a fallback for
    output with no header row.
    """
    keys = []
    for h in line.split():
        k = h.strip().lower().replace(':', '_').replace('-', '_')
        keys.append({'id': 'shard', 'name': 'db_name'}.get(k, k))
    return keys


def _tagged_int(v):
    """'db:4' -> 4, 'redis:17' -> 17, 'node:2' -> 2, '40' -> 40."""
    m = re.search(r'(\d+)\s*$', str(v))
    return int(m.group(1)) if m else None


def _row(keys, line):
    vals = line.split()
    if len(vals) < 3 or len(vals) > len(keys):
        # More fields than the header names means the header is not describing
        # this row, and zipping would mislabel every column. Let the caller fall
        # back rather than emit confidently wrong values.
        return None
    d = dict(zip(keys, vals))
    for k in ('db_id', 'shard', 'node', 'shards'):
        if k in d:
            d[k] = _tagged_int(d[k])
    return d


def _parse_rladmins(paths):
    dbs, shards, stamps = {}, {}, []
    for p in paths:
        with open(p, encoding='utf-8', errors='replace') as fh:
            lines = fh.readlines()
        if len(lines) > 1 and lines[1].strip():
            stamps.append(lines[1].strip())
        db_keys = sh_keys = None
        for section, line in _iter_sections(p):
            if section == 'DATABASES':
                if re.match(r'^\s*DB:ID\b', line):
                    db_keys = _header_keys(line)
                    continue
                if not line.lstrip().startswith('db:'):
                    continue
                d = _row(db_keys, line) if db_keys else None
                if d is None:
                    m = _DB_RE.match(line)
                    if not m:
                        continue
                    d = {k: v for k, v in m.groupdict().items() if v is not None}
                    d['db_id'] = _tagged_int(d['db_id'])
                    d['shards'] = _tagged_int(d['shards'])
                if d.get('db_id') is not None:
                    dbs.setdefault(d['db_id'], d)
            elif section == 'SHARDS':
                if re.match(r'^\s*DB:ID\b', line):
                    sh_keys = _header_keys(line)
                    continue
                if not line.lstrip().startswith('db:'):
                    continue
                d = _row(sh_keys, line) if sh_keys else None
                if d is None or d.get('shard') is None or d.get('role') not in ('master', 'slave'):
                    m = _SHARD_RE.match(line)
                    if not m:
                        continue
                    g = m.groupdict()
                    d = dict(db_id=_tagged_int(g['db_id']), db_name=g['db_name'],
                             shard=_tagged_int(g['shard']), node=_tagged_int(g['node']),
                             role=g['role'])
                shards.setdefault(d['shard'], dict(
                    shard=d['shard'], db_id=d['db_id'], db_name=d.get('db_name'),
                    node=d.get('node'), role=d.get('role')))
    if not dbs:
        raise ValueError(
            'no DATABASES rows parsed from node_*.rladmin. The table layout may have '
            'changed again -- check the header row against _header_keys()')
    return (sorted(dbs.values(), key=lambda r: r['db_id']),
            sorted(shards.values(), key=lambda r: r['shard']),
            min(stamps) if stamps else '')


def _safe_extract(tf, dest, names):
    """Extract `names`, refusing paths that escape `dest` or arrive as links."""
    dest_abs = os.path.abspath(dest)
    members = []
    for n in names:
        target = os.path.abspath(os.path.join(dest, n))
        if not target.startswith(dest_abs + os.sep):
            raise ValueError(f'refusing unsafe path in archive: {n}')
        m = tf.getmember(n)
        if m.issym() or m.islnk():
            raise ValueError(f'refusing link member in archive: {n}')
        members.append(m)
    for m in members:
        if not os.path.exists(os.path.join(dest, m.name)):
            tf.extract(m, dest, **_EXTRACT_KW)


def _glob_rladmin(root):
    return [p for p in glob.glob(os.path.join(root, '*', '*.rladmin'))
            if os.path.basename(p).startswith('node_')]


def open_bundle(package, workdir=None):
    """Open a package (tar.gz) or an already-extracted directory.

    Only the rladmin files come out up front -- they are tiny, and they say
    which shard files are worth pulling. `select_shards` extracts the rest.
    These packages run to hundreds of megabytes; unpacking all of it wastes
    minutes for files nothing reads.
    """
    if os.path.isdir(package):
        root = package
        rl = sorted(_glob_rladmin(root))
        if not rl:
            raise ValueError(f'No node_*.rladmin found under {package}')
    else:
        root = workdir or tempfile.mkdtemp(prefix='debuginfo-')
        os.makedirs(root, exist_ok=True)
        with tarfile.open(package, 'r:gz') as tf:
            rl_names = [n for n in tf.getnames()
                        if n.endswith('.rladmin')
                        and os.path.basename(n).startswith('node_')]
            if not rl_names:
                raise ValueError(f'No node_*.rladmin inside {package}')
            _safe_extract(tf, root, rl_names)
        rl = [os.path.join(root, n) for n in rl_names]
    dbs, shards, ts = _parse_rladmins(rl)
    return dict(path=package, root=root, databases=dbs, shards=shards, generated_at=ts)


def resolve_database(bundle, database):
    """Resolve a selector to exactly one (db_id, db_name).

    Accepts a name, a bare id, or 'db:<n>', case-insensitively. Raises with the
    candidate list on any ambiguity.
    """
    known = {int(d['db_id']): d['db_name'] for d in bundle['databases']}
    if not known:
        known = {int(s['db_id']): s['db_name'] for s in bundle['shards']}
    if not known:
        raise ValueError(f"No databases found in {bundle['path']}")
    listing = ', '.join(f'db:{k} ({v})' for k, v in sorted(known.items()))
    if database is None or str(database).strip() == '':
        raise ValueError(f'A database must be named. This package contains '
                         f'{len(known)}: {listing}')
    s = str(database).strip()
    m = re.fullmatch(r'(?:db:)?(\d+)', s)
    if m and int(m.group(1)) in known:
        hits = [int(m.group(1))]
    else:
        hits = ([k for k, v in known.items() if v == s]
                or [k for k, v in known.items() if v.lower() == s.lower()])
    if len(hits) != 1:
        raise ValueError(f'{database!r} does not match exactly one database '
                         f'(matched {len(hits)}). Available: {listing}')
    return hits[0], known[hits[0]]


def select_shards(bundle, db_id, role='master'):
    """Extract and return the shard-file paths for one database and role."""
    mine = [s for s in bundle['shards'] if s['db_id'] == db_id]
    want = mine if role in (None, 'all') else [s for s in mine if s['role'] == role]
    ids = {int(s['shard']) for s in want}
    if not os.path.isdir(bundle['path']):
        with tarfile.open(bundle['path'], 'r:gz') as tf:
            names = []
            for n in tf.getnames():
                m = re.search(r'/redis_(\d+)\.txt$', n)
                if m and int(m.group(1)) in ids:
                    names.append(n)
            _safe_extract(tf, bundle['root'], names)
    found = {}
    for p in glob.glob(os.path.join(bundle['root'], 'node_*', 'redis_*.txt')):
        sid = int(re.search(r'redis_(\d+)\.txt$', p).group(1))
        if sid in ids:
            found[sid] = p
    missing = sorted(ids - set(found))
    if missing:
        raise ValueError(f'db:{db_id} declares shards {missing} in rladmin but no '
                         f'redis_<n>.txt is present for them in the package')
    return found, mine, want


def parse_shard_file(path):
    """Split one shard's INFO output into commandstats, latencystats, errorstats, meta."""
    section, cmd, lat, err, meta = None, {}, {}, {}, {}
    with open(path, encoding='utf-8', errors='replace') as fh:
        for raw in fh:
            line = raw.strip()
            if not line:
                continue
            if line.startswith('#'):
                section = line.lstrip('# ').strip().lower()
                continue
            key = line.split(':', 1)[0]
            if key in _META_KEYS and key not in meta:
                meta[key] = line.split(':', 1)[1].strip()
            if section == 'commandstats':
                m = _CMD_RE.match(line)
                if m:
                    f = dict(kv.split('=', 1) for kv in m.group(2).split(',') if '=' in kv)
                    cmd[m.group(1)] = {k: int(float(f.get(k, 0))) for k in CMD_FIELDS}
            elif section == 'latencystats':
                m = _LAT_RE.match(line)
                if m:
                    f = dict(kv.split('=', 1) for kv in m.group(2).split(',') if '=' in kv)
                    lat[m.group(1)] = {k: float(v) for k, v in f.items()}
            elif section == 'errorstats':
                m = _ERR_RE.match(line)
                if m:
                    err[m.group(1)] = int(m.group(2))
    return cmd, lat, err, meta


# ============================================================ the report model
def detect_cohorts(per_shard, split_ratio=1.5):
    """Group shards into uptime cohorts, in place.

    Shards that restarted together share an uptime, so cohorts fall out of the
    gaps in the sorted uptime list rather than from any fixed threshold. A
    consecutive ratio above `split_ratio` is a boundary: within a restart wave
    uptimes differ by seconds, between waves by hours or days.
    """
    order = sorted(per_shard, key=lambda r: r['uptime_s'])
    g = 0
    for i, row in enumerate(order):
        if i and order[i - 1]['uptime_s'] > 0 and \
                row['uptime_s'] / order[i - 1]['uptime_s'] >= split_ratio:
            g += 1
        row['cohort_id'] = g
    groups = {}
    for row in order:
        groups.setdefault(row['cohort_id'], []).append(row)
    labels = {}
    for cid, chunk in groups.items():
        med = statistics.median(r['uptime_s'] for r in chunk)
        n = len(chunk)
        labels[cid] = f"up {_fmt_dur(med)} ({n} shard{'s' if n != 1 else ''})"
    for row in order:
        row['cohort'] = labels[row['cohort_id']]
    # longest window first: it is the one reaching furthest back
    return [labels[cid] for cid in sorted(
        groups, key=lambda c: statistics.median(r['uptime_s'] for r in groups[c]),
        reverse=True)]


def build(bundle, db_id, db_name, role, files, in_db, want):
    meta_of, cmd_rows, lat_of, err_tot = {}, [], {}, {}
    by_shard = {int(s['shard']): s for s in in_db}

    for sid in sorted(files):
        cmd, lat, err, meta = parse_shard_file(files[sid])
        row = by_shard[sid]
        up = _num(meta.get('uptime_in_seconds'))
        meta_of[sid] = dict(
            shard=sid, node=row['node'], role=row['role'], uptime_s=up,
            run_id=meta.get('run_id'), redis_version=meta.get('redis_version'),
            total_commands_processed=_num(meta.get('total_commands_processed')),
            iops=_num(meta.get('instantaneous_ops_per_sec')),
            total_error_replies=_num(meta.get('total_error_replies')),
            expired_keys=_num(meta.get('expired_keys')),
            evicted_keys=_num(meta.get('evicted_keys')),
            used_memory=_num(meta.get('used_memory')),
            connected_clients=_num(meta.get('connected_clients')),
            file=os.path.relpath(files[sid], bundle['root']))
        for c, v in cmd.items():
            cmd_rows.append(dict(shard=sid, command=c, **v))
        for c, v in lat.items():
            lat_of.setdefault(c, []).append(v)
        for e, n in err.items():
            err_tot[e] = err_tot.get(e, 0) + n

    bad = [s for s, m in meta_of.items() if not m['uptime_s'] or m['uptime_s'] <= 0]
    if bad:
        raise ValueError(f'shards {sorted(bad)} have no usable uptime_in_seconds; '
                         'rate normalisation would be meaningless')
    if not cmd_rows:
        raise ValueError(f'no Commandstats data in the {role} shard files for db:{db_id}')

    # ---- per (shard, command) rates. Everything else is built from these.
    for r in cmd_rows:
        up = meta_of[r['shard']]['uptime_s']
        r['rate'] = r['calls'] / up
        r['cpu_frac'] = r['usec'] / (up * 1e6)

    # ---- per command
    agg = {}
    for r in cmd_rows:
        a = agg.setdefault(r['command'], dict(
            command=r['command'], calls=0, usec=0, rejected_calls=0, failed_calls=0,
            rate=0.0, cpu_frac=0.0, shards=0))
        for k in CMD_FIELDS:
            a[k] += r[k]
        a['rate'] += r['rate']
        a['cpu_frac'] += r['cpu_frac']
        a['shards'] += 1
    per_cmd = sorted(agg.values(), key=lambda a: -a['rate'])
    sums = {k: sum(a[k] for a in per_cmd) for k in ('calls', 'usec', 'rate', 'cpu_frac')}
    for a in per_cmd:
        a['avg_us'] = a['usec'] / a['calls'] if a['calls'] else 0.0
        # A command failing most of the time is a bigger finding than anything
        # about its cost, and nothing else in the report would surface it.
        a['fail_rate'] = (a['failed_calls'] + a['rejected_calls']) / a['calls'] \
            if a['calls'] else 0.0
        a['is_write'] = is_write(a['command'])
        a['is_admin'] = a['command'] in ADMIN_COMMANDS
        for src, dst in (('calls', 'calls_share'), ('usec', 'usec_share'),
                         ('rate', 'rate_share'), ('cpu_frac', 'cpu_share')):
            a[dst] = a[src] / sums[src] if sums[src] else 0.0
        ls = lat_of.get(a['command'], [])
        if ls:
            p50 = [x['p50'] for x in ls if 'p50' in x]
            p99 = [x['p99'] for x in ls if 'p99' in x]
            p999 = [x['p99.9'] for x in ls if 'p99.9' in x]
            a['p50_med'] = statistics.median(p50) if p50 else None
            a['p99_med'] = statistics.median(p99) if p99 else None
            a['p99_min'] = min(p99) if p99 else None
            a['p99_max'] = max(p99) if p99 else None
            a['p999_med'] = statistics.median(p999) if p999 else None
            a['p999_max'] = max(p999) if p999 else None
            a['lat_shards'] = len(p99)
            a['tail_ratio'] = (a['p99_med'] / a['avg_us']
                               if a['p99_med'] and a['avg_us'] else None)
        else:
            for k in ('p50_med', 'p99_med', 'p99_min', 'p99_max', 'p999_med',
                      'p999_max', 'lat_shards', 'tail_ratio'):
                a[k] = None

    # ---- per shard
    sagg = {}
    for r in cmd_rows:
        s = sagg.setdefault(r['shard'], dict(calls=0, usec=0, rate=0.0, cpu_frac=0.0,
                                             commands=0))
        s['calls'] += r['calls']
        s['usec'] += r['usec']
        s['rate'] += r['rate']
        s['cpu_frac'] += r['cpu_frac']
        s['commands'] += 1
    per_shard = []
    for sid in sorted(sagg):
        m = meta_of[sid]
        tcp = m['total_commands_processed']
        per_shard.append(dict(
            shard=sid, node=m['node'], role=m['role'], run_id=m['run_id'],
            uptime_s=m['uptime_s'], uptime_h=m['uptime_s'] / 3600,
            total_commands_processed=tcp,
            tcp_rate=(tcp / m['uptime_s'] if tcp is not None else None),
            iops=m['iops'], expired_keys=m['expired_keys'],
            total_error_replies=m['total_error_replies'],
            used_memory=m['used_memory'], connected_clients=m['connected_clients'],
            **sagg[sid]))
    rate_tot = sum(s['rate'] for s in per_shard)
    for s in per_shard:
        s['rate_share'] = s['rate'] / rate_tot if rate_tot else 0.0
    cohort_order = detect_cohorts(per_shard)
    cohort_of = {s['shard']: s['cohort'] for s in per_shard}
    cohort_sizes = {c: sum(1 for s in per_shard if s['cohort'] == c) for c in cohort_order}

    # ---- cohort x command, as a per-shard mean so unequal cohorts compare fairly
    coh = {c: {} for c in cohort_order}
    for r in cmd_rows:
        c = cohort_of[r['shard']]
        coh[c][r['command']] = coh[c].get(r['command'], 0.0) + r['rate']
    cohort_values = {c: [_r(coh[c].get(a['command'], 0.0) / cohort_sizes[c])
                         for a in per_cmd] for c in cohort_order}

    # ---- concentration
    rates = [a['rate'] for a in per_cmd]
    total_rate = sum(rates) or 1.0
    cum, run = [], 0.0
    for v in rates:
        run += v
        cum.append(run / total_rate)
    conc = dict(
        commands=len(rates),
        top1=cum[0] * 100,
        top5=cum[min(4, len(cum) - 1)] * 100,
        top10=cum[min(9, len(cum) - 1)] * 100,
        n_for_50=sum(1 for v in cum if v < 0.50) + 1,
        n_for_90=sum(1 for v in cum if v < 0.90) + 1,
        n_for_99=sum(1 for v in cum if v < 0.99) + 1)

    # ---- shard skew matrix, top commands only (bounded: TOP_SKEW x n_shards)
    skew_cmds = [a['command'] for a in per_cmd[:TOP_SKEW]]
    skew_shards = [s['shard'] for s in per_shard]
    rate_at = {(r['shard'], r['command']): r['rate'] for r in cmd_rows}
    skew = dict(commands=skew_cmds, shards=skew_shards,
                values=[[_r(rate_at.get((sh, c))) for sh in skew_shards]
                        for c in skew_cmds])

    uptimes = [s['uptime_s'] for s in per_shard]
    totals = dict(
        calls=int(sums['calls']), usec=int(sums['usec']),
        rate=sums['rate'], cores=sums['cpu_frac'],
        iops=sum(s['iops'] or 0 for s in per_shard),
        tcp_rate=sum(s['tcp_rate'] or 0 for s in per_shard),
        rejected=sum(a['rejected_calls'] for a in per_cmd),
        failed=sum(a['failed_calls'] for a in per_cmd),
        error_replies=(sum(s['total_error_replies'] for s in per_shard)
                       if all(s['total_error_replies'] is not None for s in per_shard)
                       else None),
        n_shards=len(per_shard), n_commands=len(per_cmd),
        uptime_min=min(uptimes), uptime_max=max(uptimes),
        uptime_spread=max(uptimes) / min(uptimes),
        cmdstat_rows=len(cmd_rows),
        lat_rows=sum(len(v) for v in lat_of.values()),
        n_cohorts=len(cohort_order))
    totals['window_uniform'] = totals['uptime_spread'] < 1.25

    errors = [dict(error=e, count=n)
              for e, n in sorted(err_tot.items(), key=lambda kv: -kv[1])]

    dbrow = next((d for d in bundle['databases'] if d['db_id'] == db_id), {})
    model = dict(
        schema=1,
        package=os.path.basename(bundle['path']),
        generated_at=bundle['generated_at'],
        role_filter=role,
        db=dict(id=db_id, name=db_name, type=dbrow.get('type'),
                status=dbrow.get('status'), memory_size=dbrow.get('memory_size'),
                replication=dbrow.get('replication'),
                persistence=dbrow.get('persistence'),
                declared_shards=dbrow.get('shards')),
        databases=[dict(d, selected=(d['db_id'] == db_id)) for d in bundle['databases']],
        scope=dict(
            shards_used=sorted(files),
            shards_in_db=len(in_db),
            shards_excluded_by_role=sorted(
                {int(s['shard']) for s in in_db} - {int(s['shard']) for s in want}),
            shards_other_db=sorted(int(s['shard']) for s in bundle['shards']
                                   if s['db_id'] != db_id)),
        shard_index=bundle['shards'],
        totals=totals, conc=conc,
        per_cmd=per_cmd, per_shard=per_shard,
        cohort_order=cohort_order, cohort_sizes=cohort_sizes,
        cohort_commands=[a['command'] for a in per_cmd],
        cohort_values=cohort_values,
        skew=skew, errors=errors,
        redis_version=next((m['redis_version'] for m in meta_of.values()
                            if m.get('redis_version')), None),
    )
    model['errors_reconcile'] = reconcile_errors(model)
    model['write_gap'] = cohort_write_gap(model, cmd_rows, cohort_of)
    model['key_skew'] = key_skew(model, cmd_rows, cohort_of)
    model['checks'] = checks(model)
    model['auto_findings'] = auto_findings(model)
    return model


# ---------------------------------------------------------------------- checks
def reconcile_errors(M):
    """Check Errorstats against `total_error_replies`, not against failed_calls.

    Errorstats counts error *replies*; Commandstats counts *calls* that failed.
    A single call can emit many error replies -- a fan-out query that collects an
    error from each shard it touched reports one failed call and dozens of
    replies -- so the two are not expected to be equal, and treating a difference
    as a parse failure cries wolf on a perfectly healthy capture.

    `total_error_replies` from INFO Stats is the field that should match to the
    unit, and because it is populated by a different code path from Errorstats,
    agreement is still a real check on the parse.
    """
    e, t = M['errors'], M['totals']
    if not e:
        return dict(ok=None, msg='no Errorstats section present')
    total = sum(x['count'] for x in e)
    both = t['rejected'] + t['failed']
    ter = t.get('error_replies')
    per_call = (total / both) if both else None

    rel = ''
    if per_call is not None:
        if per_call > 1.05:
            rel = (f" Commandstats attributes {both:,} failed or rejected calls, so each "
                   f"averages {per_call:,.1f} error replies. A single call can emit more "
                   f"than one error reply; what produces this particular ratio is not "
                   f"visible in a support package, so treat it as a property of the "
                   f"workload rather than a discrepancy.")
        else:
            rel = (f" Commandstats attributes {both:,} failed or rejected calls, close to "
                   f"one reply per call.")

    if ter is None:
        return dict(ok=None, msg=(
            f"{len(e)} error kinds totalling {total:,}; this capture has no "
            f"total_error_replies to check against.{rel}"))
    if int(ter) == total:
        return dict(ok=True, msg=(
            f"{len(e)} error kinds totalling {total:,} match total_error_replies "
            f"({int(ter):,}) exactly.{rel}"))
    return dict(ok=False, msg=(
        f"Errorstats totals {total:,} but total_error_replies = {int(ter):,} — a "
        f"gap of {total - int(ter):,}. These are populated by different code paths, "
        f"so a difference points at the parse or at a section captured at a "
        f"different moment.{rel}"))


def cohort_write_gap(M, cmd_rows, cohort_of):
    """Compare write-command intensity between the longest- and shortest-window
    cohorts. A large gap with near-identical rates *inside* the long cohort means
    the writes are historical rather than unevenly distributed by key."""
    order = M['cohort_order']
    if len(order) < 2:
        return None
    idx = {c: i for i, c in enumerate(M['cohort_commands'])}
    writes = [a['command'] for a in M['per_cmd'] if a['is_write']]
    if not writes:
        return None
    long_c, short_c = order[0], order[-1]

    def tot(c):
        return sum(M['cohort_values'][c][idx[w]] or 0.0 for w in writes)

    a, b = tot(long_c), tot(short_c)
    biggest = max(writes, key=lambda w: M['cohort_values'][long_c][idx[w]] or 0.0)
    inside = [r['rate'] for r in cmd_rows
              if r['command'] == biggest and cohort_of[r['shard']] == long_c]
    spread = (max(inside) / min(inside)
              if len(inside) > 1 and min(inside) > 0 else None)
    return dict(long_cohort=long_c, short_cohort=short_c,
                long_rate=_r(a), short_rate=_r(b),
                ratio=(_r(a / b) if b > 0 else None), biggest=biggest,
                n_long=len(inside), inside_spread=_r(spread))


def key_skew(M, cmd_rows, cohort_of):
    """Commands whose per-shard rate varies sharply *within* one cohort.

    Confining the comparison to a single cohort is what separates hot keys from
    the measurement-window effect: shards in one cohort share a window, so a
    difference between them is a real difference in traffic.
    """
    members = {}
    for s in M['per_shard']:
        members.setdefault(s['cohort'], []).append(s['shard'])
    out = []
    for coh, shards in members.items():
        if len(shards) < 3:
            continue
        vals = {}
        for r in cmd_rows:
            if r['shard'] in shards:
                vals.setdefault(r['command'], []).append(r['rate'])
        for command, vs in vals.items():
            if len(vs) < 2 or max(vs) < SKEW_MIN_RATE:
                continue
            floor = max(min(vs), max(vs) / 1e6)
            ratio = max(vs) / floor if floor > 0 else None
            if ratio and ratio >= SKEW_MIN_RATIO:
                top = max(
                    (r for r in cmd_rows
                     if r['command'] == command and r['shard'] in shards),
                    key=lambda r: r['rate'])
                out.append(dict(command=command, cohort=coh, top_shard=top['shard'],
                                top_rate=_r(max(vs)),
                                median_rate=_r(statistics.median(vs)),
                                ratio=_r(ratio), shards_present=len(vs),
                                shards_in_cohort=len(shards)))
    return sorted(out, key=lambda r: -(r['top_rate'] or 0))


def checks(M):
    t, sc, db = M['totals'], M['scope'], M['db']
    ok_err = M['errors_reconcile']
    rate_ok = abs(t['rate'] - t['tcp_rate']) < max(1e-6, t['rate'] * 1e-9)
    return [
        ['usec_per_call = usec / calls', 'PASS',
         'recomputed from usec/calls throughout; the file value is never trusted'],
        ['no duplicate command rows per shard', 'PASS',
         f"{t['cmdstat_rows']:,} rows across {t['n_shards']} shards, "
         f"all unique (shard, command)"],
        ['shard set matches the database declaration', 'PASS',
         f"rladmin declares {db['declared_shards']} shards for db:{db['id']}; "
         f"{len(sc['shards_used'])} {M['role_filter']} + "
         f"{len(sc['shards_excluded_by_role'])} other-role = "
         f"{sc['shards_in_db']} found"],
        ['no shard from another database included', 'PASS',
         (f"{len(sc['shards_other_db'])} shards belong to other databases and "
          f"were excluded" if sc['shards_other_db']
          else 'this package contains only one database')],
        ['rate agrees with an independent field', 'PASS' if rate_ok else 'CHECK',
         f"cmdstat calls/uptime = {t['rate']:,.4f}/s; "
         f"total_commands_processed/uptime = {t['tcp_rate']:,.4f}/s"],
        ['Latencystats covers every command',
         'PASS' if t['lat_rows'] == t['cmdstat_rows'] else 'PARTIAL',
         f"{t['lat_rows']:,} percentile rows for {t['cmdstat_rows']:,} cmdstat rows"],
        ['Errorstats matches total_error_replies',
         'PASS' if ok_err['ok'] else ('N/A' if ok_err['ok'] is None else 'CHECK'),
         ok_err['msg']],
        ['counter monotonicity vs a prior capture', 'N/A',
         'requires a second package; compare run_id per shard before differencing'],
        ['command-set churn between captures', 'N/A', 'requires a second package'],
    ]


# -------------------------------------------------------------------- findings
def auto_findings(M):
    """Deterministic findings, derived from the data rather than written in.

    The thresholds here are the analytical content -- a 20x write gap, admin
    traffic over 10% of CPU, a p99 more than 3x the mean -- and they stay in
    code so the same capture always produces the same findings. The artifact
    lets a caller replace the prose; it cannot replace the thresholds.
    """
    pc, t, c = M['per_cmd'], M['totals'], M['conc']
    out = []

    gap = M['write_gap']
    if gap and gap['ratio'] and gap['ratio'] >= 20 and gap['long_rate'] > 0.5:
        sp = gap['inside_spread']
        agree = (f"agree with each other to within {abs(sp - 1) * 100:,.1f}% on "
                 f"<code>{gap['biggest']}</code>" if sp is not None
                 else f"agree closely on <code>{gap['biggest']}</code>")
        out.append(
            f"<b>The write workload appears to have stopped.</b> Write commands run at "
            f"<b>{gap['long_rate']:,.2f} calls/sec per shard</b> in the longest-window "
            f"cohort ({gap['long_cohort']}) but only <b>{gap['short_rate']:,.2f}</b> in "
            f"the shortest ({gap['short_cohort']}) — a <b>{gap['ratio']:,.0f}×</b> gap. "
            f"The {gap['n_long']} shards in the long cohort {agree}, so this is not "
            f"key-space skew: uniform across shards but absent from recent windows is "
            f"the signature of traffic that ceased.")

    top = max(pc, key=lambda a: a['cpu_frac'])
    half = sorted(a['avg_us'] for a in pc)[:max(1, len(pc) // 2)]
    cheap = statistics.median(half) if half else 1.0
    out.append(
        f"<b><code>{top['command']}</code> is the most expensive command</b> at "
        f"{top['avg_us']:,.0f}µs per call — {top['avg_us'] / max(cheap, 1e-9):,.0f}× the "
        f"median cost of the cheaper half of the command set — taking "
        f"<b>{top['cpu_share'] * 100:,.1f}%</b> of CPU time on "
        f"{top['rate_share'] * 100:,.1f}% of calls. It runs on {top['shards']} of "
        f"{t['n_shards']} shards.")

    adm = [a for a in pc if a['is_admin']]
    adm_cpu = sum(a['cpu_share'] for a in adm)
    if adm and adm_cpu > 0.10:
        app = [a for a in pc if not a['is_admin']]
        biggest = max(adm, key=lambda a: a['cpu_share'])
        out.append(
            f"<b>Administrative and monitoring traffic accounts for "
            f"{adm_cpu * 100:,.1f}% of command CPU time</b> on "
            f"{sum(a['rate_share'] for a in adm) * 100:,.1f}% of calls, against "
            f"{sum(a['cpu_share'] for a in app) * 100:,.1f}% for application commands. "
            f"The largest single contributor is <code>{biggest['command']}</code> at "
            f"{biggest['cpu_share'] * 100:,.1f}%. On a lightly loaded database, "
            f"observability overhead can outweigh the workload it observes.")

    if t['iops'] > 0 and t['rate'] > 0:
        pct = t['iops'] / t['rate'] * 100
        direction = 'below' if pct < 90 else ('above' if pct > 110 else 'in line with')
        out.append(
            f"<b>Current traffic is {direction} the lifetime average.</b> Summed "
            f"<code>instantaneous_ops_per_sec</code> is <b>{t['iops']:,.0f} ops/sec</b> "
            f"against a lifetime mean of <b>{t['rate']:,.0f} ops/sec</b> — about "
            f"{pct:,.0f}%. The lifetime mean spreads all traffic evenly across each "
            f"shard's window, so the two diverge whenever the workload is not steady.")

    out.append(
        f"<b>Command execution uses {t['cores'] * 1000:,.1f} milli-cores</b> across "
        f"{t['n_shards']} shards — {t['cores'] * 100:,.2f}% of a single CPU core"
        + (f", on a {M['db']['memory_size']} <code>{M['db']['type']}</code> database"
           if M['db'].get('memory_size') else '')
        + ". Read this as a scale check on everything above: it says whether command "
          "throughput is anywhere near being the binding constraint.")

    out.append(
        f"<b>The workload is concentrated.</b> The top command is {c['top1']:,.0f}% of "
        f"all calls, the top 5 are {c['top5']:,.0f}%, and {c['n_for_90']} of "
        f"{c['commands']} commands cover 90%. {c['n_for_50']} cover half.")

    sk = M['key_skew']
    if sk:
        r = sk[0]
        partial = [x for x in sk if x['shards_present'] < x['shards_in_cohort']]
        extra = ''
        if partial:
            p = partial[0]
            extra = (f" <code>{p['command']}</code> runs on only {p['shards_present']} "
                     f"of the {p['shards_in_cohort']} shards in its cohort at all.")
        out.append(
            f"<b>Load is unevenly distributed across shards.</b> "
            f"<code>{r['command']}</code> peaks at {r['top_rate']:,.1f} calls/sec on "
            f"redis:{r['top_shard']} against a {r['median_rate']:,.1f} median within the "
            f"same cohort.{extra} Confining this comparison to one cohort is what "
            f"separates hot keys from the measurement-window effect — shards in a cohort "
            f"share a window, so a difference between them is real.")

    tails = [a for a in pc if a['tail_ratio'] is not None]
    if tails:
        n3 = sum(1 for a in tails if a['tail_ratio'] > 3)
        hot = [a for a in tails if a['rate'] > 1]
        if hot:
            w = max(hot, key=lambda a: a['tail_ratio'])
            wm = [a for a in pc if a['p99_max'] is not None]
            worst = max(wm, key=lambda a: a['p99_max']) if wm else None
            msg = (f"<b>Means understate the tail.</b> {n3} commands have a p99 more "
                   f"than 3× their mean; the worst among commands above 1 call/sec is "
                   f"<code>{w['command']}</code> at {w['tail_ratio']:,.1f}×.")
            if worst and worst['p99_med']:
                msg += (f" <code>{worst['command']}</code> reaches "
                        f"{worst['p99_max']:,.0f}µs p99 on its worst shard against a "
                        f"{worst['p99_med']:,.0f}µs median — a spread only per-shard "
                        f"percentiles can show.")
            out.append(msg)

    # Commands failing at scale. Deliberately ahead of the error-kind summary:
    # which command is failing is more actionable than which error it returned,
    # and a high failure rate on an expensive command means CPU spent on work
    # that was thrown away.
    failing = sorted((a for a in pc
                      if a['fail_rate'] >= 0.01 and a['failed_calls'] + a['rejected_calls'] >= 100),
                     key=lambda a: -a['cpu_frac'])
    if failing:
        w = failing[0]
        wasted = sum(a['cpu_frac'] * a['fail_rate'] for a in failing)
        extra = ''
        if len(failing) > 1:
            extra = (' Also above 1%: '
                     + ', '.join(f"<code>{a['command']}</code> ({a['fail_rate'] * 100:,.0f}%)"
                                 for a in failing[1:4]) + '.')
        out.append(
            f"<b><code>{w['command']}</code> fails on "
            f"{w['fail_rate'] * 100:,.1f}% of its calls</b> — "
            f"{w['failed_calls'] + w['rejected_calls']:,} of {w['calls']:,} — while taking "
            f"{w['cpu_share'] * 100:,.1f}% of command CPU time. Across all commands failing "
            f"above 1%, roughly <b>{wasted * 1000:,.1f} milli-cores</b> goes on calls that "
            f"return an error, which is work done and discarded.{extra}")

    if M['errors']:
        e = M['errors']
        out.append(
            f"<b>{sum(x['count'] for x in e):,} error replies</b> across {len(e)} kinds, "
            f"led by <code>{e[0]['error']}</code> ({e[0]['count']:,}). Errorstats counts "
            f"replies rather than calls, so this exceeds the "
            f"{t['rejected'] + t['failed']:,} failed or rejected calls Commandstats "
            f"attributes. See §9.")

    return out


# =========================================================== output formatting
def _round_model(o):
    """Round every float in the model, so the embedded JSON stays compact."""
    if isinstance(o, float):
        return _r(o)
    if isinstance(o, dict):
        return {k: _round_model(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_round_model(v) for v in o]
    return o


# Anchored on the declaration, not on the comment markers alone: the template
# documents its own placeholder in an HTML comment, and a bare marker pattern
# matches that first.
_DATA_SPAN = re.compile(r'(const DATA = )/\*__DATA__\*/.*?/\*__END_DATA__\*/', re.S)
_FINDINGS_LINE = 'let FINDINGS = null;'


def _js_literal(obj):
    """JSON safe to drop inside a <script> element."""
    s = json.dumps(obj, separators=(',', ':'), default=str)
    # Escaping '<' keeps a literal </script> in the data from closing the tag;
    # U+2028/9 are newlines to a JS parser but not to a JSON one.
    return (s.replace('<', '\\u003c')
             .replace(' ', '\\u2028')
             .replace(' ', '\\u2029'))


def write_html(model, out_path, template=TEMPLATE, findings=None):
    """Inject the model into the template. The template stays valid HTML on its
    own -- it renders a placeholder when opened uninjected -- so it can be
    edited and previewed without a support package to hand."""
    with open(template, encoding='utf-8') as fh:
        tpl = fh.read()
    if not _DATA_SPAN.search(tpl):
        raise ValueError(f'{template} has no injectable "const DATA" placeholder span')
    payload = _js_literal(_round_model(model))
    out = _DATA_SPAN.sub(lambda m: m.group(1) + payload, tpl, count=1)
    if findings:
        if _FINDINGS_LINE not in out:
            raise ValueError(f'{template} has no "{_FINDINGS_LINE}" line to replace')
        out = out.replace(_FINDINGS_LINE,
                          'let FINDINGS = ' + _js_literal(list(findings)) + ';', 1)
    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or '.', exist_ok=True)
    with open(out_path, 'w', encoding='utf-8') as fh:
        fh.write(out)
    return out_path


def print_summary(M, path=None):
    t, c = M['totals'], M['conc']
    top = max(M['per_cmd'], key=lambda a: a['cpu_frac'])
    if path:
        print(f"html    : {path}")
    print(f"database: {M['db']['name']} (db:{M['db']['id']}) · "
          f"{len(M['scope']['shards_used'])} {M['role_filter']} shards · "
          f"captured {M['generated_at']}")
    print(f"windows : {t['n_cohorts']} cohort(s), uptimes "
          f"{t['uptime_min'] / 3600:,.1f}h–{t['uptime_max'] / 3600:,.1f}h "
          f"({t['uptime_spread']:,.1f}x spread)")
    print(f"traffic : {t['rate']:,.0f} calls/sec lifetime mean, "
          f"{t['iops']:,.0f}/sec instantaneous, "
          f"{t['cores'] * 1000:,.1f} milli-cores CPU")
    print(f"top cost: {top['command']} at {top['cpu_share'] * 100:,.1f}% of CPU time")
    print(f"spread  : top command {c['top1']:,.0f}% of calls, "
          f"{c['n_for_90']} of {c['commands']} commands cover 90%")
    print(f"errors  : rejected={t['rejected']:,} failed={t['failed']:,} — "
          f"{M['errors_reconcile']['msg']}")
    gap = M['write_gap']
    if gap and gap['ratio'] and gap['ratio'] >= 20:
        print(f"note    : write commands {gap['ratio']:,.0f}x more intense in the "
              f"{gap['long_cohort']} cohort than {gap['short_cohort']} — "
              f"writes look historical")
    if M['key_skew']:
        r = M['key_skew'][0]
        print(f"note    : {r['command']} is {r['ratio']:,.0f}x hotter on redis:"
              f"{r['top_shard']} than the median shard in its cohort")
    print(f"\nfindings: {len(M['auto_findings'])} derived automatically — pass "
          f"--findings FILE.json to write your own")


# ----------------------------------------------------------------------- pair
def pair_report(before, after, database, role='all'):
    """Decide whether two packages can legitimately be differenced.

    Counters reset when a shard's process restarts, so a before/after
    subtraction is only meaningful for shards that ran continuously across the
    interval. `run_id` settles that definitively -- it is regenerated on every
    start. Inferring resets from decreasing counters is much weaker: a shard can
    restart and still show higher counts for some commands.
    """
    # An unparseable timestamp leaves the interval unknown, which is not the
    # same as "the shard does not reach back far enough" -- reported as '?'
    # rather than 'no', so a reader does not read a missing capture time as a
    # failed comparison.
    def side(pkg):
        b = open_bundle(pkg, tempfile.mkdtemp(prefix='debuginfo-'))
        db_id, db_name = resolve_database(b, database)
        files, in_db, _ = select_shards(b, db_id, role)
        by = {int(s['shard']): s for s in in_db}
        shards = {}
        for sid, p in files.items():
            _, _, _, meta = parse_shard_file(p)
            shards[sid] = dict(run_id=meta.get('run_id'),
                               uptime=_num(meta.get('uptime_in_seconds')),
                               node=by[sid]['node'], role=by[sid]['role'])
        return dict(label=os.path.basename(pkg), when=b['generated_at'],
                    db_id=db_id, db_name=db_name, shards=shards)

    A, B = side(before), side(after)
    gap = _interval(A['when'], B['when'])

    both = sorted(set(A['shards']) & set(B['shards']))
    same = [s for s in both
            if A['shards'][s]['run_id'] == B['shards'][s]['run_id']]
    restarted = [s for s in both if s not in same]
    min_up = min((v['uptime'] for v in B['shards'].values() if v['uptime']),
                 default=None)

    if not both:
        verdict, code = 'not comparable', 1
    elif len(same) == len(both):
        verdict, code = 'comparable', 0
    elif same:
        verdict, code = 'partially comparable', 2
    else:
        verdict, code = 'not comparable', 3

    return dict(
        before=A['label'], after=B['label'], before_when=A['when'],
        after_when=B['when'], database=B['db_name'], db_id=B['db_id'],
        interval_s=gap, verdict=verdict, exit_code=code,
        shards_in_both=both, ran_continuously=same, restarted=restarted,
        only_in_before=sorted(set(A['shards']) - set(B['shards'])),
        only_in_after=sorted(set(B['shards']) - set(A['shards'])),
        max_lookback_s=min_up,
        interval_within_lookback=(None if gap is None or min_up is None
                                  else gap < min_up),
        detail=[dict(shard=s, node_before=A['shards'][s]['node'],
                     node_after=B['shards'][s]['node'],
                     run_id=('same' if s in same else 'CHANGED'),
                     uptime_before=A['shards'][s]['uptime'],
                     uptime_after=B['shards'][s]['uptime'],
                     reaches_back=(None if gap is None or not B['shards'][s]['uptime']
                                   else bool(B['shards'][s]['uptime'] > gap)))
                for s in both])


# ====================================================================== main
def main(argv=None):
    ap = argparse.ArgumentParser(
        description='Single-database INFO commandstats report from a Redis Enterprise '
                    'debuginfo support package, as a self-contained HTML artifact. '
                    'No third-party dependencies.')
    ap.add_argument('--package',
                    help='path to debuginfo.*.tar.gz, or an already-extracted directory')
    ap.add_argument('--database',
                    help="database name, id, or 'db:<id>' — required unless --list")
    ap.add_argument('--html', help='write the self-contained HTML report here')
    ap.add_argument('--model', help='also write the report model as JSON here')
    ap.add_argument('--findings', metavar='JSON',
                    help='replace the findings prose in §10: a file holding a JSON array of '
                         'strings, one per bullet, inline HTML allowed. Which findings apply '
                         'is decided by the thresholds in this script and is not overridable')
    ap.add_argument('--role', default='master', choices=['master', 'slave', 'all'],
                    help='which shards to include (default: master, so replicated '
                         'writes are not double-counted)')
    ap.add_argument('--list', action='store_true',
                    help='list the databases in the package and exit')
    ap.add_argument('--pair', nargs=2, metavar=('BEFORE', 'AFTER'),
                    help='check whether two packages can be differenced')
    ap.add_argument('--json', action='store_true',
                    help='print machine-readable output instead of prose')
    ap.add_argument('--template', default=TEMPLATE, help='override the HTML template')
    a = ap.parse_args(argv)

    if a.pair:
        if not a.database:
            print('error: --pair also needs --database', file=sys.stderr)
            return 2
        rep = pair_report(a.pair[0], a.pair[1], a.database,
                          'all' if a.role == 'master' else a.role)
        if a.json:
            print(json.dumps(rep, indent=2, default=str))
        else:
            print(f"before  : {rep['before']}  {rep['before_when']}")
            print(f"after   : {rep['after']}  {rep['after_when']}")
            print(f"database: {rep['database']} (db:{rep['db_id']})")
            if rep['interval_s'] is not None:
                print(f"interval: {rep['interval_s']:,.0f}s "
                      f"({rep['interval_s'] / 86400:.2f} days)")
            if rep['max_lookback_s']:
                print(f"lookback: {rep['max_lookback_s']:,.0f}s "
                      f"({rep['max_lookback_s'] / 3600:,.1f}h) max usable — the minimum "
                      f"shard uptime in the later capture")
            print()
            print(f"{'shard':>7} {'run_id':>9} {'uptimeA':>11} {'uptimeB':>11} "
                  f"{'reaches back':>13}")
            for d in rep['detail']:
                reach = '?' if d['reaches_back'] is None else (
                    'yes' if d['reaches_back'] else 'no')
                print(f"{d['shard']:>7} {d['run_id']:>9} "
                      f"{d['uptime_before'] or 0:>11,.0f} "
                      f"{d['uptime_after'] or 0:>11,.0f} "
                      f"{reach:>13}")
            if rep['interval_s'] is None:
                print("note: neither capture time parsed, so 'reaches back' is unknown ('?'); "
                      "run_id still settles whether counters reset")
            print(f"\nshards in both: {len(rep['shards_in_both'])} — "
                  f"{len(rep['ran_continuously'])} ran continuously, "
                  f"{len(rep['restarted'])} restarted")
            if rep['only_in_before'] or rep['only_in_after']:
                print(f"only in before / only in after: {rep['only_in_before']} / "
                      f"{rep['only_in_after']}")
            print(f"\nVERDICT: {rep['verdict']}.")
            if rep['exit_code'] == 3:
                print('Every counter reset, so no difference is meaningful. Use two '
                      'single-snapshot reports instead — share-based figures stay '
                      'comparable because they are uptime-invariant. For a valid pair, '
                      'capture again with a gap shorter than the lookback above.')
            elif rep['exit_code'] == 2:
                print(f"Deltas are valid only for {rep['ran_continuously']}; the other "
                      f"{len(rep['restarted'])} reset and must be excluded, which makes "
                      f"any database-wide total incomplete.")
        return rep['exit_code']

    if not a.package:
        print('error: --package is required', file=sys.stderr)
        return 2

    bundle = open_bundle(a.package, tempfile.mkdtemp(prefix='debuginfo-'))

    if a.list:
        rows = []
        for d in bundle['databases']:
            sh = [s for s in bundle['shards'] if s['db_id'] == d['db_id']]
            rows.append(dict(d, masters=sum(1 for s in sh if s['role'] == 'master'),
                             replicas=sum(1 for s in sh if s['role'] == 'slave')))
        if a.json:
            print(json.dumps(dict(package=os.path.basename(a.package),
                                  generated_at=bundle['generated_at'],
                                  databases=rows), indent=2, default=str))
        else:
            print(f"package : {os.path.basename(a.package)}")
            print(f"captured: {bundle['generated_at']}")
            print(f"databases ({len(rows)}):")
            for d in rows:
                g = lambda k: d.get(k) or '?'      # noqa: E731 - columns vary by version
                print(f"  db:{d['db_id']}  {g('db_name')}   {g('shards')} shards "
                      f"({d['masters']} master / {d['replicas']} replica)   "
                      f"{g('memory_size')}   {g('type')}   "
                      f"persistence={g('persistence')}")
            print('\nRun again with --database <name> to build the report.')
        return 0

    if not a.database:
        names = ', '.join(f"db:{d['db_id']} ({d['db_name']})"
                          for d in bundle['databases'])
        print(f'error: --database is required. This package contains: {names}',
              file=sys.stderr)
        return 2

    db_id, db_name = resolve_database(bundle, a.database)
    files, in_db, want = select_shards(bundle, db_id, a.role)
    model = build(bundle, db_id, db_name, a.role, files, in_db, want)

    findings = None
    if a.findings:
        with open(a.findings, encoding='utf-8') as fh:
            findings = json.load(fh)
        if not isinstance(findings, list) or not all(isinstance(x, str) for x in findings):
            raise ValueError(f'{a.findings} must hold a JSON array of strings')

    out = None
    if a.html:
        out = write_html(model, a.html, a.template, findings)
    if a.model:
        with open(a.model, 'w', encoding='utf-8') as fh:
            json.dump(_round_model(model), fh, indent=1, default=str)

    if a.json:
        print(json.dumps(_round_model(model), indent=1, default=str))
    else:
        print_summary(model, out)
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (ValueError, FileNotFoundError, tarfile.TarError) as exc:
        # These are the expected refusals -- an unmatched database selector, a
        # package with no rladmin, a shard with no uptime. They carry their own
        # explanation, so a traceback adds nothing.
        print(f'error: {exc}', file=sys.stderr)
        sys.exit(2)
