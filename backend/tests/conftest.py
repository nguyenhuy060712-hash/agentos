"""Test-process defaults. GĐ10.2: the in-process tests talk to fake receivers on loopback, which netguard only reaches when AOS_EGRESS_ALLOW lists them
(same value as the local server start scripts). Tests that check the policy itself clear/override it with monkeypatch."""
import os
os.environ.setdefault("AOS_EGRESS_ALLOW", "127.0.0.0/8,::1")
