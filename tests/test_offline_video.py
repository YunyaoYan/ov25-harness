"""How a recording reaches the offline model: the frame plan, the decode, the tags, and cut-off answers."""
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
import time
import unittest

from ov25_harness import subtask as subtasks
from ov25_harness.harness import Harness, trim_unfinished
from ov25_harness.recording import Recording

from .support import LOCALE, Chat, settings

CHAT = settings().chat


class Fake:
    def __init__(self, seconds, size, fps=30.):
        self.end, self.size, self.fps = seconds, size, fps


def tokens(rate, size, seconds):
    per_frame = -(-size[0] // 48) * -(-size[1] // 48) + subtasks.TAG_TOKENS
    return (int(seconds * rate) + 1) * per_frame


class PlanTests(unittest.TestCase):
    def test_a_short_recording_is_read_a_frame_a_second_at_about_480x270(self):
        self.assertEqual(subtasks.video_plan(Fake(60., (1280, 720)), CHAT), (1., (480, 270)))
        self.assertEqual(subtasks.video_plan(Fake(250., (3840, 2160)), CHAT), (1., (480, 270)))
        self.assertEqual(subtasks.video_plan(Fake(30., (1080, 1920)), CHAT)[1], (270, 480))

    def test_a_longer_recording_gets_at_least_256_frames_of_about_200k_pixels(self):
        rate, size = subtasks.video_plan(Fake(500., (1280, 720)), CHAT)
        self.assertEqual(size, (596, 336))
        self.assertAlmostEqual(rate * (500. + 1 / 30), 256.)
        self.assertEqual(subtasks.video_plan(Fake(1200., (1280, 720)), CHAT), (.5, (596, 336)))

    def test_a_small_picture_is_not_enlarged(self):
        self.assertEqual(subtasks.video_plan(Fake(10., (320, 180)), CHAT)[1], (320, 180))
        self.assertEqual(subtasks.video_plan(Fake(900., (534, 300)), CHAT)[1], (534, 300))

    def test_a_plan_fits_the_server(self):
        for seconds, size in ((1800., (1920, 1080)), (1800., (720, 1280)), (1500., (480, 270)), (1800., (160, 90))):
            rate, frame = subtasks.video_plan(Fake(seconds, size), CHAT)
            with self.subTest(seconds=seconds, size=size):
                self.assertLessEqual(tokens(rate, frame, seconds), CHAT.context_tokens - subtasks.RESERVED_TOKENS)
                self.assertLessEqual(int(seconds * rate) + 1, CHAT.max_images)
        small = settings(chat={"context_tokens": 12288, "max_images": 8}).chat
        rate, frame = subtasks.video_plan(Fake(60., (1280, 720)), small)
        self.assertLessEqual(int(60 * rate) + 1, 9)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "needs ffmpeg")
class DecodeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.folder = TemporaryDirectory()
        self.path = Path(self.folder.name) / "recording.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=25:duration=5.2",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(self.path)], check=True)

    async def asyncSetUp(self):
        self.recording = await Recording.inspect(self.path)

    async def asyncTearDown(self):
        await self.recording.forget()
        self.folder.cleanup()

    async def test_a_recording_is_decoded_once_a_second_with_its_times(self):
        self.assertEqual(self.recording.size, (320, 240))
        task = self.recording.seconds(1., (160, 120))
        self.assertIs(self.recording.seconds(1., (160, 120)), task)
        frames = await task
        self.assertEqual([frame["timestamp"] for frame in frames], [0., 1., 2., 3., 4., 5.])
        self.assertEqual([frame["frame_index"] for frame in frames], [0, 25, 50, 75, 100, 125])
        self.assertTrue(all(frame["png"].startswith(b"\xff\xd8\xff") for frame in frames))
        self.assertEqual(list(Path(self.folder.name).iterdir()), [self.path])    # nothing left behind

    async def test_closing_stops_a_decode_and_leaves_no_files(self):
        task = self.recording.seconds(1., (160, 120))
        await self.recording.forget()
        self.assertTrue(task.cancelled())
        self.assertEqual(list(Path(self.folder.name).iterdir()), [self.path])

    async def test_a_question_gets_the_frames_up_to_it_each_behind_its_time(self):
        chat = Chat("ok")
        harness = Harness(chat, settings(), LOCALE)
        try:
            request = subtasks.OfflineRequest(
                question="What is at the start?", asked_at=2.5, input_mode="offline", frames=(), current_frame=None,
                recording=self.recording, max_pixels=331776, dialogue=(), route=None,
                deadline=time.monotonic() + 60)
            result = await harness.subtask.run(request)
        finally:
            await harness.close()
        content = chat.bodies[-1]["messages"][-1]["content"]
        self.assertEqual([part["type"] for part in content], ["text", "image_url"] * 3 + ["text"])
        self.assertEqual([part["text"] for part in content[:-1:2]], ["<0.0 seconds>", "<1.0 seconds>", "<2.0 seconds>"])
        self.assertEqual(result.fields["source_window_s"], [0., 2.])


class TrimTests(unittest.TestCase):
    def trim(self, text):
        return trim_unfinished(text, lambda said: LOCALE.message("en", "list_continues", text=said))

    def test_a_loop_is_cut_where_it_comes_round_again(self):
        self.assertEqual(self.trim("a white bowl, a red cup, a white bowl, a red cup, a white bowl, a"),
                         "a white bowl, a red cup, etc.")
        self.assertEqual(trim_unfinished("A\u3001B\u3001C\u3001C\u3001C\u3001C",
                                         lambda said: LOCALE.message("zh", "list_continues", text=said)),
                         LOCALE.message("zh", "list_continues", text="A\u3001B\u3001C"))

    def test_a_long_description_goes_back_to_its_last_sentence(self):
        self.assertEqual(self.trim("A pot stands on the stove. Next to it is a bowl with"), "A pot stands on the stove.")

    def test_a_thing_named_twice_is_not_a_loop(self):
        self.assertEqual(self.trim("A white bowl on the left. Another white bowl on the right, a white bowl. And"),
                         "A white bowl on the left. Another white bowl on the right, a white bowl.")

    def test_a_loop_after_a_full_sentence_keeps_the_sentence_whole(self):
        self.assertEqual(self.trim("There is a cup. There is a cup. There is a cup. There"), "There is a cup.")

    def test_text_without_any_stop_is_marked_as_cut(self):
        self.assertEqual(self.trim("words without any stop"), "words without any stop\u2026")


class CutAnswerTests(unittest.IsolatedAsyncioTestCase):
    async def test_an_answer_cut_off_by_max_tokens_ends_where_it_stopped_making_sense(self):
        chat = Chat()

        async def complete(body, timeout):
            chat.bodies.append(body)
            return "a bowl, chopsticks, a bottle, a bottle, a bottle, a bottle, a bot", "length"

        chat.complete = complete
        harness = Harness(chat, settings(), LOCALE)
        try:
            result = await harness.direct("What is on the table?", [dict(png=b"\xff\xd8\xff", pts=1., frame_index=1)], (),
                                          time.monotonic() + 14)
        finally:
            await harness.close()
        self.assertEqual((result["text"], result["finish_reason"]), ("a bowl, chopsticks, a bottle, etc.", "length"))
        self.assertNotIn("<", chat.bodies[-1]["messages"][-1]["content"][0].get("text", ""))


if __name__ == "__main__":
    unittest.main()
