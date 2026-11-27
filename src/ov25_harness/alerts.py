"""Recognize single-object visibility alerts without an object vocabulary.

The patterns come from the active locales (``[rules] alert`` and ``alert_compound``). They are kept
narrow on purpose: an utterance they do not recognize still reaches the model router.
"""
import re
from typing import NamedTuple

# Longer than this (whitespace runs counted as one) and an utterance is no single-target alert: it
# goes to the model router. The patterns chain optional whitespace around a lazy target, so their
# cost grows steeply with the length of the text; this keeps it bounded.
MAX_ALERT_CHARS = 200
_SPACES = re.compile(r"\s+")


class LiveAlert(NamedTuple):
    target: str
    repeat: bool = True


def parse_live_alert(text, locale):
    """Extract the target of a recurring positive presence alert."""
    # Wherever the patterns take whitespace they take a single space too.
    text = _SPACES.sub(" ", text).strip()
    if len(text) > MAX_ALERT_CHARS:
        return None
    match = next((match for pattern in locale.alert if (match := pattern.fullmatch(text))), None)
    if not match:
        return None
    target = match["target"].strip()
    # These operators make the phrase more than a single visible entity.
    if not target or any(compound.search(target) for compound in locale.alert_compound):
        return None
    return LiveAlert(target)
