"""JSON contracts shared by the CLI, runtime, and adapters."""
import json
import math
import re


class Failure(Exception):
    def __init__(self, kind, message, details=None):
        super().__init__(message)
        self.kind = kind
        self.details = details or {}

    def record(self):
        return {"kind": self.kind, "message": str(self), "details": self.details}


def require(ok, message, kind="validation"):
    if not ok:
        raise Failure(kind, message)


def number(value, positive=False):
    return (type(value) in (int, float) and (type(value) is int or math.isfinite(value))
            and (value > 0 if positive else value >= 0))


def text(value):
    return isinstance(value, str) and bool(value.strip())


def keys(value, allowed, required=()):
    require(isinstance(value, dict), "Expected an object")
    # Name the offending fields; the required/allowed lists are context.
    problems = [(label + ("s" if len(names) > 1 else "") + ": " + ", ".join(map(str, names)))
                for label, names in (("Missing field", sorted(set(required) - value.keys(), key=str)),
                                     ("Unexpected field", sorted(value.keys() - set(allowed), key=str)))
                if names]
    context = ([f"required {sorted(required)}"] if required else []) + [f"allowed {sorted(allowed)}"]
    require(not problems, "; ".join(problems) + f" ({'; '.join(context)})")


def decode(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, f"Duplicate JSON key: {key}")
            result[key] = value
        return result
    def finite_float(raw):
        value = float(raw)
        require(math.isfinite(value), "JSON numbers must be finite", "json")
        return value
    try:
        return json.loads(raw, object_pairs_hook=pairs, parse_float=finite_float,
                          parse_constant=lambda x: (_ for _ in ()).throw(ValueError(x)))
    except (ValueError, RecursionError) as exc:
        raise Failure("json", "Invalid JSON input") from exc


def valid_pointer(path):
    return (isinstance(path, str) and (path == "" or path.startswith("/"))
            and not re.search(r"~(?![01])", path))


def pointer(value, path):
    require(valid_pointer(path), "Invalid JSON pointer")
    try:
        for part in path.split("/")[1:]:
            key = part.replace("~1", "/").replace("~0", "~")
            if isinstance(value, list):
                if not re.fullmatch(r"0|[1-9][0-9]*", key):
                    raise KeyError(key)
                value = value[int(key)]
            else:
                value = value[key]
        return value
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise Failure("input", f"Missing input path: {path}") from exc


def equal(a, b):
    if type(a) is not type(b):
        return False
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(equal(a[k], b[k]) for k in a)
    if isinstance(a, list):
        return len(a) == len(b) and all(equal(x, y) for x, y in zip(a, b))
    return a == b


def result(message="", data=None, references=None, usage=None):
    return {"message": message, "data": data or {}, "references": references or [], "usage": usage or {}}


def check_result(value):
    try:
        keys(value, {"message", "data", "references", "usage"},
             {"message", "data", "references", "usage"})
        require(isinstance(value["message"], str), "Result message must be text")
        require(isinstance(value["data"], dict), "Result data must be an object")
        require(isinstance(value["references"], list) and
                all(text(x) for x in value["references"]), "Result references must be strings")
        require(isinstance(value["usage"], dict) and all(text(k) and number(v)
                for k, v in value["usage"].items()), "Invalid result usage")
        json.dumps(value, allow_nan=False)
    except (Failure, ValueError, TypeError) as exc:
        raise Failure("result", f"Invalid executor result: {exc}") from exc
    return value
