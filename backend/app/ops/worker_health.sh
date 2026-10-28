#!/bin/bash
# Restart the queue worker if its heartbeat is older than 90 s (or the unit is not active).
AGE=$(docker exec lc-postgres psql -U langgraph -d langgraph -Atc "SELECT coalesce(extract(epoch from now()-at)::int, 9999) FROM worker_heartbeats WHERE name='worker1'" 2>/dev/null)
AGE=${AGE:-9999}
if ! systemctl is-active --quiet deepagent-worker || [ "$AGE" -gt 90 ]; then
  logger -t agentos-worker-health "worker1 unhealthy (heartbeat age ${AGE}s) -> restart"; systemctl restart deepagent-worker
fi
