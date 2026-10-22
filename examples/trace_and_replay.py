"""Export, load and replay a deterministic research run without calling agents again.

Run: python examples/trace_and_replay.py [trace.json]
"""

import argparse
import asyncio
import json
from pathlib import Path

from researchswarm import (
    AgentResult,
    Artifact,
    Claim,
    Hypothesis,
    ReplayEngine,
    ResearchRelation,
    ResearchWorkspace,
    ResourceUsage,
    RunTrace,
    Swarm,
    Task,
    TraceValidator,
    verify_replay,
)


class ResearchAgent:
    async def run(self, task, context):
        workspace = ResearchWorkspace.from_context(context)
        if task.name == "Propose":
            hypothesis = await workspace.add_hypothesis(Hypothesis("Feature X predicts Y"))
            return AgentResult(
                output={"hypothesis_id": hypothesis.id}, usage=ResourceUsage(cost=0.1)
            )
        dependencies = await context.get_dependency_results()
        hypothesis_id = next(iter(dependencies.values()))["hypothesis_id"]
        claim = await workspace.add_claim(
            Claim("X and Y are associated in the illustrative sample")
        )
        await workspace.link(claim.id, hypothesis_id, ResearchRelation.SUPPORTS)
        return AgentResult(
            output={"claim_id": claim.id},
            artifacts=[Artifact("measurements", "json", data={"association": 0.4})],
            usage=ResourceUsage(cost=0.2, compute_seconds=0.01),
        )


async def execute(output: Path) -> None:
    swarm = Swarm("Demonstrate portable research traces")
    swarm.register_agent("research", ResearchAgent())
    propose = Task("Propose", "Record a hypothesis", agent_type="research")
    measure = Task(
        "Measure", "Record a factual claim", agent_type="research", dependencies=(propose.id,)
    )
    swarm.add_task(propose)
    swarm.add_task(measure)
    try:
        result = await swarm.run()
        await result.export_trace(output)
    finally:
        await swarm.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("output", nargs="?", type=Path, default=Path("trace-example.json"))
    args = parser.parse_args()
    asyncio.run(execute(args.output))
    # The swarm and its agents are gone. Only a JSON file is needed from here on.
    trace = RunTrace.load(args.output)
    TraceValidator.validate(trace).raise_for_errors()
    verify_replay(trace).raise_for_errors()
    state = ReplayEngine.replay(trace)
    names = {task.task_id: task.name for task in trace.tasks}
    print("Execution order:", " -> ".join(names[task_id] for task_id in state.task_execution_order))
    print("Final run state:", state.run_status)
    print(
        "Final task states:",
        {names[key]: value.value for key, value in state.task_statuses.items()},
    )
    print("Manifest:", json.dumps(trace.manifest(), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
