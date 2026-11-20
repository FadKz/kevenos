"""Disposable synthetic SQLite comparison; not application or migration code."""
import json
import sqlite3
import statistics
import tempfile
import time
from pathlib import Path


def percentile(values, fraction):
    return sorted(values)[int(len(values) * fraction) - 1]


def run(mode, path):
    db = sqlite3.connect(path)
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('CREATE TABLE kv(k TEXT PRIMARY KEY, v TEXT NOT NULL)')
    db.execute('CREATE TABLE responses(run_id TEXT, question_id TEXT, principal TEXT, choice INTEGER, receipt TEXT, PRIMARY KEY(run_id,question_id,principal))')
    with db:
        db.execute('INSERT INTO kv VALUES (?,?)', ('state', json.dumps({'responses':{}})))
    timings = []
    for index in range(1000):
        question, principal = f'q{index // 100}', f'guest{index % 100}'
        receipt = f'receipt{index}'
        start = time.perf_counter()
        with db:
            if mode == 'embedded':
                state = json.loads(db.execute('SELECT v FROM kv WHERE k=?', ('state',)).fetchone()[0])
                state['responses'].setdefault(question, {})[principal] = {'choice':index % 2, 'receipt':receipt}
                db.execute('UPDATE kv SET v=? WHERE k=?', (json.dumps(state), 'state'))
            else:
                db.execute('INSERT INTO responses VALUES(?,?,?,?,?) ON CONFLICT DO NOTHING', ('run',question,principal,index % 2,receipt))
        timings.append((time.perf_counter() - start)*1000)
    if mode == 'embedded':
        state = json.loads(db.execute('SELECT v FROM kv WHERE k=?', ('state',)).fetchone()[0])
        rows = sum(len(items) for items in state['responses'].values())
    else:
        rows = db.execute('SELECT count(*) FROM responses').fetchone()[0]
        with db:
            db.execute('INSERT INTO responses VALUES(?,?,?,?,?) ON CONFLICT DO NOTHING', ('run','q0','guest0',1,'different'))
        assert db.execute('SELECT choice,receipt FROM responses WHERE run_id=? AND question_id=? AND principal=?',('run','q0','guest0')).fetchone() == (0,'receipt0')
    assert rows == 1000
    result = {'mode':mode, 'count':rows, 'p50_ms':round(statistics.median(timings),4),
              'p95_ms':round(percentile(timings,.95),4), 'last_100_p95_ms':round(percentile(timings[-100:],.95),4)}
    db.close()
    return result


if __name__ == '__main__':
    with tempfile.TemporaryDirectory(prefix='kevenos-response-spike-') as temp:
        for mode in ('embedded','rows'):
            print(json.dumps(run(mode, str(Path(temp) / (mode+'.db')))))
