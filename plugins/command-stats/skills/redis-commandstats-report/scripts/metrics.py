"""Turn parsed shard data into the quantities a single capture can support.

The governing fact about `INFO commandstats` is that its counters are
cumulative since each shard last started. In a Redis Enterprise cluster the
shards do not restart together, so a capture routinely contains shards whose
counters cover wildly different windows. Summing raw counters across such a set
produces a number with no coherent meaning.

Everything here is therefore normalised by each shard's own
`uptime_in_seconds`, which makes shards comparable and yields real rates. Where
a quantity cannot be normalised -- percentiles, which are not additive -- the
spread across shards is reported instead of a fabricated aggregate.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

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


def is_write(command: str) -> bool:
    base = command.split('|', 1)[0].lower()
    return base.startswith(WRITE_PREFIXES)


def _fmt_dur(seconds: float) -> str:
    h = seconds / 3600
    if h < 48:
        return f'{h:,.1f}h'
    return f'{h/24:,.1f}d'


def detect_cohorts(per_shard: pd.DataFrame, split_ratio: float = 1.5) -> pd.DataFrame:
    """Group shards into uptime cohorts.

    Shards that restarted together share an uptime, so cohorts fall out of the
    gaps in the sorted uptime list rather than from any fixed threshold. A
    consecutive ratio above `split_ratio` is treated as a boundary: within a
    restart wave uptimes differ by seconds, between waves by hours or days.
    """
    ps = per_shard.sort_values('uptime_s').copy()
    ups = ps['uptime_s'].to_numpy(dtype=float)
    group, g = np.zeros(len(ups), dtype=int), 0
    for i in range(1, len(ups)):
        if ups[i-1] > 0 and ups[i] / ups[i-1] >= split_ratio:
            g += 1
        group[i] = g
    ps['cohort_id'] = group
    labels = {}
    for cid, chunk in ps.groupby('cohort_id'):
        med = float(chunk['uptime_s'].median())
        n = len(chunk)
        labels[cid] = f"up {_fmt_dur(med)} ({n} shard{'s' if n != 1 else ''})"
    ps['cohort'] = ps['cohort_id'].map(labels)
    # order: longest window first, which is the one reaching furthest back
    order = (ps.groupby('cohort')['uptime_s'].median()
               .sort_values(ascending=False).index.tolist())
    ps.attrs['cohort_order'] = order
    return ps


def build(info, cmd, lat, err, meta):
    """Compute every derived table the report needs."""
    meta = meta.copy()
    meta['uptime_s'] = pd.to_numeric(meta['uptime_in_seconds'], errors='coerce')
    meta['total_commands_processed'] = pd.to_numeric(meta['total_commands_processed'],
                                                     errors='coerce')
    meta['iops'] = pd.to_numeric(meta['instantaneous_ops_per_sec'], errors='coerce')
    meta['expired_keys'] = pd.to_numeric(meta['expired_keys'], errors='coerce')

    if meta['uptime_s'].isna().any() or (meta['uptime_s'] <= 0).any():
        bad = meta.loc[meta['uptime_s'].isna() | (meta['uptime_s'] <= 0), 'shard'].tolist()
        raise ValueError(f'shards {bad} have no usable uptime_in_seconds; '
                         'rate normalisation would be meaningless')

    up = meta.set_index('shard')['uptime_s']
    cmd = cmd.copy()
    cmd['uptime_s'] = cmd['shard'].map(up)
    cmd['rate'] = cmd['calls'] / cmd['uptime_s']
    cmd['cpu_frac'] = cmd['usec'] / (cmd['uptime_s'] * 1e6)
    cmd['is_write'] = cmd['command'].map(is_write)
    cmd['is_admin'] = cmd['command'].isin(ADMIN_COMMANDS)

    n_shards = int(meta['shard'].nunique())

    g = cmd.groupby('command')
    per_cmd = pd.DataFrame({
        'calls': g['calls'].sum(), 'usec': g['usec'].sum(),
        'rejected_calls': g['rejected_calls'].sum(), 'failed_calls': g['failed_calls'].sum(),
        'rate': g['rate'].sum(), 'cpu_frac': g['cpu_frac'].sum(),
        'shards': g['shard'].nunique(),
        'is_write': g['is_write'].first(), 'is_admin': g['is_admin'].first(),
    })
    per_cmd['avg_us'] = per_cmd['usec'] / per_cmd['calls']
    for src, dst in (('calls', 'calls_share'), ('usec', 'usec_share'),
                     ('rate', 'rate_share'), ('cpu_frac', 'cpu_share')):
        per_cmd[dst] = per_cmd[src] / per_cmd[src].sum()

    if not lat.empty:
        lg = lat.groupby('command')
        per_cmd = per_cmd.join(pd.DataFrame({
            'p50_med': lg['p50'].median(), 'p99_min': lg['p99'].min(),
            'p99_med': lg['p99'].median(), 'p99_max': lg['p99'].max(),
            'p999_med': lg['p99.9'].median(), 'p999_max': lg['p99.9'].max(),
            'lat_shards': lg['p99'].size(),
        }))
        per_cmd['tail_ratio'] = per_cmd['p99_med'] / per_cmd['avg_us']
    else:
        for c in ('p50_med', 'p99_min', 'p99_med', 'p99_max', 'p999_med',
                  'p999_max', 'lat_shards', 'tail_ratio'):
            per_cmd[c] = np.nan

    sg = cmd.groupby(['shard', 'node'])
    per_shard = pd.DataFrame({
        'calls': sg['calls'].sum(), 'usec': sg['usec'].sum(),
        'rate': sg['rate'].sum(), 'cpu_frac': sg['cpu_frac'].sum(),
        'commands': sg['command'].nunique(),
    }).reset_index().merge(
        meta[['shard', 'uptime_s', 'total_commands_processed', 'iops',
              'expired_keys', 'run_id']], on='shard', how='left')
    per_shard['uptime_h'] = per_shard['uptime_s'] / 3600
    per_shard['tcp_rate'] = per_shard['total_commands_processed'] / per_shard['uptime_s']
    per_shard['rate_share'] = per_shard['rate'] / per_shard['rate'].sum()
    per_shard = detect_cohorts(per_shard)
    cohort_order = per_shard.attrs['cohort_order']

    # cohort x command, as a per-shard mean so unequal cohorts compare fairly
    r = cmd.merge(per_shard[['shard', 'cohort']], on='shard')
    coh_tot = (r.pivot_table(index='command', columns='cohort', values='rate', aggfunc='sum')
                .reindex(columns=cohort_order).fillna(0.0))
    coh_n = per_shard.groupby('cohort')['shard'].count().reindex(cohort_order)
    cohort = coh_tot.div(coh_n, axis=1)
    cohort.attrs['sizes'] = coh_n.to_dict()

    s = per_cmd['rate'].sort_values(ascending=False)
    cum = s.cumsum() / s.sum()
    conc = dict(
        commands=int(len(s)),
        top1=float(cum.iloc[0] * 100),
        top5=float(cum.iloc[min(4, len(s)-1)] * 100),
        top10=float(cum.iloc[min(9, len(s)-1)] * 100),
        n_for_50=int((cum < 0.50).sum() + 1),
        n_for_90=int((cum < 0.90).sum() + 1),
        n_for_99=int((cum < 0.99).sum() + 1),
    )

    # per-shard rate matrix, for skew
    skew = cmd.pivot_table(index='command', columns='shard', values='rate', aggfunc='sum')

    totals = dict(
        calls=int(per_cmd['calls'].sum()), usec=int(per_cmd['usec'].sum()),
        rate=float(per_cmd['rate'].sum()), cores=float(per_cmd['cpu_frac'].sum()),
        iops=float(per_shard['iops'].sum(skipna=True)),
        tcp_rate=float(per_shard['tcp_rate'].sum(skipna=True)),
        rejected=int(per_cmd['rejected_calls'].sum()),
        failed=int(per_cmd['failed_calls'].sum()),
        n_shards=n_shards, n_commands=int(len(per_cmd)),
        uptime_min=float(per_shard['uptime_s'].min()),
        uptime_max=float(per_shard['uptime_s'].max()),
        uptime_spread=float(per_shard['uptime_s'].max() / per_shard['uptime_s'].min()),
        cmdstat_rows=int(len(cmd)), lat_rows=int(len(lat)),
        n_cohorts=len(cohort_order),
    )
    totals['window_uniform'] = totals['uptime_spread'] < 1.25

    errors = (err.groupby('error')['count'].sum().sort_values(ascending=False).to_frame('count')
              if not err.empty else pd.DataFrame({'count': []}))
    errors.index.name = 'error'

    return dict(info=info, cmd=cmd, lat=lat, err=err, meta=meta,
                per_cmd=per_cmd, per_shard=per_shard, cohort=cohort,
                cohort_order=cohort_order, conc=conc, skew=skew,
                totals=totals, errors=errors,
                generated_at=info['generated_at'])


def reconcile_errors(D):
    """Errorstats and Commandstats come from different code paths, so agreement
    between them is a real check on the parse. Returns (matched, explanation)."""
    e = D['errors']
    t = D['totals']
    if e.empty:
        return None, 'no Errorstats section present'
    total = int(e['count'].sum())
    both = t['rejected'] + t['failed']
    if total == both:
        return True, (f"{len(e)} error kinds totalling {total:,} match "
                      f"rejected_calls ({t['rejected']:,}) + failed_calls ({t['failed']:,})")
    return False, (f"Errorstats totals {total:,} but rejected_calls + failed_calls = {both:,} "
                   f"({t['rejected']:,} + {t['failed']:,})")


def cohort_write_gap(D):
    """Compare write-command intensity between the longest- and shortest-window
    cohorts. A large gap with near-identical rates inside the long cohort means
    the writes are historical rather than unevenly distributed by key."""
    coh, order = D['cohort'], D['cohort_order']
    if len(order) < 2:
        return None
    writes = [c for c in coh.index if is_write(c)]
    if not writes:
        return None
    long_c, short_c = order[0], order[-1]
    a = float(coh.loc[writes, long_c].sum())
    b = float(coh.loc[writes, short_c].sum())
    # how tightly the long cohort agrees with itself on its biggest writer
    biggest = coh.loc[writes, long_c].idxmax()
    per_shard = D['cmd'][D['cmd'].command == biggest].merge(
        D['per_shard'][['shard', 'cohort']], on='shard')
    inside = per_shard[per_shard.cohort == long_c]['rate']
    spread = float(inside.max() / inside.min()) if len(inside) > 1 and inside.min() > 0 else np.nan
    return dict(long_cohort=long_c, short_cohort=short_c, long_rate=a, short_rate=b,
                ratio=(a / b if b > 0 else np.inf), biggest=biggest,
                n_long=int(len(inside)), inside_spread=spread)


def key_skew(D, min_rate=1.0, min_ratio=5.0):
    """Commands whose per-shard rate varies sharply *within* one cohort.

    Confining the comparison to a single cohort is what separates hot keys from
    the measurement-window effect: shards in one cohort share a window, so a
    difference between them is a real difference in traffic.
    """
    out = []
    ps = D['per_shard']
    for coh, chunk in ps.groupby('cohort'):
        if len(chunk) < 3:
            continue
        sub = D['cmd'][D['cmd'].shard.isin(chunk.shard)]
        piv = sub.pivot_table(index='command', columns='shard', values='rate', aggfunc='sum')
        for command, row in piv.iterrows():
            vals = row.dropna()
            if len(vals) < 2 or vals.max() < min_rate:
                continue
            floor = max(vals.min(), vals.max() / 1e6)
            ratio = float(vals.max() / floor) if floor > 0 else np.inf
            if ratio >= min_ratio:
                out.append(dict(command=command, cohort=coh, top_shard=int(vals.idxmax()),
                                top_rate=float(vals.max()), median_rate=float(vals.median()),
                                ratio=ratio, shards_present=int(len(vals)),
                                shards_in_cohort=int(len(chunk))))
    df = pd.DataFrame(out)
    return df.sort_values('top_rate', ascending=False) if not df.empty else df
