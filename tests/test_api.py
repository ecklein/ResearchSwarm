import pytest

from researchswarm import (
    AgentNotFoundError,
    AgentResult,
    RunStatus,
    SQLiteStorage,
    Swarm,
    Task,
    TaskStatus,
)


class Agent:
    async def run(self, task, context):
        return AgentResult(output={"done": True})


@pytest.mark.asyncio
async def test_unknown_agent_fails_before_task_starts():
    swarm = Swarm("invalid")
    task = Task("task", "", agent_type="missing")
    swarm.add_task(task)
    with pytest.raises(AgentNotFoundError):
        await swarm.run()
    assert task.status is TaskStatus.PENDING


@pytest.mark.asyncio
async def test_sqlite_acceptance_flow_and_result_accessors(tmp_path):
    path = tmp_path / "acceptance.db"
    swarm = Swarm("acceptance", storage=path, max_concurrency=3)
    swarm.register_agent("agent", Agent())
    tasks = [Task(str(index), "", agent_type="agent") for index in range(3)]
    synthesis = Task(
        "synthesis", "", agent_type="agent", dependencies=tuple(task.id for task in tasks)
    )
    for task in [*tasks, synthesis]:
        swarm.add_task(task)
    result = await swarm.run()
    assert result.status is RunStatus.COMPLETED
    assert result.task_count == 4
    assert len(await result.tasks()) == 4
    await swarm.close()


@pytest.mark.asyncio
async def test_resume_resets_interrupted_running_task(tmp_path):
    path = tmp_path / "resume.db"
    storage = SQLiteStorage(path)
    original = Swarm("resume", storage=storage)
    original.register_agent("agent", Agent())
    task = Task("task", "", agent_type="agent")
    original.add_task(task)
    await original._initialize()
    original.run_record.transition_to(RunStatus.RUNNING)
    await storage.save_run(original.run_record)
    task.transition_to(TaskStatus.READY)
    task.transition_to(TaskStatus.RUNNING)
    await storage.save_task(original.run_id, task)

    resumed = await Swarm.load(original.run_id, storage=storage)
    loaded = await storage.get_task(original.run_id, task.id)
    assert loaded.status is TaskStatus.PENDING
    resumed.register_agent("agent", Agent())
    result = await resumed.run()
    assert result.status is RunStatus.COMPLETED
    assert any(event.event_type == "task.recovered" for event in await result.events())
    await resumed.close()
