"""Refuse an incompatible database before loading any application module.

For guarded rollback mount this same guard into the old image and invoke it;
never run an older server directly against a migrated event volume.
"""
import ast
import json
import os
from pathlib import Path
import sqlite3
import sys

import database


def check(source: Path, data: Path) -> None:
    tree=ast.parse(source.read_text())
    versions=[n.value.value for n in tree.body if isinstance(n,ast.Assign)
              and any(isinstance(t,ast.Name) and t.id=='SCHEMA_VERSION' for t in n.targets)
              and isinstance(n.value,ast.Constant) and type(n.value.value) is int]
    if len(versions)!=1:raise RuntimeError('Cannot verify application schema compatibility.')
    if database.resolve_backend().server:
        return
    for path in (data/'judging.db', data/'rehearsal'/'judging.db'):
        if not path.exists():continue
        with sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='kv'").fetchone():continue
            row=db.execute("SELECT v FROM kv WHERE k='schema_version'").fetchone()
            if row and int(json.loads(row[0]))>versions[0]:
                raise RuntimeError('Database requires a newer application. Restore the matching backup before rollback.')


if __name__=='__main__':
    source=Path(os.environ.get('KEVENOS_SERVER_SOURCE','/app/server.py'))
    try:check(source,Path(os.environ.get('KEVENOS_DATA','/data')))
    except (OSError,ValueError,RuntimeError,sqlite3.Error) as error:
        print(str(error),file=sys.stderr);sys.exit(1)
    os.execv(sys.executable,[sys.executable,str(source),*sys.argv[1:]])
