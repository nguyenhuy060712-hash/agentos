"""Marks the pre-GĐ10 schema (built by the per-module `init()` DDL). Changes nothing."""
VERSION = "0000"
NAME = "baseline"

async def up(q): pass
async def down(q): pass
async def plan(q): return ["no change (marks the pre-GĐ10 schema)"]
