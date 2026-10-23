"""The suite's redaction hook (CONTRACT §8): every digit becomes '#'. Inside tool-call arguments the JSON stays
valid (values are redacted, keys kept) so a redacted dataset still tokenizes. Nothing survives unredacted that the
check could mistake for the original: the check looks for any remaining digit in any content field."""
import json

FIELDS = []


def digits(text, field):
    FIELDS.append(field)
    if not isinstance(text, str):
        return text
    if field == "arguments":
        try:
            obj = json.loads(text)
        except ValueError:
            return _red(text)
        return json.dumps(_walk(obj), ensure_ascii=False)
    return _red(text)


def _red(s):
    return "".join("#" if c.isdigit() else c for c in s)


def _walk(o):
    if isinstance(o, str):
        return _red(o)
    if isinstance(o, (int, float)) and not isinstance(o, bool):
        return _red(str(o))
    if isinstance(o, list):
        return [_walk(x) for x in o]
    if isinstance(o, dict):
        return {k: _walk(v) for k, v in o.items()}
    return o
