import pytest

from llm_agent.circuit_breaker.circuit_breaker import AgentCircuitBreaker
from llm_agent.config import CircuitBreakerConfig
from llm_agent.core.llm_client import LLMError, CONTEXT_FULL
from llm_agent.core.patience import Patience


def make(max_wait=600):
    slept = []

    async def sleep(s):
        slept.append(s)
    return Patience(max_wait, sleep=sleep, jitter=lambda: 0.5), slept


async def test_waits_out_a_rate_limit_and_carries_on_with_the_same_request():
    p, slept = make()
    calls, told = [], []

    async def fn():
        calls.append(1)
        if len(calls) < 3:
            raise LLMError("rate limited", status=429, retryable=True)
        return "answer"

    async def on_wait(delay, why, attempt):
        told.append((int(delay), why, attempt))
    assert await p.call(fn, on_wait) == "answer"
    assert len(calls) == 3 and slept == [10.0, 20.0] and told[0][1] == "the model is rate limited" and told[1][2] == 2


async def test_gives_up_with_a_clear_message_when_the_waiting_allowance_is_used():
    p, slept = make(max_wait=25)

    async def fn():
        raise LLMError("overloaded", status=503, retryable=True)
    with pytest.raises(LLMError) as e:
        await p.call(fn)
    assert "waited 10 seconds" in str(e.value) and slept == [10.0]


async def test_real_errors_are_not_waited_for():
    p, slept = make()
    for status in (401, 404, 500, 400):
        async def fn():
            raise LLMError("no", status=status, retryable=status == 500)
        with pytest.raises(LLMError):
            await p.call(fn)
    assert slept == []


async def test_a_dropped_connection_is_waited_for():
    p, slept = make()
    n = []

    async def fn():
        n.append(1)
        if len(n) == 1:
            raise LLMError("Cannot connect", retryable=True)
        return "ok"
    assert await p.call(fn) == "ok" and slept == [10.0]


def test_context_errors_are_recognised():
    for m in ("This model's maximum context length is 8192 tokens", "prompt is too long", "Please reduce the length of the messages"):
        assert CONTEXT_FULL.search(m)
    assert not CONTEXT_FULL.search("invalid api key")


def test_a_renewed_allowance_counts_progress_but_loops_never_renew():
    b = AgentCircuitBreaker(CircuitBreakerConfig(max_total_turns=2, max_checkpoints=2))
    assert b.check_turn().allowed and b.check_turn().allowed and not b.check_turn().allowed
    assert b.can_renew()
    b.renew()
    assert b.check_turn().allowed and b.all_turns == 3 and b.checkpoints == 1
    b.note_repeat("x"), b.note_repeat("x"), b.note_repeat("x")
    assert not b.note_repeat("x").allowed and not b.can_renew()          # a loop is not a limit
    b2 = AgentCircuitBreaker(CircuitBreakerConfig(max_total_turns=1, max_checkpoints=1))
    b2.check_turn(); b2.check_turn(); b2.renew(); b2.check_turn(); b2.check_turn()
    assert not b2.can_renew()                                              # the number of checkpoints is capped
