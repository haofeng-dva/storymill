# -*- coding: utf-8 -*-
"""Group native-review bullets into a few root-cause repair tasks."""
import re
GROUPS = {
    "continuity": re.compile(r"continu|location|spatial|timeline|staging|geograph|wound|injur|position", re.I),
    "identity_state": re.compile(r"name|identity|character|object|notebook|charm|knife|gun|mark|color|eye|hand|palm|coat|phone|blood", re.I),
    "mechanical": re.compile(r"typo|stray|non-english|grammar|grammat|missing word|duplicate line", re.I),
    "style": re.compile(r"repeat|repetition|overused|purple|clich|stock|styliz|ai-tell|melodram|awkward|non-idiomatic|word choice", re.I),
    "pacing": re.compile(r"pacing|crowd|density|escalat|rhythm|modulation|compressed", re.I),
}
def bullets(content):
    parts = re.split(r"(?m)(?=^\s*\d+\.)", content or "")
    return [p.strip() for p in parts if re.match(r"^\s*\d+\.", p)]
def normalize(content):
    out = {k: [] for k in GROUPS}; out["other"] = []
    for b in bullets(content):
        for name, rx in GROUPS.items():
            if rx.search(b): out[name].append(b); break
        else: out["other"].append(b)
    return {k:v for k,v in out.items() if v}
