"""Expand the active DAG from an agent result."""

import asyncio

from researchswarm import AgentResult, Swarm, Task


class Planner:
    async def run(self, task, context):
        follow_ups = [
            Task(
                name=f"Investigate {topic}",
                description=f"Follow up on {topic}",
                agent_type="worker",
                dependencies=(task.id,),
            )
            for topic in ("quality", "risk")
        ]
        return AgentResult(output={"plan": "created"}, new_tasks=follow_ups)


class Worker:
    async def run(self, task, context):
        return AgentResult(
            output={"finding": task.name, "inputs": await context.get_dependency_results()}
        )


async def main():
    swarm = Swarm("Dynamic research plan")
    swarm.register_agent("planner", Planner())
    swarm.register_agent("worker", Worker())
    swarm.add_task(Task("Plan", "Choose follow-up work", agent_type="planner"))
    result = await swarm.run()
    print(result.status, [task.name for task in await result.tasks()])


if __name__ == "__main__":
    asyncio.run(main())

