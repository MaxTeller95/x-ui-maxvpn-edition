#!/usr/bin/env python3
"""xuidb - the x-ui panel's database, SQLite or PostgreSQL, found the way x-ui finds it.

Every tool here reads and writes the panel's settings, inbounds and clients. 3x-ui 3.x keeps them
in SQLite (/etc/x-ui/x-ui.db) or, with XUI_DB_TYPE=postgres and XUI_DB_DSN, in PostgreSQL. What
counts is what the running panel was started with, so that is read first: the environment of the
x-ui process, then the service's Environment= / EnvironmentFile= (/etc/default/x-ui), then the
SQLite default. XUI_DB, XUI_DB_TYPE, XUI_DB_DSN and XUI_DB_FOLDER in the caller's environment win.

    import xuidb
    c = xuidb.connect()               # sqlite3-style: execute(sql, params) with ?, commit(), close()
    c.row_factory = xuidb.Row          # rows by name as well as by position
    xuidb.backup_to(folder)            # x-ui.db, or a pg_dump of the whole database
    xuidb.undo_hint(folder)            # the command that puts that backup back

PostgreSQL is reached with psql (no Python driver needed). Reads run at once; writes wait for
commit() and are sent together in one transaction, as SQLite's implicit transaction would.

As a command (installed as xui-db):
    xui-db detect                      where the panel keeps its data
    xui-db backup FOLDER               back it up into FOLDER
    xui-db restore FILE                put a backup back (stops and starts x-ui)
"""
import json, os, re, shutil, sqlite3, subprocess, sys, time, urllib.parse

SERVICE = "x-ui"
KEYS = ("XUI_DB_TYPE", "XUI_DB_DSN", "XUI_DB_FOLDER")
PG_DUMP = "x-ui.pgdump"


class PgError(Exception):
    pass


Error = (sqlite3.Error, PgError)


# ------------------------------------------------------------------ where the data is
def _parse_env(text, sep="\n"):
    env = {}
    for line in text.split(sep):
        m = re.match(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$", line)
        if m:
            env[m.group(1)] = m.group(2).strip("'\"")
    return env


def _pid():
    try:
        r = subprocess.run(["systemctl", "show", "-p", "MainPID", "--value", SERVICE],
                           capture_output=True, text=True, timeout=10)
        if r.stdout.strip().isdigit() and r.stdout.strip() != "0":
            return r.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    for pid in filter(str.isdigit, os.listdir("/proc")):
        try:
            if os.path.basename(os.readlink("/proc/%s/exe" % pid)) == "x-ui":
                return pid
        except OSError:
            pass
    return None


def _service_env():
    env, out = {}, ""
    try:
        out = subprocess.run(["systemctl", "show", "-p", "EnvironmentFiles", "-p", "Environment", SERVICE],
                             capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        pass
    files = [l[17:].split(" ")[0].lstrip("-") for l in out.splitlines() if l.startswith("EnvironmentFiles=")]
    for f in files + ["/etc/default/x-ui"]:
        try:
            env.update({k: v for k, v in _parse_env(open(f).read()).items() if k in KEYS and k not in env})
        except OSError:
            pass
    for l in out.splitlines():
        if l.startswith("Environment="):
            for item in re.findall(r'(?:"[^"]*"|\S)+', l[12:]):
                env.update({k: v for k, v in _parse_env(item.strip('"')).items() if k in KEYS})
    return env


_found = None


def detect():
    """{"kind": "sqlite"|"postgres", "path", "dsn", "source", "running"} as the panel sees it."""
    global _found
    if _found:
        return _found
    env, source, pid = {}, "default", _pid()
    if pid:
        try:
            raw = open("/proc/%s/environ" % pid, "rb").read().decode("utf-8", "replace")
            env = {k: v for k, v in _parse_env(raw, "\0").items() if k in KEYS}
            source = "running x-ui"
        except OSError:
            pid = None
    if not pid:
        env = _service_env()
        source = "x-ui service settings" if env else "default"
    env.update({k: os.environ[k] for k in KEYS if os.environ.get(k)})
    kind = "postgres" if env.get("XUI_DB_TYPE", "").strip().lower() in ("postgres", "postgresql", "pg") else "sqlite"
    path = os.environ.get("XUI_DB") or "%s/x-ui.db" % (env.get("XUI_DB_FOLDER") or "/etc/x-ui").rstrip("/")
    _found = {"kind": kind, "path": path, "dsn": env.get("XUI_DB_DSN", "").strip(), "source": source,
              "running": bool(pid)}
    return _found


def kind():
    return detect()["kind"]


def describe():
    d = detect()
    where = re.sub(r"//[^@/]*@", "//", d["dsn"]) if d["kind"] == "postgres" else d["path"]    # no password
    return "%s %s (from %s)" % (d["kind"], where, d["source"])


def _pg_env(dsn):
    """libpq settings from the DSN, so the password never lands on a command line."""
    if not dsn:
        raise PgError("x-ui is set to PostgreSQL but XUI_DB_DSN is empty")
    if "://" not in dsn:                                       # key=value form
        kv = dict(re.findall(r"(\w+)\s*=\s*('(?:[^'\\]|\\.)*'|\S+)", dsn))
        kv = {k: v.strip("'") for k, v in kv.items()}
        m = {"host": "PGHOST", "port": "PGPORT", "user": "PGUSER", "password": "PGPASSWORD",
             "dbname": "PGDATABASE", "sslmode": "PGSSLMODE"}
        return dict(os.environ, PGCONNECT_TIMEOUT="10", **{m[k]: v for k, v in kv.items() if k in m})
    u = urllib.parse.urlsplit(dsn)
    q = dict(urllib.parse.parse_qsl(u.query))
    return dict(os.environ, PGHOST=u.hostname or "127.0.0.1", PGPORT=str(u.port or 5432),
                PGUSER=urllib.parse.unquote(u.username or ""), PGPASSWORD=urllib.parse.unquote(u.password or ""),
                PGDATABASE=urllib.parse.unquote(u.path.lstrip("/")), PGSSLMODE=q.get("sslmode", "prefer"),
                PGCONNECT_TIMEOUT="10")


# ------------------------------------------------------------------ PostgreSQL, sqlite3-style
class Row(sqlite3.Row):
    """Rows by name and by position: sqlite3.Row for SQLite, _PgRow for PostgreSQL."""


class _PgRow(tuple):
    def __new__(cls, pairs):
        r = tuple.__new__(cls, [v for _, v in pairs])
        r._keys = [k for k, _ in pairs]
        return r

    def keys(self):
        return list(self._keys)

    def __getitem__(self, i):
        if isinstance(i, str):
            return tuple.__getitem__(self, self._keys.index(i))
        return tuple.__getitem__(self, i)


class _Cursor:
    def __init__(self, rows, rowcount=-1):
        self.rows, self.rowcount, self.i = rows, rowcount, 0

    def fetchone(self):
        if self.i < len(self.rows):
            self.i += 1
            return self.rows[self.i - 1]
        return None

    def fetchall(self):
        out, self.i = self.rows[self.i:], len(self.rows)
        return out

    def __iter__(self):
        return iter(self.fetchall())


def _lit(v):
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, bytes):
        v = v.decode("utf-8")
    return "'" + str(v).replace("'", "''") + "'"


def _bind(sql, params):
    """Put ? placeholders' values in, skipping quoted text."""
    if not params:
        return sql
    out, it, q = [], iter(params), None
    for ch in sql:
        if q:
            q = None if ch == q else q
            out.append(ch)
        elif ch in "'\"":
            q = ch
            out.append(ch)
        elif ch == "?":
            out.append(_lit(next(it)))
        else:
            out.append(ch)
    return "".join(out)


class PgConnection:
    def __init__(self, dsn):
        if not shutil.which("psql"):
            raise PgError("the panel uses PostgreSQL and psql is missing (apt install postgresql-client)")
        self.env = _pg_env(dsn)
        self.pending = []
        self.row_factory = None

    def _psql(self, script):
        r = subprocess.run(["psql", "-X", "-q", "-At", "-v", "ON_ERROR_STOP=1", "-f", "-"], input=script,
                           capture_output=True, text=True, env=self.env, timeout=120)
        if r.returncode:
            raise PgError((r.stderr.strip().splitlines() or ["psql failed"])[-1])
        return r.stdout

    def execute(self, sql, params=()):
        s = _bind(sql, params).strip().rstrip(";")
        low = s.lower()
        m = re.match(r"pragma\s+table_info\s*\(\s*[\"'`]?(\w+)", low)
        if m:                                                    # SQLite's column list
            s = ("select ordinal_position - 1 as cid, column_name as name, data_type as type, 0 as notnull, "
                 "null as dflt_value, 0 as pk from information_schema.columns where table_schema = current_schema() "
                 "and table_name = %s order by ordinal_position" % _lit(m.group(1)))
            low = "select"
        if re.match(r"begin(\s+(immediate|deferred|exclusive|transaction))?$", low):
            return _Cursor([])                                   # commit() makes the transaction
        if low.startswith(("select", "with", "values")) and not re.search(r"\b(update|insert|delete)\b", low.split("select", 1)[0]):
            out = self._psql("select row_to_json(q) from (%s) q;" % s)
            rows = [json.loads(l, object_pairs_hook=list) for l in out.splitlines() if l.strip()]
            wrap = _PgRow if self.row_factory else (lambda pairs: tuple(v for _, v in pairs))
            return _Cursor([wrap(p) for p in rows])
        self.pending.append(s)
        return _Cursor([], -1)

    def executemany(self, sql, seq):
        for p in seq:
            self.execute(sql, p)
        return _Cursor([], -1)

    def commit(self):
        if self.pending:
            script = "begin;\n" + ";\n".join(self.pending) + ";\ncommit;\n"
            self.pending = []
            self._psql(script)

    def rollback(self):
        self.pending = []

    def close(self):
        self.pending = []

    def __enter__(self):
        return self

    def __exit__(self, typ, *_):
        if typ is None:
            self.commit()
        else:
            self.rollback()
        return False


def connect(path=None):
    """The panel's database. `path` forces a SQLite file (a tool's --db)."""
    d = detect()
    if path is None and d["kind"] == "postgres":
        return PgConnection(d["dsn"])
    p = path or d["path"]
    if not os.path.isfile(p):
        raise sqlite3.OperationalError("no x-ui database at %s" % p)
    c = sqlite3.connect(p)
    return c


def exists():
    d = detect()
    return d["kind"] == "postgres" or os.path.isfile(d["path"])


# ------------------------------------------------------------------ backups
def backup_to(folder):
    """Copy of the whole database into `folder`; returns the file written."""
    os.makedirs(folder, exist_ok=True)
    d = detect()
    if d["kind"] == "sqlite":
        dest = os.path.join(folder, os.path.basename(d["path"]))
        src = sqlite3.connect(d["path"])
        dst = sqlite3.connect(dest)
        with dst:
            src.backup(dst)                                     # consistent even while x-ui writes
        src.close()
        dst.close()
        return dest
    if not shutil.which("pg_dump"):
        raise PgError("pg_dump is missing (apt install postgresql-client)")
    dest = os.path.join(folder, PG_DUMP)
    r = subprocess.run(["pg_dump", "-Fc", "--no-owner", "-f", dest], capture_output=True, text=True,
                       env=_pg_env(d["dsn"]), timeout=600)
    if r.returncode:
        raise PgError("pg_dump: " + (r.stderr.strip().splitlines() or ["failed"])[-1])
    return dest


def undo_hint(folder):
    d = detect()
    if d["kind"] == "sqlite":
        return "cp %s/%s %s && x-ui restart" % (folder, os.path.basename(d["path"]), d["path"])
    return "xui-db restore %s/%s" % (folder, PG_DUMP)


def restore(path, start=True):
    """Put a backup back: a SQLite file over the database, or a pg_dump into PostgreSQL. x-ui is
    stopped for it and, unless start=False (the caller has something to do first), started again."""
    d = detect()
    subprocess.run(["systemctl", "stop", SERVICE], capture_output=True)
    try:
        if d["kind"] == "sqlite":
            shutil.copy2(path, d["path"])
        else:
            env = _pg_env(d["dsn"])
            r = subprocess.run(["pg_restore", "--clean", "--if-exists", "--no-owner", "-d", env["PGDATABASE"], path],
                               capture_output=True, text=True, env=env, timeout=900)
            if r.returncode:
                raise PgError("pg_restore: " + (r.stderr.strip().splitlines() or ["failed"])[-1])
    finally:
        if start:
            subprocess.run(["systemctl", "start", SERVICE], capture_output=True)


def is_backup(name):
    return name.endswith(".db") or name == PG_DUMP


def _main(a):
    if not a or a[0] in ("-h", "--help"):
        print(__doc__.strip())
        return 0
    if a[0] == "detect":
        print(describe())
        return 0
    if a[0] == "backup" and len(a) == 2:
        print(backup_to(a[1]))
        return 0
    if a[0] == "restore" and len(a) == 2:
        restore(a[1])
        print("restored %s; x-ui started" % a[1])
        return 0
    sys.stderr.write("usage: xui-db detect | backup FOLDER | restore FILE\n")
    return 2


if __name__ == "__main__":
    try:
        sys.exit(_main(sys.argv[1:]))
    except Error as e:
        sys.exit("xui-db: %s" % e)
