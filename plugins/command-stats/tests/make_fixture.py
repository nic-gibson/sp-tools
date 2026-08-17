#!/usr/bin/env python3
"""Fabricate a debuginfo-shaped support package to exercise the pipeline.

Deliberately contains every condition the report is supposed to detect:
  * two databases, so database scoping is actually tested
  * masters and replicas, so the role filter is actually tested
  * two uptime cohorts (3 shards up ~6d, 2 up ~8h) -> cohort detection
  * writes present only in the long cohort -> write-workload-stopped finding
  * one command 40x hotter on one shard inside a cohort -> key skew
  * admin commands dominating CPU -> observability-overhead finding
  * p99 well above the mean on some commands -> tail finding
  * errorstats that sum exactly to rejected+failed -> reconcile PASS
  * total_commands_processed exactly equal to summed calls -> rate check PASS
"""
import os
import random
import shutil
import sys
import tarfile

random.seed(20260817)
OUT = next((a for a in sys.argv[1:] if not a.startswith('-')), None) or 'debuginfo.20260817-101500.tar.gz'
STAGE = os.path.splitext(os.path.splitext(OUT)[0])[0] + '.d'
STAMP = '2026-08-17 10:15:00.123456+00:00'

LONG, SHORT = 6 * 86400 + 3600, 8 * 3600 + 240

# shard -> (node, db_id, role, uptime)
SHARDS = {
    1: (1, 4, 'master', LONG),        3: (2, 4, 'slave', LONG),
    2: (1, 4, 'master', LONG + 17),   4: (2, 4, 'slave', LONG + 9),
    5: (2, 4, 'master', LONG - 22),   6: (1, 4, 'slave', LONG - 40),
    7: (1, 4, 'master', SHORT),       9: (2, 4, 'slave', SHORT + 6),
    8: (2, 4, 'master', SHORT + 11), 10: (1, 4, 'slave', SHORT - 3),
    11: (1, 1, 'master', 300000),    12: (2, 1, 'slave', 300000),
}

# command -> (calls/sec, mean usec, is_write, tail_multiplier)
PROFILE = {
    'get':          (240.0,   3.1,  False, 2.4),
    'mget':         (18.0,   14.0,  False, 6.5),
    'exists':       (31.0,    2.4,  False, 2.0),
    'ttl':          (4.4,     2.1,  False, 1.9),
    'hgetall':      (12.5,   21.0,  False, 8.0),
    'zrangebyscore': (2.2,   47.0,  False, 11.0),
    'eval':         (0.9,   410.0,  False, 3.2),
    'json.get':     (1.4,    88.0,  False, 4.0),
    'set':          (95.0,    4.2,  True,  2.6),
    'setex':        (6.0,     4.6,  True,  2.2),
    'hset':         (22.0,    5.8,  True,  3.0),
    'del':          (7.5,     3.3,  True,  2.1),
    'expire':       (3.0,     2.6,  True,  1.8),
    'json.set':     (0.8,   120.0,  True,  3.6),
    'ts.add':       (2.6,    18.0,  True,  2.4),
    'info':         (0.42, 2850.0,  False, 1.6),
    'ping':         (2.10,    1.1,  False, 1.4),
    'config|get':   (0.35, 1180.0,  False, 1.5),
    'client|list':  (0.10, 3400.0,  False, 1.7),
    'cluster|info': (0.28,  240.0,  False, 1.5),
    'dbsize':       (0.14,   66.0,  False, 1.5),
    'slowlog|get':  (0.07,  520.0,  False, 1.6),
    'memory|usage': (0.05,  180.0,  False, 2.2),
    'auth':         (0.90,   12.0,  False, 1.3),
    'select':       (0.90,    0.9,  False, 1.2),
    'scan':         (0.30,  920.0,  False, 5.0),
    # Costly, but deliberately not costly enough to push admin CPU under its 10%
    # threshold -- the fixture has to keep both findings reachable at once.
    '_ft.aggregate': (1.1, 5000.0, False, 2.0),
    'ft.aggregate': (0.45, 9000.0, False, 2.0),
    'unlink':       (0.20,    3.0,  True,  2.0),
    'pfadd':        (0.05,    4.4,  True,  1.9),
}
HOT = 'zrangebyscore'   # made 40x hotter on redis:5, inside the long cohort

# Per-command failure rates. `_ft.aggregate` reproduces a pattern seen in a real
# support package: an expensive fan-out query failing on almost every call, so
# most of its considerable CPU cost buys nothing.
FAIL_RATE = {
    '_ft.aggregate': 0.98,
    'hset': 0.00002,
    'json.set': 0.00002,
    'eval': 0.04,
}

# Error replies per failed call. A fan-out query collects an error from every
# shard it touched, so Errorstats (which counts replies) runs far ahead of
# failed_calls (which counts calls) -- the two must not be compared directly.
REPLY_FAN = 63


def shard_rows(sid):
    node, db_id, role, up = SHARDS[sid]
    long_cohort = up > 86400
    rows = []
    for cmd, (rate, mean_us, wr, tail) in PROFILE.items():
        r = rate
        if db_id == 1:
            r *= 0.05
        if wr and not long_cohort:
            r *= 0.004          # writes all but stopped in the recent window
        if role == 'slave':
            r *= 0.62 if not wr else 1.0
        if cmd == HOT and sid == 5:
            r *= 40.0
        r *= random.uniform(0.97, 1.03)
        calls = max(1, int(r * up))
        usec = int(calls * mean_us * random.uniform(0.96, 1.04))
        rejected = int(calls * 0.00004) if cmd in ('get', 'set', 'eval') else 0
        failed = int(calls * FAIL_RATE.get(cmd, 0.0))
        p50 = mean_us * 0.72
        p99 = mean_us * tail * (3.1 if (cmd == 'mget' and sid == 2) else 1.0)
        rows.append(dict(cmd=cmd, calls=calls, usec=usec, rejected=rejected,
                         failed=failed, p50=p50, p99=p99, p999=p99 * 2.3))
    return rows


def write_shard(path, sid):
    node, db_id, role, up = SHARDS[sid]
    rows = shard_rows(sid)
    total_calls = sum(r['calls'] for r in rows)
    rej = sum(r['rejected'] for r in rows)
    fail = sum(r['failed'] for r in rows)
    with open(path, 'w', encoding='utf-8') as fh:
        fh.write('# Server\n')
        fh.write('redis_version:7.4.0\n')
        fh.write(f'run_id:{"%040x" % random.getrandbits(160)}\n')
        fh.write(f'process_id:{2000 + sid}\n')
        fh.write(f'uptime_in_seconds:{up}\n')
        fh.write(f'uptime_in_days:{up // 86400}\n')
        fh.write('\n# Clients\n')
        fh.write(f'connected_clients:{40 + sid}\n')
        fh.write('\n# Memory\n')
        fh.write(f'used_memory:{180_000_000 + sid * 1_000_000}\n')
        fh.write('\n# Stats\n')
        # exactly the summed calls, so the independent-field check can pass
        fh.write(f'total_commands_processed:{total_calls}\n')
        fh.write(f'instantaneous_ops_per_sec:{int(total_calls / up * 0.42)}\n')
        fh.write(f'expired_keys:{int(up * 0.31)}\n')
        fh.write(f'evicted_keys:0\n')
        # Errorstats must reconcile against this, not against failed_calls.
        fh.write(f'total_error_replies:{(rej + fail) * REPLY_FAN}\n')
        fh.write(f'total_net_input_bytes:{total_calls * 64}\n')
        fh.write(f'total_net_output_bytes:{total_calls * 128}\n')
        fh.write('\n# Commandstats\n')
        for r in rows:
            fh.write(f"cmdstat_{r['cmd']}:calls={r['calls']},usec={r['usec']},"
                     f"usec_per_call={r['usec'] / r['calls']:.2f},"
                     f"rejected_calls={r['rejected']},failed_calls={r['failed']}\n")
        fh.write('\n# Latencystats\n')
        for r in rows:
            fh.write(f"latency_percentiles_usec_{r['cmd']}:p50={r['p50']:.3f},"
                     f"p99={r['p99']:.3f},p99.9={r['p999']:.3f}\n")
        fh.write('\n# Errorstats\n')
        # split across two kinds summing exactly to total_error_replies
        replies = (rej + fail) * REPLY_FAN
        a = replies // 2
        fh.write(f'errorstat_ERR:count={a}\n')
        fh.write(f'errorstat_WRONGTYPE:count={replies - a}\n')
        fh.write('\n# Keyspace\n')
        fh.write(f'db0:keys={100000 + sid * 500},expires=12,avg_ttl=0\n')


# Two rladmin layouts, because the tables gain columns between Redis Enterprise
# versions and that is exactly what broke the original positional parser.
#   modern (8.x): MODULE sits between TYPE and STATUS, and EXEC_STATE,
#                 BACKUP_PROGRESS and REDIS_VERSION trail the endpoint
#   legacy:       no MODULE, no trailing columns, and no header row at all
DB_ROWS = [
    dict(db_id=1, name='cache-1', shards=2, memory='1GB', placement='dense',
         replication='disabled', persistence='disabled', port=12000),
    dict(db_id=4, name='pers-3950', shards=10, memory='25GB', placement='sparse',
         replication='enabled', persistence='aof', port=12004),
]

MODERN_DB_HEADER = ('DB:ID NAME TYPE MODULE STATUS SHARDS MEMORY_SIZE PLACEMENT '
                    'REPLICATION PERSISTENCE ENDPOINT CRDB EXEC_STATE '
                    'EXEC_STATE_MACHINE BACKUP_PROGRESS MISSING_BACKUP_TIME '
                    'REDIS_VERSION')
LEGACY_DB_HEADER = ('DB:ID NAME TYPE STATUS SHARDS MEMORY_SIZE PLACEMENT '
                    'REPLICATION PERSISTENCE ENDPOINT')
MODERN_SH_HEADER = ('DB:ID NAME ID NODE ROLE SLOTS USED_MEMORY RAM_FRAG '
                    'WATCHDOG_STATUS STATUS')
LEGACY_SH_HEADER = 'DB:ID NAME ID NODE ROLE SLOTS USED_MEMORY BACKUP'


def db_line(d, modern):
    ep = f"redis-{d['port']}.rec.cluster.local:{d['port']}"
    cols = [f"db:{d['db_id']}", d['name'], 'redis']
    if modern:
        cols.append('yes')
    cols += ['active', str(d['shards']), d['memory'], d['placement'],
             d['replication'], d['persistence'], ep]
    if modern:
        cols += ['no', 'N/A', 'N/A', 'N/A', 'N/A', '8.4.0']
    return '  '.join(cols)


def shard_line(sid, node, db_id, role, modern):
    name = 'pers-3950' if db_id == 4 else 'cache-1'
    cols = [f'db:{db_id}', name, f'redis:{sid}', f'node:{node}', role, '0-8191',
            f'{180 + sid}GB']
    cols += ['251.9MB', 'OK', 'OK'] if modern else ['OK']
    return '  '.join(cols)


def write_rladmin(path, node, modern=True, headers=True):
    with open(path, 'w', encoding='utf-8') as fh:
        fh.write('Redis Enterprise Node Information\n')
        fh.write(STAMP + '\n')
        fh.write('\n' + '-' * 60 + '\n')
        fh.write('rladmin status extra all:\n')
        fh.write('CLUSTER:\n')
        fh.write('OK. Cluster master: 1 (10.0.0.11)\n')
        fh.write('\nCLUSTER NODES:\n')
        fh.write('NODE:ID ROLE   ADDRESS    HOSTNAME SHARDS CORES STATUS\n')
        fh.write('*node:1 master 10.0.0.11  rec-0    6/100  64    OK\n')
        fh.write('node:2  slave  10.0.0.12  rec-1    6/100  64    OK\n')
        fh.write('\nDATABASES:\n')
        if headers:
            fh.write((MODERN_DB_HEADER if modern else LEGACY_DB_HEADER) + '\n')
        for d in DB_ROWS:
            fh.write(db_line(d, modern) + '\n')
        fh.write('\nENDPOINTS:\n')
        fh.write('DB:ID NAME      ID           NODE   ROLE              SSL\n')
        fh.write('db:4  pers-3950 endpoint:4:1 node:1 all-master-shards No\n')
        fh.write('\nSHARDS:\n')
        if headers:
            fh.write((MODERN_SH_HEADER if modern else LEGACY_SH_HEADER) + '\n')
        for sid, (nd, db_id, role, up) in sorted(SHARDS.items()):
            fh.write(shard_line(sid, nd, db_id, role, modern) + '\n')


def main():
    # --legacy emits the pre-MODULE layout with no header rows at all, which is
    # what exercises the regex fallback rather than the header-driven path.
    legacy = '--legacy' in sys.argv[1:]
    if os.path.isdir(STAGE):
        shutil.rmtree(STAGE)
    for node in (1, 2):
        d = os.path.join(STAGE, f'node_{node}')
        os.makedirs(d)
        write_rladmin(os.path.join(d, f'node_{node}.rladmin'), node,
                      modern=not legacy, headers=not legacy)
        # a decoy large file, to prove only what is needed gets extracted
        with open(os.path.join(d, 'ccs.json'), 'w') as fh:
            fh.write('{"noise": "' + 'x' * 200000 + '"}')
    for sid, (node, db_id, role, up) in SHARDS.items():
        write_shard(os.path.join(STAGE, f'node_{node}', f'redis_{sid}.txt'), sid)
    with tarfile.open(OUT, 'w:gz') as tf:
        for root, _, files in os.walk(STAGE):
            for name in sorted(files):
                p = os.path.join(root, name)
                tf.add(p, arcname=os.path.relpath(p, STAGE))
    print(f'wrote {OUT} ({os.path.getsize(OUT):,} bytes) and {STAGE}/')


if __name__ == '__main__':
    main()
