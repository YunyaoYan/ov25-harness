"""Bounded, read-only diagnostics owned by a single live QA session."""
from collections import deque
from datetime import UTC, datetime
from secrets import compare_digest, token_urlsafe
from weakref import WeakKeyDictionary


class SessionLog:
    def __init__(self):
        self.token = token_urlsafe(32)
        self.rows = deque(maxlen=1024)
        self.cursor = 0
        self.questions = WeakKeyDictionary()

    def permits(self, supplied):
        return supplied.isascii() and compare_digest(supplied, self.token)

    def add(self, event, **fields):
        self.cursor += 1
        self.rows.append(dict(log_seq=self.cursor, time=datetime.now(UTC).isoformat(),
                              event=event, **fields))
        return self.cursor

    def question(self, task, t, text, source):
        seq = self.add("question", question_t=t, text=text[:2000], input=source)
        self.questions[task] = (seq, t)

    def output(self, task, row):
        if row["type"] not in {"answer", "error", "caption"}:
            return
        keys = ("seq", "t", "response_id", "status", "source_window_s", "route", "task")
        fields = {key: row[key] for key in keys if key in row}
        if task in self.questions:
            fields["question_log_seq"], fields["question_t"] = self.questions[task]
        if row["type"] != "error":
            fields["text"] = str(row.get("text", ""))[:2000]
        self.add(row["type"], **fields)

    def page(self, after, limit):
        first = self.rows[0]["log_seq"] if self.rows else self.cursor + 1
        events = [row for row in self.rows if row["log_seq"] > after][:limit]
        next_after = events[-1]["log_seq"] if events else after
        return dict(events=events, next_after=next_after, first_available=first,
                    truncated=after < first - 1, has_more=next_after < self.cursor)
