"""Agent OS run-queue worker (second process). Runs the same Postgres queue as the API (FOR UPDATE SKIP LOCKED), no HTTP server.
systemd: deepagent-worker.service (AOS_ROLE=worker, AOS_WORKER=worker1). Exits non-zero if the DB is unreachable for 60 s so systemd restarts it."""
import asyncio, os, signal, sys, time
os.environ.setdefault("AOS_ROLE", "worker"); os.environ.setdefault("AOS_WORKER", "worker1")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import deepagent_api as A

async def main():
    stop = asyncio.Event()
    for sg in (signal.SIGTERM, signal.SIGINT): asyncio.get_running_loop().add_signal_handler(sg, stop.set)
    async with A.lifespan(A.app):
        print(f"worker {os.environ['AOS_WORKER']} up pid={os.getpid()}", flush=True)
        bad_since = None
        while not stop.is_set():
            try:
                await A.q("SELECT 1"); bad_since = None
            except Exception as e:  # noqa: BLE001
                bad_since = bad_since or time.time(); print("db check failed", e, flush=True)
                if time.time() - bad_since > 60: sys.exit(3)
            if A.S["queue"].done():
                print("queue loop died", A.S["queue"].exception() if not A.S["queue"].cancelled() else "cancelled", flush=True); sys.exit(4)
            try: await asyncio.wait_for(stop.wait(), 10)
            except asyncio.TimeoutError: pass
        import server   # GĐ13.30 graceful: no new claims, in-flight runs finish (AOS_BG_SHUTDOWN_GRACE_SECS, default 120), the rest goes back to `pending` (not lost)
        await server.shutdown()

if __name__ == "__main__":
    asyncio.run(main())
