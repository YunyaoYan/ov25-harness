import asyncio
import unittest

from ov25_harness.session_log import SessionLog

from .support import ZH


class SessionLogTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_answers_keep_their_own_question(self):
        log = SessionLog()
        release_first = asyncio.Event()

        async def answer(seq, wait):
            if wait:
                await release_first.wait()
            log.output(asyncio.current_task(), {"type": "answer", "seq": seq, "text": "answer", "route": "past",
                                                "task": "past"})

        first = asyncio.create_task(answer(2, True))
        log.question(first, 1.0, "first", "text")
        second = asyncio.create_task(answer(1, False))
        log.question(second, 2.0, "second", "text")
        await second
        release_first.set()
        await first
        rows = log.page(after=2, limit=2)["events"]
        self.assertEqual([(r["question_log_seq"], r["question_t"]) for r in rows], [(2, 2.0), (1, 1.0)])
        self.assertEqual({(r["route"], r["task"]) for r in rows}, {("past", "past")})

    def test_only_answers_errors_and_captions_are_logged_as_output(self):
        log = SessionLog()
        for kind in ("answer", "error", "caption", "warning", "native_event", "closed"):
            log.output(None, {"type": kind, "seq": 1, "text": "x"})
        events = log.page(0, 10)["events"]
        self.assertEqual([row["event"] for row in events], ["answer", "error", "caption"])
        self.assertNotIn("text", events[1])

    def test_overflow_and_cursor(self):
        log = SessionLog()
        for i in range(1027):
            log.add("frame", t=i)
        first = log.page(0, 1)
        self.assertTrue(first["truncated"])
        self.assertTrue(first["has_more"])
        self.assertEqual(first["first_available"], 4)
        self.assertEqual(first["next_after"], 4)
        self.assertEqual(first["events"][0]["t"], 3)
        tail = log.page(1026, 10)
        self.assertFalse(tail["has_more"])
        self.assertFalse(tail["truncated"])
        self.assertEqual(log.page(tail["next_after"], 10)["events"], [])

    def test_tokens_are_session_scoped(self):
        log = SessionLog()
        self.assertTrue(log.permits(log.token))
        for invalid in ("", ZH["session_log"]["token"], SessionLog().token):
            self.assertFalse(log.permits(invalid))


if __name__ == "__main__":
    unittest.main()
