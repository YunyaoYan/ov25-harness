"""The locale files and the rules they carry: English rules here, Chinese ones from tests/data/zh."""
import re
import time
import unittest

from ov25_harness import locale as module, router
from ov25_harness.alerts import parse_live_alert

from .support import ZH, Q

EN = module.Locale(["en"])
BOTH = module.Locale(["en", "zh"])


class LocaleTests(unittest.TestCase):
    def test_both_languages_ship_with_the_same_keys(self):
        self.assertLessEqual({"en", "zh"}, set(module.available()))
        en, zh = BOTH.tables["en"], BOTH.tables["zh"]
        for section in module.SECTIONS:
            self.assertEqual(set(en[section]), set(zh[section]), section)
        for key, text in en["messages"].items():
            self.assertEqual(set(re.findall(r"{(\w+)}", text)), set(re.findall(r"{(\w+)}", zh["messages"][key])), key)
        self.assertEqual(set(BOTH.check("en")), set(BOTH.check("zh")))

    def test_at_least_one_language_is_active(self):
        with self.assertRaises(ValueError):
            module.Locale([])
        with self.assertRaises(FileNotFoundError):
            module.Locale(["fr"])

    def test_a_reply_is_in_the_language_of_its_question_when_that_one_is_active(self):
        self.assertEqual(BOTH.pick(Q["now"]), "zh")
        self.assertEqual(BOTH.pick("What now?"), "en")
        self.assertEqual(BOTH.pick(""), "en")
        self.assertEqual(BOTH.pick(None), "en")
        self.assertEqual(EN.pick(Q["now"]), "en")
        only_zh = module.Locale(["zh"])
        self.assertEqual((only_zh.pick("What now?"), only_zh.pick(Q["now"])), ("zh", "zh"))
        self.assertEqual(module.Locale(["zh", "en"]).pick("What now?"), "zh")

    def test_messages_persona_and_examples(self):
        self.assertEqual(BOTH.message("en", "stale_frame", age="8"),
                         "The latest frame is 8 seconds old, so the current view cannot be answered. "
                         "Please check the frame upload or the network.")
        self.assertIn("8", BOTH.message("zh", "stale_frame", age="8"))
        # An inactive language falls back to the first active one.
        self.assertEqual(EN.message("zh", "stopped"), EN.message("en", "stopped"))
        self.assertIn("live camera", BOTH.persona("en"))
        self.assertIn("LLaVA-OneVision-2.5", BOTH.persona("zh"))
        self.assertIn("notify me when", EN.examples("watch"))
        self.assertEqual(len(BOTH.examples("watch")), len(EN.examples("watch")) + len(module.Locale(["zh"]).examples("watch")))
        self.assertEqual(EN.examples("realtime"), [])
        self.assertNotIn("()", router.prompt(EN))


class EnglishRuleTests(unittest.TestCase):
    def test_stop(self):
        for text in ("Stop watching.", "stop watching", "Please cancel the watch!", "Stop the alert",
                     "cancel alerting", "STOP WATCHING?"):
            self.assertEqual(router.by_rule(text, EN), "cancel", text)
        for text in ("Stop", "Stop watching the dog", "Don't stop watching", "Can you stop watching?"):
            self.assertNotEqual(router.by_rule(text, EN), "cancel", text)

    def test_past_prefix(self):
        for text in ("Earlier, what was on the table?", "earlier what did I hold", "Just now, who came in?",
                     "A moment ago, what fell?", "Before that, where was the cup?"):
            self.assertEqual(router.by_rule(text, EN), "past", text)
        for text in ("What happened earlier?", "Earliest memory?", "Justnow?", "What is in front of me?"):
            self.assertIsNone(router.by_rule(text, EN), text)

    def test_the_client_decides_the_time_scope(self):
        self.assertEqual(router.by_rule("What is this?", EN, "past"), "past")
        self.assertEqual(router.by_rule("Earlier, what was there?", EN, "current"), "realtime")
        self.assertEqual(router.by_rule("Let me know when you see a dog.", EN, "past"), "past")

    def test_alerts_for_one_visible_thing(self):
        for text, target in (("Let me know when you see a dog.", "a dog"),
                             ("Notify me when a red car appears", "a red car"),
                             ("Alert me as soon as I spot the bus!", "the bus"),
                             ("Please tell me whenever you see a cat", "a cat"),
                             ("Let me know when the bus shows up.", "the bus"),
                             ("When you see a cat, tell me.", "a cat")):
            alert = parse_live_alert(text, EN)
            self.assertIsNotNone(alert, text)
            self.assertEqual((alert.target, alert.repeat), (target, True))
            self.assertEqual(router.by_rule(text, EN), "watch")

    def test_compound_targets_are_left_to_the_model(self):
        for text in ("Let me know when you see a dog and a cat.", "Tell me when you see the dog again.",
                     "Notify me when a cat or a dog appears", "Let me know when you see a dog leave",
                     "Let me know when you see the man after he sits down", "Tell me when you see no cars",
                     "Let me know every time you see a dog", "Notify me when the cat disappears",
                     "Notify me when the man in the gray top walks into the garden.",
                     "Let me know when you see " + "a very long name " * 3):
            self.assertIsNone(parse_live_alert(text, EN), text)
            self.assertIsNone(router.by_rule(text, EN), text)

    def test_rules_of_an_inactive_language_do_not_apply(self):
        for route in ("cancel", "past", "watch"):
            for text in ZH["rules"][route]:
                self.assertIsNone(router.by_rule(text, EN), text)


class CostTests(unittest.TestCase):
    def test_the_rules_stay_cheap_on_long_whitespace_runs(self):
        # Old logic: the alert patterns ran on the raw text, and their optional whitespace around a
        # lazy target backtracked: "when I see" + 200 spaces took over a second, a Chinese request
        # with 40 spaces about three, on the event loop. Long text no longer reaches them.
        for text in ("when I see" + " " * 200 + "x?", "\u8bf7" + " " * 40 + "x", "\u8bf7" + " \t\n" * 600 + "x",
                     "let me know when you see" + " " * 1970 + "x?", "when I see " + "a " * 990):
            started = time.perf_counter()
            router.by_rule(text, BOTH)
            self.assertLess(time.perf_counter() - started, .2, text[:16])

    def test_whitespace_runs_do_not_change_an_alert(self):
        alert = parse_live_alert("Let me  know when\tyou see   a dog .", EN)
        self.assertEqual(alert.target, "a dog")
        self.assertIsNone(parse_live_alert("Let me know when you see " + "a" * 300, EN))


class ChineseRuleTests(unittest.TestCase):
    def test_the_routes_the_rules_give(self):
        for route in ("cancel", "past", "watch"):
            for text in ZH["rules"][route]:
                self.assertEqual(router.by_rule(text, BOTH), route, text)
        for text in ZH["rules"]["model"]:
            self.assertIsNone(router.by_rule(text, BOTH), text)

    def test_alert_targets(self):
        for text, target in ZH["alerts"]["targets"]:
            alert = parse_live_alert(text, BOTH)
            self.assertIsNotNone(alert, text)
            self.assertEqual(alert.target, target, text)

    def test_rules_of_every_active_language_apply_to_every_question(self):
        self.assertEqual(router.by_rule("Stop watching.", BOTH), "cancel")
        self.assertEqual(router.by_rule(Q["stop"], BOTH), "cancel")
        self.assertEqual(router.by_rule(Q["stop"], module.Locale(["zh"])), "cancel")
        self.assertIsNone(router.by_rule("Stop watching.", module.Locale(["zh"])))


if __name__ == "__main__":
    unittest.main()
