import asyncio

from app.agent.gateway import Gateway, LoopState
from app.config import MAX_LOOP_ITERATIONS


def test_loop_halts_after_max_iterations():
    loop = LoopState()
    allowed = [loop.step() for _ in range(MAX_LOOP_ITERATIONS + 3)]
    assert allowed[:MAX_LOOP_ITERATIONS] == [True] * MAX_LOOP_ITERATIONS
    assert not any(allowed[MAX_LOOP_ITERATIONS:])
    assert loop.states[-1] == "HALTED"


class FailingHub:
    """Every retrieval call fails with the same error, so the breaker must open."""

    status = {}

    async def call(self, server, tool, args):
        return {"error": "E_TIMEOUT", "message": "simulated"}

    async def start(self):
        pass

    async def stop(self):
        pass


def test_breaker_halts_repeated_identical_failures():
    gw = Gateway(hub=FailingHub())

    async def run():
        out = []
        for _ in range(3):
            out.append(await gw.handle(query="What is the QA sign-off window?", tenant_id="tenant_a",
                                       user_id="u9"))
        return out

    results = asyncio.run(run())
    assert results[0]["status"] == "ERROR"  # first failure: reported, not yet halted
    assert results[1]["status"] == "ERROR"  # second failure: count reaches the breaker limit
    assert results[2]["status"] == "HALTED_BREAKER"  # third identical call is refused by the breaker
    assert results[2]["answer"]  # user-facing remediation message is present
