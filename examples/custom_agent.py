"""The smallest useful framework-agnostic agent."""

import asyncio

from researchswarm import AgentResult, Swarm, Task


class ArithmeticAgent:
    async def run(self, task, context):
        return AgentResult(output={"answer": sum(task.inputs["numbers"])})


async def main():
    swarm = Swarm("Add some numbers")
    swarm.register_agent("arithmetic", ArithmeticAgent())
    swarm.add_task(
        Task(
            name="Sum",
            description="Add the supplied numbers",
            agent_type="arithmetic",
            inputs={"numbers": [10, 20, 12]},
        )
    )
    result = await swarm.run()
    print((await result.tasks())[0].output)


if __name__ == "__main__":
    asyncio.run(main())

