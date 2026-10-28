"""GĐ11.41 per-agent settings of the virtual computer (`AgentCfg.computer`). Pure pydantic: no DB, no Docker.
mode      off    = today's behaviour (no box, no `execute`)
          thread = one box per chat thread (and per user)
          agent  = one box per agent for each user (never one box shared between two people, see sbx_backend.box_key)
internet  off | basic | custom (+ allow_list); the workspace ceiling (workspace_sandbox_quotas.internet) still caps it (Computers.create)
fs_rules  FilesystemPermission rules. Deep Agents refuses rules for paths a shell can reach (the `execute` tool would bypass them), so only the
          routed virtual folders can carry rules; anything else is refused here with a clear message instead of giving a false sense of safety."""
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

ROUTED = ("/pack/", "/skills/", "/memory/")                       # prefixes served by read-only routes next to the box (sbx_backend.wire)
DEFAULT_DELETE_AFTER_STOP_S = 7 * 86400                           # a stopped per-thread box is deleted after a week (keeps the workspace's box quota free)
MAX_TTL_S = 30 * 86400

class FsRule(BaseModel):
    operations: list[Literal["read", "write"]] = Field(min_length=1, max_length=2)
    paths: list[str] = Field(min_length=1, max_length=20)
    mode: Literal["allow", "deny", "interrupt"] = "deny"

    @field_validator("paths")
    @classmethod
    def _paths(cls, v):
        for p in v:
            if not isinstance(p, str) or not p.startswith(ROUTED) or len(p) > 200 or ".." in p.split("/") or "~" in p.split("/"):
                raise ValueError(f"fs_rules paths must start with one of {', '.join(ROUTED)} (the shell can reach the rest of the box, so a file rule there would not protect anything; use approve_shell): {str(p)[:60]}")
        return v

class ComputerCfg(BaseModel):
    mode: Literal["off", "thread", "agent"] = "off"
    internet: Literal["off", "basic", "custom"] = "off"
    allow_list: list[str] = Field(default_factory=list, max_length=200)
    size: Literal["small", "medium"] = "small"
    idle_ttl_seconds: int | None = Field(default=None, ge=60, le=MAX_TTL_S)
    delete_after_stop_seconds: int | None = Field(default=DEFAULT_DELETE_AFTER_STOP_S, ge=60, le=MAX_TTL_S)   # per-thread boxes only (an agent box is never deleted automatically)
    approve_shell: bool = False            # every `execute` waits for a human decision (HITL)
    fs_rules: list[FsRule] = Field(default_factory=list, max_length=50)
    setup_sh: str = Field(default="", max_length=20000)   # written to /opt/aos-user/setup.sh and run once at creation; aos-boot re-runs it at every start

    @field_validator("delete_after_stop_seconds")
    @classmethod
    def _dels(cls, v):
        if v is not None and v % 60: raise ValueError("delete_after_stop_seconds must be a multiple of 60")
        return v

    @model_validator(mode="after")
    def _check(self):
        if self.internet == "custom":
            import egress_policy as EP      # 11.45 (lazy: only a custom allow-list needs it)
            try: self.allow_list = EP.validate_list(self.allow_list)
            except EP.BadEntry as e: raise ValueError("bad allow-list: " + str(e)) from None
        elif self.allow_list:
            raise ValueError("allow_list is only used with internet='custom'")
        return self

    @property
    def on(self) -> bool: return self.mode != "off"
