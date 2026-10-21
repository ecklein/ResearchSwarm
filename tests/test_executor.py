import asyncio

import pytest

from researchswarm import AgentResult, Artifact, RunStatus, Swarm, Task, TaskStatus


class FunctionAgent:
    def __init__(self, function):
        self.function = function

    async def run(self, task, context):
        result = self.function(task, context)
        if asyncio.iscoroutine(result):
            return await result
        return result


@pytest.mark.asyncio
async def test_dependency_outputs_and_diamond_graph():
    async def worker(task, context):
        deps = await context.get_dependency_results()
        return AgentResult(output={"name": task.name, "deps": deps})

    swarm = Swarm("diamond", max_concurrency=2)
    swarm.register_agent("worker", FunctionAgent(worker))
    a = Task("a", "", agent_type="worker")
    b = Task("b", "", agent_type="worker", dependencies=(a.id,))
    c = Task("c", "", agent_type="worker", dependencies=(a.id,))
    d = Task("d", "", agent_type="worker", dependencies=(b.id, c.id))
    for task in (a, b, c, d):
        swarm.add_task(task)
    result = await swarm.run()
    stored = {task.id: task for task in await result.tasks()}
    assert result.status is RunStatus.COMPLETED
    assert stored[d.id].output["deps"][b.id]["name"] == "b"
    assert stored[d.id].output["deps"][c.id]["name"] == "c"


@pytest.mark.asyncio
async def test_parallelism_respects_limit_and_overlaps():
    active = 0
    maximum = 0
    lock = asyncio.Lock()

    async def worker(task, context):
        nonlocal active, maximum
        async with lock:
            active += 1
            maximum = max(maximum, active)
        await asyncio.sleep(0.03)
        async with lock:
            active -= 1
        return AgentResult()

    swarm = Swarm("parallel", max_concurrency=2)
    swarm.register_agent("worker", FunctionAgent(worker))
    for index in range(5):
        swarm.add_task(Task(str(index), "", agent_type="worker"))
    result = await swarm.run()
    assert result.completed_count == 5
    assert maximum == 2


@pytest.mark.asyncio
async def test_failed_branch_blocks_descendants_but_independent_branch_finishes():
    async def worker(task, context):
        if task.name == "bad":
            raise ValueError("boom")
        return AgentResult(output={"ok": True})

    swarm = Swarm("failure")
    swarm.register_agent("worker", FunctionAgent(worker))
    bad = Task("bad", "", agent_type="worker")
    blocked = Task("blocked", "", agent_type="worker", dependencies=(bad.id,))
    independent = Task("independent", "", agent_type="worker")
    for task in (bad, blocked, independent):
        swarm.add_task(task)
    result = await swarm.run()
    stored = {task.name: task for task in await result.tasks()}
    assert result.status is RunStatus.FAILED
    assert stored["bad"].status is TaskStatus.FAILED
    assert stored["bad"].error == "ValueError: boom"
    assert stored["blocked"].status is TaskStatus.BLOCKED
    assert stored["independent"].status is TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_dynamic_tasks_and_artifacts_are_integrated():
    child_id = None

    async def worker(task, context):
        nonlocal child_id
        if task.name == "parent":
            child = Task("child", "", agent_type="worker", dependencies=(task.id,))
            child_id = child.id
            return AgentResult(
                output={"parent": True},
                new_tasks=[child],
                artifacts=[Artifact("note", "json", data={"a": 1})],
            )
        return AgentResult(output={"deps": await context.get_dependency_results()})

    swarm = Swarm("dynamic")
    swarm.register_agent("worker", FunctionAgent(worker))
    parent = Task("parent", "", agent_type="worker")
    swarm.add_task(parent)
    result = await swarm.run()
    tasks = {task.id: task for task in await result.tasks()}
    assert result.task_count == 2
    assert tasks[child_id].output["deps"][parent.id] == {"parent": True}
    assert len(await result.artifacts()) == 1
    events = await result.events()
    assert [event.sequence_number for event in events] == list(range(1, len(events) + 1))


@pytest.mark.asyncio
async def test_active_cancellation():
    started = asyncio.Event()

    async def worker(task, context):
        started.set()
        await asyncio.sleep(10)
        return AgentResult()

    swarm = Swarm("cancel")
    swarm.register_agent("worker", FunctionAgent(worker))
    swarm.add_task(Task("slow", "", agent_type="worker"))
    running = asyncio.create_task(swarm.run())
    await started.wait()
    await swarm.cancel()
    result = await running
    assert result.status is RunStatus.CANCELLED
