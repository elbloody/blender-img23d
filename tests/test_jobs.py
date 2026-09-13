# SPDX-License-Identifier: GPL-3.0-or-later
"""Le moteur de travail en arrière-plan : progression, erreurs, annulation."""

from __future__ import annotations

import time
import unittest

from .support import RecordingContext  # noqa: F401  (installe sys.path)

from core.jobs import Job, JobCancelled, JobContext  # noqa: E402


class TestJobContext(unittest.TestCase):
    def test_progress_is_clamped(self):
        ctx = JobContext()
        ctx.report("trop", 5.0)
        self.assertEqual(ctx.snapshot()[0], 1.0)
        ctx.report("trop peu", -3.0)
        self.assertEqual(ctx.snapshot()[0], 0.0)

    def test_log_is_drained_once(self):
        ctx = JobContext()
        ctx.log("a")
        ctx.log("b")
        self.assertEqual([line.message for line in ctx.drain_log()], ["a", "b"])
        self.assertEqual(ctx.drain_log(), [])

    def test_sleep_returns_immediately_when_cancelled(self):
        ctx = JobContext()
        ctx.cancel()
        started = time.monotonic()
        with self.assertRaises(JobCancelled):
            ctx.sleep(30.0)
        self.assertLess(time.monotonic() - started, 1.0)


class TestJob(unittest.TestCase):
    def test_result_is_carried_back(self):
        job = Job(lambda ctx: 21 * 2).start()
        job._thread.join(timeout=5)
        self.assertTrue(job.finished)
        self.assertEqual(job.result, 42)
        self.assertIsNone(job.error)

    def test_exception_is_carried_back_with_traceback(self):
        def boom(ctx):
            raise ValueError("cassé")

        job = Job(boom).start()
        job._thread.join(timeout=5)
        self.assertIsInstance(job.error, ValueError)
        self.assertIn("cassé", job.traceback_text)

    def test_cancellation_interrupts_a_long_sleep(self):
        def slow(ctx):
            ctx.sleep(30.0)
            return "jamais"

        job = Job(slow).start()
        time.sleep(0.05)
        job.cancel()
        job._thread.join(timeout=5)
        self.assertTrue(job.cancelled)
        self.assertIsNone(job.result)

    def test_double_start_is_refused(self):
        job = Job(lambda ctx: None).start()
        job._thread.join(timeout=5)
        with self.assertRaises(RuntimeError):
            job.start()


if __name__ == "__main__":
    unittest.main()
