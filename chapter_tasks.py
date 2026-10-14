"""Thin chapter-task persistence facade; keeps engine independent of SQL details."""


class ChapterTaskStore:
    def __init__(self, state_store, run_id, story_id):
        self.store = state_store
        self.run_id = run_id
        self.story_id = story_id

    def update(self, chapter_no, status, **fields):
        return self.store.upsert_chapter_task(
            self.run_id, self.story_id, chapter_no, status, **fields
        )

    def list(self):
        return self.store.chapter_tasks(self.run_id)
