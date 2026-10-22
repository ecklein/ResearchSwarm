"""Deterministic value scheduling with a two-slot, limited-budget swarm.

Run with: python examples/information_value_scheduler.py
No APIs, providers, or keys are needed.
"""

import asyncio

from researchswarm import (
    AgentResult,
    BudgetLedger,
    BudgetLimits,
    InformationValueScheduler,
    ResourceUsage,
    SchedulingSignals,
    Swarm,
    Task,
    TaskStatus,
    get_scheduling_signals,
    set_scheduling_signals,
)


class MeasurementAgent:
    async def run(self, task, context):
        await asyncio.sleep(0.01)
        return AgentResult(
            output={"measurement": task.inputs["measurement"]},
            usage=ResourceUsage(cost=task.inputs["actual_cost"], input_tokens=20, output_tokens=10),
        )


async def main() -> None:
    scheduler = InformationValueScheduler()
    limits = BudgetLimits(max_cost=0.6)
    swarm = Swarm(
        "Select useful affordable measurements",
        scheduler=scheduler,
        budget=limits,
        max_concurrency=2,
    )
    swarm.register_agent("measure", MeasurementAgent())
    specifications = [
        ("Promising measurement", (0.9, 0.8, 0.9, 0.7, 0.2), 0.15),
        ("Independent replication", (0.8, 0.7, 0.6, 0.8, 0.25), 0.20),
        ("Expensive broad study", (1.0, 1.0, 1.0, 1.0, 0.9), 0.9),
        ("Cheap follow-up", (0.5, 0.4, 0.5, 0.3, 0.1), 0.1),
        ("Low-value extension", (0.2, 0.3, 0.2, 0.2, 0.3), 0.3),
    ]
    tasks = {}
    for index, (name, estimates, actual) in enumerate(specifications):
        task = Task(
            name,
            "Produce a deterministic measurement",
            agent_type="measure",
            inputs={"measurement": index, "actual_cost": actual},
        )
        set_scheduling_signals(task, SchedulingSignals(*estimates))
        swarm.add_task(task)
        tasks[task.id] = task

    try:
        result = await swarm.run()
        ledger = BudgetLedger(limits)
        print(f"{'Task':28} {'Score':>8} {'Estimate':>9} {'Actual':>8} {'Remaining':>10}")
        for event in await result.events():
            if event.event_type == "scheduler.tasks_selected":
                names = [tasks[task_id].name for task_id in event.payload["task_ids"]]
                print(f"Round {event.payload['round']}: {', '.join(names)}")
            elif event.event_type == "task.completed":
                task = tasks[event.task_id]
                usage = ResourceUsage(**event.payload["usage"])
                ledger.record(task.id, usage)
                signals = get_scheduling_signals(task)
                assert signals is not None
                print(
                    f"{task.name:28} {scheduler.score(task).score:8.3f} "
                    f"{signals.estimated_cost:9.2f} {usage.cost:8.2f} "
                    f"{ledger.snapshot().cost_remaining:10.2f}"
                )
        reason = swarm.run_record.metadata["researchswarm"].get(
            "stop_reason", "all_tasks_completed"
        )
        print(f"Run: {result.status}; stop reason: {reason}")
        print(
            "Unselected tasks:",
            ", ".join(
                task.name
                for task in await result.tasks()
                if task.status is not TaskStatus.COMPLETED
            ),
        )
    finally:
        await swarm.close()


if __name__ == "__main__":
    asyncio.run(main())
