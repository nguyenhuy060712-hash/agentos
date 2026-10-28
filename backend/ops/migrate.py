#!/usr/bin/env python3
"""GĐ10.29 migration CLI: status | up [--dry-run] | down [N]. Uses PG_URI like the app. Run from agentos-local: python ops/migrate.py status"""
import asyncio, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))
import psycopg, migrations

async def main(argv):
    cmd, rest = (argv or ["status"])[0], argv[1:]
    if cmd not in ("status", "up", "down"): return print(__doc__) or 2
    async with await psycopg.AsyncConnection.connect(os.environ["PG_URI"], autocommit=True) as conn:
        await conn.execute("SELECT pg_advisory_lock(hashtext('agentos_boot_ddl'))")   # same lock as app boot
        try:
            if cmd == "status": lines = [f"{'applied' if r['applied'] else 'pending'}  {r['version']}  {r['name']}" for r in await migrations.status(conn)]
            elif cmd == "up": lines = await migrations.run_pending(conn, dry_run="--dry-run" in rest) or ["nothing to apply"]
            else: lines = await migrations.rollback(conn, int(rest[0]) if rest else 1) or ["nothing to roll back"]
        except migrations.MigrationError as e: return print("MIGRATION ERROR:", e, file=sys.stderr) or 1
    print("\n".join(lines)); return 0

if __name__ == "__main__": sys.exit(asyncio.run(main(sys.argv[1:])))
