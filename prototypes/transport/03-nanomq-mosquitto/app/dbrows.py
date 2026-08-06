"""Row counts of NanoMQ's bridge-cache SQLite database, mounted read-only at /data.

Run against the `edge-data` volume by run-experiment.sh. This is the direct
evidence for whether the disk cache is actually holding anything: `t_client_msg`
is where queued bridge messages would live.

Read-only + WAL: the broker normally holds the database open while this runs, so
the connection is opened with mode=ro and the -wal/-shm files are left alone.
"""

from __future__ import annotations

import glob
import os
import sqlite3

DB = "/data/mqtt_client.db"


def main() -> None:
    files = sorted(os.path.basename(p) for p in glob.glob("/data/*"))
    if not os.path.exists(DB):
        print(f"sin mqtt_client.db; contenido de mounted_file_path: {files or '(vacio)'}")
        return
    try:
        conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        tables = [name for (name,) in conn.execute(
            "select name from sqlite_master where type = 'table'")]
        counts = {}
        for name in tables:
            counts[name] = conn.execute(f'select count(*) from "{name}"').fetchone()[0]
        print(f"ficheros={files} filas={counts}")
    except sqlite3.Error as exc:
        print(f"mqtt_client.db presente pero ilegible: {exc}")


if __name__ == "__main__":
    main()
