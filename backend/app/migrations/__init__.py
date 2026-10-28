"""GĐ10.29 ordered schema migrations (F24).

RULE: a new table / column / index = a NEW numbered file here (`NNNN_name.py`), never a new module-level `ALTER ... IF NOT EXISTS`.
Never edit a file once it is applied anywhere: its checksum is stored in `schema_migrations` and boot fails loudly if it changes.
Each file defines: VERSION = "NNNN" (= the file prefix), NAME, `async def up(q)`, `async def down(q)`, optional `async def plan(q) -> list[str]`
(what up() would change, shown by dry-run). q(sql, args=()) runs inside the migration's own transaction and returns rows (or None).
Runner: `run_pending(conn)` at boot (deepagent_api lifespan, inside the boot advisory lock), CLI `ops/migrate.py status|up [--dry-run]|down [N]`.
`conn` = psycopg AsyncConnection in autocommit mode (each migration opens its own transaction)."""
import hashlib, importlib.util, re
from pathlib import Path

DIR = Path(__file__).parent
_FILE = re.compile(r"^(\d{4})_[a-z0-9_]+\.py$")
_DDL = "CREATE TABLE IF NOT EXISTS schema_migrations(version TEXT PRIMARY KEY, name TEXT, checksum TEXT, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"

class MigrationError(RuntimeError): pass

def _checksum(p: Path): return hashlib.sha256(p.read_bytes().replace(b"\r\n", b"\n")).hexdigest()   # CRLF-proof (Windows checkout vs WSL/VPS)

def _load():
    """Sorted [(version, module, checksum)] from DIR (looked up at call time: tests point DIR at a temp folder)."""
    out = []
    for p in sorted(DIR.iterdir()):
        m = _FILE.match(p.name)
        if not m: continue
        spec = importlib.util.spec_from_file_location("aos_migration_" + p.stem, p); mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
        if mod.VERSION != m.group(1): raise MigrationError(f"{p.name}: VERSION {mod.VERSION!r} does not match the file prefix")
        out.append((mod.VERSION, mod, _checksum(p)))
    if len({v for v, _, _ in out}) != len(out): raise MigrationError("duplicate migration VERSION")
    return out

def _q(conn):
    async def q(sql, args=()):
        cur = await conn.execute(sql, args)
        return await cur.fetchall() if cur.description else None
    return q

async def _applied(conn, create=True):
    """{version: (name, checksum)}; create=False (dry-run / status) never creates the table."""
    if create: await conn.execute(_DDL)
    elif not (await (await conn.execute("SELECT to_regclass('schema_migrations')")).fetchone())[0]: return {}
    return {r[0]: (r[1], r[2]) for r in await (await conn.execute("SELECT version,name,checksum FROM schema_migrations")).fetchall()}

def _guard(files, applied):
    """Loud failure if an applied file changed or vanished."""
    byv = {v: c for v, _, c in files}
    for v, (name, ck) in applied.items():
        if v not in byv: raise MigrationError(f"migration {v} ({name}) is applied but its file is missing")
        if byv[v] != ck: raise MigrationError(f"migration {v} ({name}) was changed after it was applied (checksum mismatch) - add a new numbered file instead")

async def status(conn):
    files, applied = _load(), await _applied(conn, create=False); _guard(files, applied)
    return [{"version": v, "name": m.NAME, "applied": v in applied} for v, m, _ in files]

async def run_pending(conn, dry_run=False):
    """Apply every not-yet-applied migration in version order, each in one transaction. Returns the lines applied (dry-run: the planned steps, nothing changed)."""
    files = _load(); applied = await _applied(conn, create=not dry_run); _guard(files, applied)
    out = []
    for v, m, ck in files:
        if v in applied: continue
        if dry_run:
            async with conn.transaction(force_rollback=True):
                steps = await m.plan(_q(conn)) if hasattr(m, "plan") else ["(no plan given)"]
            out += [f"{v} {m.NAME}: {s}" for s in steps]; continue
        async with conn.transaction():
            await m.up(_q(conn))
            await conn.execute("INSERT INTO schema_migrations(version,name,checksum) VALUES(%s,%s,%s)", (v, m.NAME, ck))
        out.append(f"{v} {m.NAME}: applied")
    return out

async def rollback(conn, steps=1):
    """Run down() of the newest `steps` applied migrations (newest first) and delete their rows."""
    files, applied = _load(), await _applied(conn); _guard(files, applied)
    mods = {v: m for v, m, _ in files}; out = []
    for v in sorted(applied, reverse=True)[:max(0, int(steps))]:
        async with conn.transaction():
            await mods[v].down(_q(conn))
            await conn.execute("DELETE FROM schema_migrations WHERE version=%s", (v,))
        out.append(f"{v} {mods[v].NAME}: rolled back")
    return out
