"""Read a Redis Enterprise debuginfo support package.

A support package is a gzipped tar holding one directory per node and per
database. The pieces this module cares about:

    node_<n>/node_<n>.rladmin   cluster state: DATABASES and SHARDS tables,
                                and the capture timestamp on line 2
    node_<n>/redis_<s>.txt      the output of INFO for shard <s>

A package can hold several databases, and both rladmin tables are keyed by
database, so every shard is attributable to exactly one. Nothing here guesses
which database is wanted: `resolve_database` requires a selector and fails with
the list of candidates if it does not match exactly one.
"""
from __future__ import annotations

import os
import re
import tarfile
import tempfile
from dataclasses import dataclass, field

import pandas as pd

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
_META_KEYS = {
    'run_id', 'uptime_in_seconds', 'process_id', 'redis_version', 'used_memory',
    'total_commands_processed', 'instantaneous_ops_per_sec', 'connected_clients',
    'expired_keys', 'evicted_keys', 'total_net_input_bytes', 'total_net_output_bytes',
}

CMD_FIELDS = ('calls', 'usec', 'rejected_calls', 'failed_calls')


@dataclass
class Bundle:
    """A support package opened for reading, with its rladmin tables parsed."""
    path: str
    root: str
    databases: pd.DataFrame
    shards: pd.DataFrame
    generated_at: str
    shard_files: dict = field(default_factory=dict)   # shard id -> path in `root`

    def describe(self) -> str:
        if self.databases.empty:
            return 'no databases found'
        return ', '.join(f"db:{i} ({r.db_name}, {r.shards} shards, {r.memory_size})"
                         for i, r in self.databases.iterrows())


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


def _parse_rladmins(paths):
    dbs, shards, stamps = {}, {}, []
    for p in paths:
        with open(p, encoding='utf-8', errors='replace') as fh:
            lines = fh.readlines()
        if len(lines) > 1 and lines[1].strip():
            stamps.append(lines[1].strip())
        for section, line in _iter_sections(p):
            if section == 'DATABASES':
                m = _DB_RE.match(line)
                if m:
                    d = {k: v for k, v in m.groupdict().items() if v is not None}
                    d['db_id'] = int(d['db_id'])
                    d['shards'] = int(d['shards'])
                    dbs.setdefault(d['db_id'], d)
            elif section == 'SHARDS':
                m = _SHARD_RE.match(line)
                if m:
                    d = m.groupdict()
                    shards.setdefault(int(d['shard']), dict(
                        shard=int(d['shard']), db_id=int(d['db_id']),
                        db_name=d['db_name'], node=int(d['node']), role=d['role']))
    db_df = pd.DataFrame(sorted(dbs.values(), key=lambda r: r['db_id']))
    if not db_df.empty:
        db_df = db_df.set_index('db_id')
    sh_df = pd.DataFrame(sorted(shards.values(), key=lambda r: r['shard']))
    return db_df, sh_df, (min(stamps) if stamps else '')


def open_bundle(package, workdir=None):
    """Open a package (tar.gz) or an already-extracted directory.

    Only the rladmin files are extracted up front -- they are tiny, and they say
    which shard files are worth pulling out. `select_shards` extracts the rest.
    """
    if os.path.isdir(package):
        root = package
        rl = sorted(_glob_rladmin(root))
        if not rl:
            raise ValueError(f'No node_*.rladmin found under {package}')
        db, sh, ts = _parse_rladmins(rl)
        b = Bundle(path=package, root=root, databases=db, shards=sh, generated_at=ts)
    else:
        root = workdir or tempfile.mkdtemp(prefix='debuginfo-')
        os.makedirs(root, exist_ok=True)
        with tarfile.open(package, 'r:gz') as tf:
            names = tf.getnames()
            rl_names = [n for n in names
                        if n.endswith('.rladmin') and os.path.basename(n).startswith('node_')]
            if not rl_names:
                raise ValueError(f'No node_*.rladmin inside {package}')
            _safe_extract(tf, root, rl_names)
        db, sh, ts = _parse_rladmins([os.path.join(root, n) for n in rl_names])
        b = Bundle(path=package, root=root, databases=db, shards=sh, generated_at=ts)
    return b


def _glob_rladmin(root):
    import glob
    return [p for p in glob.glob(os.path.join(root, '*', '*.rladmin'))
            if os.path.basename(p).startswith('node_')]


def _safe_extract(tf, dest, names):
    """Extract `names`, refusing paths that escape `dest`."""
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
            tf.extract(m, dest)


def resolve_database(bundle, database):
    """Resolve a selector to exactly one (db_id, db_name). Accepts a name, an
    integer id, or 'db:<n>'. Raises with the candidate list on any ambiguity --
    a report silently scoped to the wrong database is worse than no report."""
    known = ({int(i): r['db_name'] for i, r in bundle.databases.iterrows()}
             if not bundle.databases.empty
             else {int(r.db_id): r.db_name for r in bundle.shards.itertuples()})
    if not known:
        raise ValueError(f'No databases found in {bundle.path}')
    listing = ', '.join(f'db:{k} ({v})' for k, v in sorted(known.items()))
    if database is None or str(database).strip() == '':
        raise ValueError(f'A database must be named. This package contains {len(known)}: {listing}')
    s = str(database).strip()
    m = re.fullmatch(r'(?:db:)?(\d+)', s)
    if m and int(m.group(1)) in known:
        hits = [int(m.group(1))]
    else:
        hits = [k for k, v in known.items() if v == s] or \
               [k for k, v in known.items() if v.lower() == s.lower()]
    if len(hits) != 1:
        raise ValueError(f'{database!r} does not match exactly one database '
                         f'(matched {len(hits)}). Available: {listing}')
    return hits[0], known[hits[0]]


def select_shards(bundle, db_id, role='master'):
    """Extract and return the shard-file paths for one database and role."""
    mine = bundle.shards[bundle.shards.db_id == db_id]
    want = mine if role in (None, 'all') else mine[mine.role == role]
    ids = set(int(s) for s in want.shard)
    if not os.path.isdir(bundle.path):
        with tarfile.open(bundle.path, 'r:gz') as tf:
            names = [n for n in tf.getnames()
                     if re.search(r'/redis_(\d+)\.txt$', n)
                     and int(re.search(r'/redis_(\d+)\.txt$', n).group(1)) in ids]
            _safe_extract(tf, bundle.root, names)
    import glob
    found = {}
    for p in glob.glob(os.path.join(bundle.root, 'node_*', 'redis_*.txt')):
        sid = int(re.search(r'redis_(\d+)\.txt$', p).group(1))
        if sid in ids:
            found[sid] = p
    missing = sorted(ids - set(found))
    if missing:
        raise ValueError(f'db:{db_id} declares shards {missing} in rladmin but no '
                         f'redis_<n>.txt is present for them in the package')
    bundle.shard_files = found
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


def load(package, database, role='master', workdir=None):
    """Open a package, scope it to one database, and parse its shard files."""
    bundle = open_bundle(package, workdir)
    db_id, db_name = resolve_database(bundle, database)
    files, in_db, want = select_shards(bundle, db_id, role)

    rows, latrows, errrows, metarows = [], [], [], []
    role_of = {int(r.shard): (int(r.node), r.role) for r in in_db.itertuples()}
    for sid, path in sorted(files.items()):
        cmd, lat, err, meta = parse_shard_file(path)
        node, rl_role = role_of[sid]
        metarows.append(dict(shard=sid, node=node, role=rl_role, db_id=db_id,
                             db_name=db_name, file=os.path.relpath(path, bundle.root),
                             **{k: meta.get(k) for k in sorted(_META_KEYS)}))
        for c, v in cmd.items():
            rows.append(dict(shard=sid, node=node, role=rl_role, command=c, **v))
        for c, v in lat.items():
            latrows.append(dict(shard=sid, command=c, **v))
        for e, n in err.items():
            errrows.append(dict(shard=sid, error=e, count=n))

    dbrow = bundle.databases.loc[db_id] if db_id in bundle.databases.index else None
    info = dict(
        package=os.path.basename(bundle.path), root=bundle.root,
        db_id=db_id, db_name=db_name, generated_at=bundle.generated_at,
        role_filter=role, databases=bundle.databases, shard_index=bundle.shards,
        shards_used=sorted(files), shards_in_db=int(len(in_db)),
        shards_excluded_by_role=sorted(set(in_db.shard) - set(want.shard)),
        shards_other_db=sorted(int(r.shard) for r in bundle.shards.itertuples()
                               if r.db_id != db_id),
        declared_shards=(int(dbrow['shards']) if dbrow is not None else None),
        memory_size=(dbrow.get('memory_size') if dbrow is not None else None),
        db_type=(dbrow.get('type') if dbrow is not None else None),
        persistence=(dbrow.get('persistence') if dbrow is not None else None),
        replication=(dbrow.get('replication') if dbrow is not None else None),
        redis_version=(dbrow.get('redis_version') if dbrow is not None else None),
    )
    return (info, pd.DataFrame(rows), pd.DataFrame(latrows),
            pd.DataFrame(errrows), pd.DataFrame(metarows))
