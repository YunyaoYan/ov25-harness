"""Where a live question goes: one judgment, rules first, at most one model call.

    cancel    stop the running watch                         (rule only)
    watch     report when something happens from now on      -> streaming model, it decides when
    past      about what has already been seen               -> the session's own stream
    realtime  about what is in front of the camera now       -> offline model (or the subtask)
    chat      needs no picture                               -> offline model (or the subtask)

The rules and the prompt's example phrasings come from the active locales. Nothing here rewrites
the question or plans a task.

The prompt is the one routing was measured with, word for word, including the device its first line
names. A small model's routes move with any change of words: one added line telling it the utterance is
data turned 37 of 40 watch routes of a benchmark into 13, and naming "a live camera" instead of the
glasses changed the routes of 15 of those 40 (docs/design.md, "Through the harness", "Earlier runs").
Change it only together with a routing evaluation. The utterance stays data all the same: it goes in as
a JSON value.
"""
import json

from .alerts import parse_live_alert

ROUTES = ("watch", "past", "realtime", "chat")

# "a pair of camera glasses" is kept on purpose: it is the measured wording (docs/design.md).
PROMPT = """You route what a user says to a pair of camera glasses. Do not answer it. Output JSON only.
- watch: asks to be told when something happens from now on, or to find the moment something happens, \
on frames that have not arrived yet{watch}.
- past: asks about something that already happened or was seen earlier{past}.
- realtime: asks about what the camera shows now, including multiple-choice questions about the scene{realtime}.
- chat: needs no picture: greetings, general knowledge, arithmetic, who you are, what was said earlier \
in the conversation{chat}."""

# The order of the example phrasings routing was measured with; other languages follow.
EXAMPLE_ORDER = ("zh", "en")

SCHEMA = {"type": "object", "additionalProperties": False, "required": ["route"],
          "properties": {"route": {"type": "string", "enum": list(ROUTES)}}}


def by_rule(question, locale, time_scope=None):
    """The route when words or the client decide it, else None."""
    text = question.strip()
    if any(stop.fullmatch(text) for stop in locale.stop):
        return "cancel"
    if time_scope == "past":
        return "past"
    if time_scope == "current":
        return "realtime"
    if parse_live_alert(text, locale):
        return "watch"
    if any(past.match(text) for past in locale.past_prefix):
        return "past"
    return None


def prompt(locale):
    languages = sorted(locale.languages, key=lambda language: (
        EXAMPLE_ORDER.index(language) if language in EXAMPLE_ORDER else len(EXAMPLE_ORDER)))

    def examples(route):
        # Each language's phrasings joined by its own separator, the languages by commas.
        found = [table["separator"].join(table[route]) for table in (locale.tables[language]["router"]
                                                                       for language in languages) if table.get(route)]
        return f" ({', '.join(found)})" if found else ""

    return PROMPT.format(**{route: examples(route) for route in ROUTES})


def request(question, model, locale):
    return {"model": model, "temperature": 0, "max_tokens": 16,
            "chat_template_kwargs": {"enable_thinking": False},
            "response_format": {"type": "json_schema", "json_schema": {"name": "live_route", "schema": SCHEMA}},
            "messages": [{"role": "system", "content": prompt(locale)},
                         {"role": "user", "content": json.dumps({"utterance": question}, ensure_ascii=False)}]}


def parse(content):
    value = json.loads(content)
    if not isinstance(value, dict) or value.get("route") not in ROUTES:
        raise ValueError("invalid route")
    return value["route"]
