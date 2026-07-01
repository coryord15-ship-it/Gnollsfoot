import time
import pytest
from app.research.queue import ResearchQueue


def test_queue_drains_after_silence():
    processed = []

    def fake_research(item):
        processed.append(item)

    q = ResearchQueue(silence_seconds=0.2, cooldown_seconds=0.05)
    q.set_research_fn(fake_research)
    q.start()

    q.enqueue("Blue Rose")
    q.enqueue("Gnoll Fang")

    # Wait long enough for both items to be processed
    time.sleep(2.0)
    q.stop()

    assert "Blue Rose" in processed
    assert "Gnoll Fang" in processed


def test_queue_pauses_on_log_activity():
    processed = []

    def fake_research(item):
        processed.append(item)

    q = ResearchQueue(silence_seconds=0.5, cooldown_seconds=0.05)
    q.set_research_fn(fake_research)
    q.start()
    q.enqueue("Magic Sword")

    # Simulate active log — keeps resetting the timer
    for _ in range(5):
        time.sleep(0.1)
        q.on_log_activity()

    # Should NOT have been processed yet (log has been active)
    assert "Magic Sword" not in processed

    # Now let it go quiet
    time.sleep(1.5)
    q.stop()
    assert "Magic Sword" in processed


def test_deduplication():
    processed = []

    def fake_research(item):
        time.sleep(0.05)
        processed.append(item)

    q = ResearchQueue(silence_seconds=0.1, cooldown_seconds=0.05)
    q.set_research_fn(fake_research)
    q.start()

    q.enqueue("Duplicate Item")
    q.enqueue("Duplicate Item")

    time.sleep(1.0)
    q.stop()

    assert processed.count("Duplicate Item") == 1
