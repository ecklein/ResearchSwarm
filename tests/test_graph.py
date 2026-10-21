import pytest

from researchswarm import CycleError, Task, TaskGraph, TaskNotFoundError, TaskStatus


def test_dependencies_dependents_and_ready_tasks():
    graph = TaskGraph()
    first = Task("first", "")
    second = Task("second", "", dependencies=(first.id,))
    graph.add_task(first)
    graph.add_task(second)
    assert graph.dependencies(second.id) == (first,)
    assert graph.dependents(first.id) == (second,)
    first.transition_to(TaskStatus.READY)
    assert graph.ready_tasks() == [first]


def test_cycle_is_rejected_and_edge_rolled_back():
    graph = TaskGraph()
    a, b = Task("a", ""), Task("b", "")
    graph.add_task(a)
    graph.add_task(b)
    graph.add_dependency(b.id, a.id)
    with pytest.raises(CycleError):
        graph.add_dependency(a.id, b.id)
    assert graph.dependencies(a.id) == ()


def test_batch_add_supports_peer_dependencies():
    graph = TaskGraph()
    a = Task("a", "")
    b = Task("b", "", dependencies=(a.id,))
    graph.add_tasks([b, a])
    graph.validate()
    assert graph.dependencies(b.id) == (a,)


def test_missing_dependency_is_rejected_atomically():
    graph = TaskGraph()
    task = Task("a", "", dependencies=("task_missing",))
    with pytest.raises(TaskNotFoundError):
        graph.add_task(task)
    assert graph.tasks == ()


def test_complete_requires_all_completed():
    graph = TaskGraph()
    task = Task("a", "")
    graph.add_task(task)
    assert not graph.is_complete()
    task.transition_to(TaskStatus.READY)
    task.transition_to(TaskStatus.RUNNING)
    task.transition_to(TaskStatus.COMPLETED)
    assert graph.is_complete()

