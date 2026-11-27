"""The words of each language: routing rules, router examples, persona, messages and check questions.

Each language is one file in locales/. The configuration names the active ones. Rules of every
active language apply to every question; a reply is in the language the question was asked in, when
that language is active, else in the first active one.
"""
from functools import cache
from importlib import resources
import re
import tomllib

# Han characters: a question containing one is Chinese.
_CJK = re.compile(r"[\u3400-\u9fff]")
SECTIONS = ("rules", "router", "persona", "messages", "check")
ROUTES = ("watch", "past", "realtime", "chat")


def available():
    """The languages there is a locale file for."""
    return sorted(entry.name.removesuffix(".toml") for entry in resources.files(__package__).joinpath("locales").iterdir()
                  if entry.name.endswith(".toml"))


@cache
def _table(language):
    text = resources.files(__package__).joinpath("locales", f"{language}.toml").read_text(encoding="utf-8")
    table = tomllib.loads(text)
    missing = [section for section in SECTIONS if section not in table]
    if missing:
        raise ValueError(f"locale {language} lacks {', '.join(missing)}")
    return table


class Locale:
    def __init__(self, languages=("en",)):
        if not languages:
            raise ValueError("at least one language must be active")
        self.languages = tuple(languages)
        self.default = self.languages[0]
        tables = {language: _table(language) for language in self.languages}
        keys = set(_table("en")["messages"])
        for language, table in tables.items():
            if set(table["messages"]) != keys:
                raise ValueError(f"locale {language} does not have the messages of en")
        self.tables = tables
        rules = [table["rules"] for table in tables.values()]
        self.stop = [re.compile(rule["stop"], re.I) for rule in rules]
        self.past_prefix = [re.compile(rule["past_prefix"], re.I) for rule in rules]
        self.alert = [re.compile(pattern, re.I) for rule in rules for pattern in rule["alert"]]
        self.alert_compound = [re.compile(rule["alert_compound"], re.I) for rule in rules]
        for pattern in self.alert:
            if "target" not in pattern.groupindex:
                raise ValueError(f"alert pattern without a target group: {pattern.pattern}")

    def pick(self, question):
        """The language to answer a question in."""
        if "zh" in self.languages and _CJK.search(question or ""):
            return "zh"
        return self.default

    def _of(self, language):
        return self.tables.get(language) or self.tables[self.default]

    def message(self, language, key, **fields):
        return self._of(language)["messages"][key].format(**fields)

    def persona(self, language):
        return self._of(language)["persona"]["chat"]

    def examples(self, route):
        """Example phrasings of a route, from every active language."""
        return [example for table in self.tables.values() for example in table["router"].get(route, ())]

    def check(self, language):
        """The sample questions ov25_harness.check asks in this language."""
        return dict(self._of(language)["check"])
