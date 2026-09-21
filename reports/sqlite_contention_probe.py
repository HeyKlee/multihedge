"""Isolated contention probe. Never writes to the production database."""
import concurrent.futures
import json
import multiprocessing
from pathlib import Path
import sqlite3
import tempfile
import time
import traceback


def worker(args):
    path, role, seconds = args
    import mh_ui
    errors, completed = [], 0
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        con = None
        try:
            if role == 'reader':
                mh_ui.list_widget_issues(20, 'new', path=path)
            else:
                con = sqlite3.connect(path, timeout=0.1)
                con.execute('BEGIN IMMEDIATE')
                con.execute('UPDATE contention_probe SET value=value+1')
                time.sleep(0.01)
                con.commit()
            completed += 1
        except sqlite3.OperationalError as exc:
            if len(errors) < 3:
                errors.append({'type': type(exc).__name__, 'message': str(exc), 'traceback': traceback.format_exc()})
        finally:
            if con is not None:
                con.close()
    return {'role': role, 'completed': completed, 'first_errors': errors}


if __name__ == '__main__':
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import mh_ui
    with tempfile.TemporaryDirectory(prefix='mh-contention-') as folder:
        db = str(Path(folder) / 'copy.db')
        source = sqlite3.connect('file:deploy/data/multihedge.db?mode=ro', uri=True, timeout=2)
        target = sqlite3.connect(db)
        start = time.monotonic()
        def progress(*_):
            if time.monotonic() - start > 30:
                raise TimeoutError('snapshot deadline exceeded')
        source.backup(target, pages=256, progress=progress)
        source.close()
        target.execute('CREATE TABLE contention_probe(value INTEGER)')
        target.execute('INSERT INTO contention_probe VALUES(0)')
        target.commit()
        target.close()
        mh_ui.list_widget_issues(path=db)
        with concurrent.futures.ProcessPoolExecutor(max_workers=6, mp_context=multiprocessing.get_context('fork')) as pool:
            results = list(pool.map(worker, [(db, role, 30) for role in ['writer']*4 + ['reader']*2]))
        print(json.dumps(results, indent=2))
