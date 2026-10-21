"""Pass structured outputs through a sequential dependency graph."""

import asyncio

from researchswarm import AgentResult, Swarm, Task


class PipelineAgent:
    async def run(self, task, context):
        dependencies = await context.get_dependency_results()
        return AgentResult(output={"stage": task.name, "received": dependencies})


async def main():
    swarm = Swarm("Sequential research pipeline")
    swarm.register_agent("pipeline", PipelineAgent())

    tasks = []
    previous = None
    for name in ("research", "analysis", "critique", "revision"):
        task = Task(
            name=name,
            description=f"Perform {name}",
            agent_type="pipeline",
            dependencies=(previous.id,) if previous else (),
        )
        tasks.append(task)
        previous = task
    for task in tasks:
        swarm.add_task(task)

    result = await swarm.run()
    for task in await result.tasks():
        print(task.name, task.output)


if __name__ == "__main__":
    asyncio.run(main())

