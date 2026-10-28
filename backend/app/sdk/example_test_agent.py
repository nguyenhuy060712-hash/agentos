"""Example: evaluate an agent with pytest + Agent OS evaluators.
    cd os/sdk && AGENTOS_ENDPOINT=https://... AGENTOS_API_KEY=ak_... AGENTOS_DATASET=pytest-demo pytest -q example_test_agent.py
Without AGENTOS_API_KEY the tests still run locally and nothing is sent."""
import pytest

def fake_agent(q):   # replace with your agent call; returns (answer, tool calls)
    return "Paris", [{"name": "web_search", "args": {"q": q}}, {"name": "fetch_url", "args": {"url": "https://en.wikipedia.org/wiki/Paris"}}]

@pytest.mark.agentos
def test_capital(agentos):
    q = "What is the capital of France?"
    agentos.log_inputs({"question": q}); agentos.log_reference({"answer": "Paris", "trajectory": ["web_search", "fetch_url"]})
    out, calls = fake_agent(q)
    agentos.log_outputs({"answer": out}); agentos.log_trajectory(calls)
    assert agentos.trajectory_match(["web_search", "fetch_url"], mode="strict") == 1.0
    assert agentos.trajectory_match([{"name": "web_search", "args": {"q": q}}], mode="superset", args="subset") == 1.0
    agentos.log_feedback("correct", out == "Paris")
    assert agentos.composite("quality", keys=["correct", "trajectory_strict"], weights={"correct": 3}) == 1.0

@pytest.mark.agentos
def test_no_extra_tools(agentos):
    agentos.log_inputs({"question": "hi"}); _, calls = fake_agent("hi"); agentos.log_trajectory(calls)
    assert agentos.trajectory_match(["web_search"], mode="subset") == 0.0   # agent called fetch_url too
