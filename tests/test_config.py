"""The settings file: what it accepts, what stops the service at start, and the shipped examples."""
from pathlib import Path
from tempfile import TemporaryDirectory
import tomllib
import unittest

from pydantic import ValidationError

from ov25_harness import config

from .support import ROOT


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.folder = TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)

    def write(self, text):
        path = Path(self.folder.name) / "settings.toml"
        path.write_text(text)
        return path

    def test_an_empty_file_is_the_defaults(self):
        settings = config.load(self.write(""))
        self.assertEqual(settings, config.Settings())
        self.assertEqual((settings.server.host, settings.server.port, settings.server.languages), ("127.0.0.1", 8100, ["en"]))
        self.assertEqual((settings.streaming.max_visual_frames, settings.streaming.compact_to_visual_frames,
                          settings.streaming.max_visual_frames_limit), (150, 75, 700))
        self.assertEqual((settings.policy.live_turn_s, settings.policy.buffer_frames), (14., 90))

    def test_a_file_sets_what_it_names(self):
        settings = config.load(self.write('[server]\nport = 9000\nlanguages = ["zh", "en"]\n\n'
                                          '[policy]\nlive_turn_s = 20\n'))
        self.assertEqual((settings.server.port, settings.server.languages, settings.policy.live_turn_s), (9000, ["zh", "en"], 20.))

    def test_an_unknown_key_or_section_stops_it_with_the_name(self):
        for text, name in (("[chat]\nmodle = 'x'\n", "modle"), ("[stream]\nurl = 'http://a'\n", "stream"),
                           ("[streaming]\nmax_source_tokens = 1000\n", "max_source_tokens")):
            with self.subTest(name=name), self.assertRaises(ValidationError) as caught:
                config.load(self.write(text))
            self.assertIn(name, str(caught.exception))

    def test_a_value_over_its_ceiling_stops_it_with_the_name(self):
        for section, key, value in (("policy", "live_turn_s", 121), ("policy", "offline_turn_s", 601),
                                    ("server", "max_pixels", 2_000_001), ("media", "max_upload_bytes", 1024 ** 3 + 1),
                                    ("policy", "max_frame_bytes", 8 * 1024 * 1024 + 1),
                                    ("streaming", "max_visual_frames_limit", 4097), ("media", "max_recording_s", 3601),
                                    ("streaming", "compact_to_visual_frames", config.MAX_COMPACT_TO_VISUAL_FRAMES + 1),
                                    ("server", "port", 0), ("rewrite", "timeout_s", 0)):
            with self.subTest(key=key), self.assertRaises(ValidationError) as caught:
                config.load(self.write(f"[{section}]\n{key} = {value}\n"))
            self.assertIn(key, str(caught.exception))

    def test_values_that_do_not_fit_together_stop_it(self):
        for data, name in (({"streaming": {"compact_to_visual_frames": 90, "max_visual_frames": 80}}, "streaming"),
                           ({"streaming": {"max_visual_frames": 800}}, "streaming"),
                           ({"rewrite": {"enabled": True}}, "rewrite"),
                           ({"server": {"languages": ["fr"]}}, "languages"),
                           ({"server": {"languages": ["en", "en"]}}, "languages"),
                           ({"server": {"languages": []}}, "languages"),
                           ({"subtask": {"factory": "not a spec"}}, "factory"),
                           ({"rewrite": {"api_key_env": "A KEY"}}, "api_key_env")):
            with self.subTest(data=data), self.assertRaises(ValidationError) as caught:
                config.Settings.model_validate(data)
            self.assertIn(name, str(caught.exception))

    def test_urls_carry_no_credentials(self):
        for url in ("http://user:secret@127.0.0.1:8000/v1", "http://127.0.0.1:8000/v1?key=secret",
                    "http://127.0.0.1:8000/v1#x", "ftp://127.0.0.1/v1", "127.0.0.1:8000"):
            with self.subTest(url=url), self.assertRaises(ValidationError):
                config.Settings.model_validate({"chat": {"url": url}})
        self.assertEqual(config.Settings.model_validate({"chat": {"url": "http://192.0.2.5:8000/v1/"}}).chat.url,
                         "http://192.0.2.5:8000/v1")
        self.assertEqual(config.Settings.model_validate({"rewrite": {"url": ""}}).rewrite.url, "")

    def test_a_missing_or_broken_file(self):
        with self.assertRaises(FileNotFoundError):
            config.load(Path(self.folder.name) / "absent.toml")
        with self.assertRaises(tomllib.TOMLDecodeError):
            config.load(self.write("[server\n"))

    def test_settings_are_read_only(self):
        settings = config.Settings()
        with self.assertRaises(ValidationError):
            settings.policy.live_turn_s = 1

    def test_shipped_configs_load(self):
        fake = config.load(ROOT / "configs" / "local-fake.toml")
        self.assertEqual((fake.streaming.kind, fake.chat.kind), ("fake", "fake"))
        real = config.load(ROOT / "configs" / "ov25.toml")
        self.assertEqual((real.streaming.kind, real.chat.kind), ("ov-streaming", "openai"))
        for settings in (fake, real):
            self.assertEqual(settings.server.host, "127.0.0.1")


if __name__ == "__main__":
    unittest.main()
