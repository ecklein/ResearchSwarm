"""Persist and reload a research graph without an LLM.

Run: python examples/research_graph.py [path/to/research.db]
The TaskGraph is an execution plan; this example records research explicitly.
"""

import asyncio
import sys
from pathlib import Path

from researchswarm import (
    Artifact,
    Claim,
    Conflict,
    Critique,
    CritiqueSeverity,
    Event,
    Evidence,
    Experiment,
    ExperimentResult,
    Hypothesis,
    ResearchRelation,
    ResearchWorkspace,
    Run,
    SQLiteStorage,
    Task,
    TaskGraph,
)


async def main(path: Path) -> None:
    storage = SQLiteStorage(path)
    run = Run("Explore the relationship between X and Y")
    try:
        await storage.save_run(run)

        # Execution dependencies have their own graph and identifiers.
        tasks = TaskGraph()
        design = Task("Design experiment", "Choose a measurement procedure")
        analyze = Task("Analyze measurements", "Estimate association", dependencies=(design.id,))
        tasks.add_tasks([design, analyze])
        for task in tasks.tasks:
            await storage.save_task(run.id, task)

        async def emit(name: str, payload: dict[str, object]) -> Event:
            return await storage.append_event(Event(run.id, name, payload=payload))

        workspace = ResearchWorkspace(run.id, storage, emit)
        h = await workspace.add_hypothesis(
            Hypothesis("Feature X predicts target Y", rationale="A proposed shared mechanism.")
        )
        experiment = await workspace.add_experiment(
            Experiment(
                "Test X against Y",
                description="Measure association on a synthetic sample.",
                hypothesis_ids=(h.id,),
                design={"sample_size": 20},
                task_ids=(design.id, analyze.id),
            )
        )
        artifact = Artifact(
            "Illustrative measurements", "statistics", data={"effect": 0.4, "n": 20}
        )
        await storage.save_artifact(run.id, artifact)
        result = await workspace.add_result(
            ExperimentResult(
                experiment.id,
                "Positive association",
                metrics={"effect": 0.4, "n": 20},
                artifact_ids=(artifact.id,),
            )
        )
        evidence = await workspace.add_evidence(
            Evidence(
                "Positive association in the sample",
                "statistics",
                artifact_id=artifact.id,
            )
        )
        claim = await workspace.add_claim(Claim("X and Y are positively associated in this sample"))
        critique = await workspace.add_critique(
            Critique(
                result.id,
                "The sample is small",
                CritiqueSeverity.MAJOR,
                recommendations=("Replicate on an independent sample",),
            )
        )
        await workspace.add_conflict(
            Conflict(
                h.id,
                claim.id,
                "Prediction beyond the sample remains untested.",
            )
        )
        for source, target, relation in [
            (experiment, h, ResearchRelation.TESTS),
            (experiment, result, ResearchRelation.PRODUCES),
            (result, evidence, ResearchRelation.PRODUCES),
            (evidence, claim, ResearchRelation.SUPPORTS),
            (claim, h, ResearchRelation.SUPPORTS),
            (critique, result, ResearchRelation.CRITIQUES),
        ]:
            await workspace.link(source, target, relation)
    finally:
        await storage.close()

    reopened = SQLiteStorage(path)
    try:
        graph = await ResearchWorkspace(run.id, reopened).graph()
        print(f"Run: {run.id}")
        print(f"TaskGraph: {len(await reopened.list_tasks(run.id))} planned execution tasks")
        print(
            f"ResearchGraph: {len(graph.entities())} entities, {len(graph.edges())} relationships"
        )
        print(f"Hypothesis: {graph.get(h.id).statement}")
        print(f"Experiments: {len(graph.experiments_for(h.id))}")
        print(f"Evidence for claim: {len(graph.evidence_for(claim.id))}")
        print(f"Critiques of result: {len(graph.critiques_for(result.id))}")
        print(f"Explicit conflicts: {len(graph.conflicts_for(h.id))}")
        print(f"Persisted events: {len(await reopened.list_events(run.id))}")
    finally:
        await reopened.close()


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1] if len(sys.argv) > 1 else "research_graph.db")))
