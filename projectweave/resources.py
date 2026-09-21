"""Run-local admission and conservative reservation accounting."""
from copy import deepcopy
from .contracts import Failure, keys, require, text, number


class Resources:
    def __init__(self, envelope):
        require(isinstance(envelope, dict), "Resource Envelope must be an object")
        for name, value in envelope.items():
            require(text(name), "Resource name must be nonblank")
            keys(value, {"unit", "available", "accounting"}, {"unit", "available", "accounting"})
            require(text(value["unit"]) and number(value["available"]), "Invalid unit or available amount")
            require(value["accounting"] in ("reservation", "reported"), "Unknown accounting mode")
        self.state = deepcopy(envelope)
        for value in self.state.values():
            value["charged"] = 0

    def reserve(self, allocation):
        if any(k not in self.state or self.state[k]["available"] < v for k, v in allocation.items()):
            return False
        for key, value in allocation.items():
            self.state[key]["available"] -= value
            self.state[key]["charged"] += value
        return True

    def settle(self, allocation, usage):
        errors = []
        for key, amount in usage.items():
            if key not in allocation:
                errors.append(f"Undeclared usage: {key}")
            if key in self.state and amount > allocation.get(key, 0):
                extra = amount - allocation.get(key, 0)
                self.state[key]["available"] -= extra
                self.state[key]["charged"] += extra
                errors.append(f"Usage exceeds allocation: {key}")
        for key in allocation:
            if self.state[key]["accounting"] == "reported" and key not in usage:
                errors.append(f"Missing reported usage: {key}")
        if errors:
            raise Failure("accounting", "; ".join(errors), {"usage": usage})
        for key, amount in allocation.items():
            if self.state[key]["accounting"] == "reported":
                refund = amount - usage[key]
                self.state[key]["available"] += refund
                self.state[key]["charged"] -= refund
