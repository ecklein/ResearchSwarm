from datetime import timedelta

from researchswarm import PriorityScheduler, Task, TaskStatus


def ready(task):
    task.transition_to(TaskStatus.READY)
    return task


def test_priority_order_slot_limit_and_pending_filter():
    base = Task("base", "")
    low = ready(Task("low", "", priority=1, created_at=base.created_at))
    high_late = ready(
        Task("late", "", priority=2, created_at=base.created_at + timedelta(seconds=1))
    )
    high_early = ready(Task("early", "", priority=2, created_at=base.created_at))
    pending = Task("pending", "", priority=100)
    chosen = PriorityScheduler().choose([low, high_late, pending, high_early], 2)
    assert chosen == [high_early, high_late]


def test_ties_break_by_id():
    a = ready(Task("a", "", id="task_b"))
    b = ready(Task("b", "", id="task_a", created_at=a.created_at))
    assert PriorityScheduler().choose([a, b], 2) == [b, a]

