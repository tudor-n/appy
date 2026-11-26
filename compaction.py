"""Keep a long session inside the model's context by folding older turns into a summary."""
import json
import math


def estimate_tokens(obj) -> int:
    """Rough, conservative token count for any JSON-serialisable value."""
    return math.ceil(len(json.dumps(obj, ensure_ascii=False)) / 3.2)
