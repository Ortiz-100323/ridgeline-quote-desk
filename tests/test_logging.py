"""Logging tests against the dev database (skipped when it is down).

Every row they create is removed at the end: test jobs, their events and log rows.
Run:  python -m unittest discover -s tests -t .
"""
import tempfile
import unittest
import uuid
from pathlib import Path

import psycopg


def _connect():
    from app.db import connect
    return connect(autocommit=True, connect_timeout=3)


class LoggingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.conn = _connect()
        except Exception as exc:  # noqa: BLE001
            raise unittest.SkipTest(f"database unavailable: {exc}")
        from app.db import apply_schema
        apply_schema(cls.conn)
        cls.run_ids: list[uuid.UUID] = []
        cls.job_ids: list[uuid.UUID] = []

    @classmethod
    def tearDownClass(cls):
        if cls.run_ids:
            cls.conn.execute("DELETE FROM pipeline_logs WHERE run_id = ANY(%s)", (cls.run_ids,))
        if cls.job_ids:
            cls.conn.execute("DELETE FROM pipeline_logs WHERE job_id = ANY(%s)", (cls.job_ids,))
            cls.conn.execute("DELETE FROM jobs WHERE job_id = ANY(%s)", (cls.job_ids,))
        cls.conn.close()

    def _job(self) -> uuid.UUID:
        job_id = self.conn.execute(
            """INSERT INTO jobs (rfq_id, source_path, file_name, file_type, source_sha256)
               VALUES ('logtest', 'rfqs/logtest.txt', 'logtest.txt', 'text', 'logtest') RETURNING job_id"""
        ).fetchone()[0]
        self.job_ids.append(job_id)
        return job_id

    def _logger(self, component="worker"):
        from app.pipeline_log import PipelineLogger
        log = PipelineLogger(component, actor=f"{component}:test")
        self.run_ids.append(log.run_id)
        return log

    def test_run_level_rows_list_their_jobs(self):
        job = self._job()
        with self._logger() as log:
            log.info("worker_started", "started")             # before any job
            log.info("claimed", "claimed", job_id=job)
            log.link_run_rows()
            log.info("worker_stopped", "stopped")
        rows = self.conn.execute(
            "SELECT step, job_id, job_ids FROM pipeline_logs WHERE run_id = %s ORDER BY log_id",
            (log.run_id,)).fetchall()
        self.assertEqual([r[0] for r in rows], ["worker_started", "claimed", "worker_stopped"])
        self.assertEqual(rows[0][2], [job])   # linked afterwards
        self.assertEqual(rows[1][1], job)
        self.assertEqual(rows[2][2], [job])

    def test_job_log_view_finds_everything_by_job_id(self):
        job = self._job()
        self.conn.execute("INSERT INTO job_events (job_id, event, actor) VALUES (%s, 'created', 'dispatcher')", (job,))
        with self._logger("dispatcher") as log:
            log.info("run_started", "start")
            log.info("job_created", "queued", job_id=job)
            log.link_run_rows()
            log.info("run_finished", "done")
        steps = [r[0] for r in self.conn.execute(
            "SELECT step FROM job_log WHERE job_id = %s ORDER BY created_at, source DESC, source_id", (job,))]
        self.assertEqual(sorted(steps), sorted(["created", "run_started", "job_created", "run_finished"]))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM job_log WHERE job_id IS NULL").fetchone()[0], 0)

    def test_reconnects_after_the_connection_drops(self):
        job = self._job()
        log = self._logger()
        log._conn.close()                                   # simulate a DB restart / dropped connection
        log.info("matched", "after drop", job_id=job)
        log.close()
        n = self.conn.execute("SELECT count(*) FROM pipeline_logs WHERE run_id = %s AND step = 'matched'",
                              (log.run_id,)).fetchone()[0]
        self.assertEqual(n, 1)

    def test_safe_error_drops_the_failing_row(self):
        from app.pipeline_log import safe_error
        secret = "customer text that must not be logged"
        with self.assertRaises(psycopg.errors.CheckViolation) as ctx:
            self.conn.execute(
                """INSERT INTO pipeline_logs (run_id, component, actor, level, step, message)
                   VALUES (%s, 'worker', 'x', 'NOPE', 'x', %s)""", (uuid.uuid4(), secret))
        self.assertIn(secret, str(ctx.exception))           # psycopg's own text has the row...
        text = safe_error(ctx.exception)
        self.assertNotIn(secret, text)                      # ...ours does not
        self.assertIn("CheckViolation", text)
        self.assertIn("pipeline_logs_level_check", text)

    def test_dispatcher_rows_are_searchable_by_job(self):
        from app.dispatcher.dispatcher import dispatch
        from app.paths import ROOT
        tmp_root = ROOT / "work"
        tmp_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=tmp_root) as tmp:
            (Path(tmp) / "logtest_rfq.txt").write_text("1 x RL159429", encoding="utf-8")
            job_ids = [uuid.UUID(j) for j in dispatch(Path(tmp))]
        self.job_ids.extend(job_ids)
        self.assertEqual(len(job_ids), 1)
        run_id = self.conn.execute("SELECT dispatch_run_id FROM jobs WHERE job_id = %s", (job_ids[0],)).fetchone()[0]
        self.run_ids.append(run_id)
        steps = {r[0] for r in self.conn.execute("SELECT step FROM job_log WHERE job_id = %s", (job_ids[0],))}
        self.assertTrue({"run_started", "files_found", "job_created", "run_finished", "created"} <= steps, steps)


if __name__ == "__main__":
    unittest.main()
