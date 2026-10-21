"""Run three branches concurrently, then synthesize their outputs."""

import asyncio

from researchswarm import AgentResult, Swarm, Task


class Worker:
    async def run(self, task, context):
        await asyncio.sleep(0.1)
        return AgentResult(output={"finding": f"finding from {task.name}"})


class Synthesizer:
    async def run(self, task, context):
        return AgentResult(output={"combined": await context.get_dependency_results()})


async def main():
    swarm = Swarm("Parallel research demonstration", max_concurrency=3)
    swarm.register_agent("worker", Worker())
    swarm.register_agent("synthesizer", Synthesizer())

    branches = [
        Task(name=name, description=f"Research {name}", agent_type="worker")
        for name in ("A", "B", "C")
    ]
    synthesis = Task(
        name="Synthesis",
        description="Combine all branches",
        agent_type="synthesizer",
        dependencies=tuple(task.id for task in branches),
    )
    for task in [*branches, synthesis]:
        swarm.add_task(task)

    result = await swarm.run()
    completed = {task.name: task.output for task in await result.tasks()}
    print(result.status)
    print(completed["Synthesis"])


if __name__ == "__main__":
    asyncio.run(main())

