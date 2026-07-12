from concurrent.futures import ThreadPoolExecutor

from app.db import DraftRepository, GenerationJobStatus, init_database


def repository(tmp_path) -> DraftRepository:
    return DraftRepository(init_database(f"sqlite:///{tmp_path / 'jobs.db'}"))


def test_generation_jobs_are_durable_claimed_once_and_requeued_after_restart(tmp_path) -> None:
    repo = repository(tmp_path)
    job = repo.create_generation_job(
        source_type="public",
        source_locator="https://chem.libretexts.org/Books/Page",
        request={"source_type": "public", "source_locator": "https://chem.libretexts.org/Books/Page"},
        reviewer="reviewer",
    )
    claimed = repo.claim_next_generation_job()
    assert claimed is not None
    assert claimed.id == job.id
    assert claimed.status == GenerationJobStatus.RUNNING.value
    assert repo.claim_next_generation_job() is None
    assert repo.requeue_interrupted_generation_jobs() == 1
    claimed_again = repo.claim_next_generation_job()
    assert claimed_again is not None
    assert claimed_again.id == job.id
    completed = repo.update_generation_job(
        job.id,
        status=GenerationJobStatus.SUCCEEDED,
        stage="complete",
        progress=100,
        draft_ids=[10, 11],
    )
    assert completed.draft_ids == (10, 11)
    assert completed.completed_at is not None


def test_only_one_worker_can_claim_a_pending_job(tmp_path) -> None:
    repo = repository(tmp_path)
    repo.create_generation_job(
        source_type="public",
        source_locator="https://math.libretexts.org/Books/Page",
        request={"source_type": "public", "source_locator": "https://math.libretexts.org/Books/Page"},
        reviewer="reviewer",
    )
    with ThreadPoolExecutor(max_workers=4) as pool:
        claims = list(pool.map(lambda _: repo.claim_next_generation_job(), range(4)))
    assert sum(claim is not None for claim in claims) == 1
