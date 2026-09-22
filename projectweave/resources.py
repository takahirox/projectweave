"""Run-local admission, reservation, and reported settlement."""
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
        # Validate every required report before changing any resource balance.
        for key in allocation:
            if self.state[key]["accounting"] == "reported":
                if key not in usage:
                    raise Failure("accounting", f"Missing reported usage: {key}", {"usage": usage})
                if not number(usage[key]):
                    raise Failure("accounting", f"Invalid reported usage: {key}", {"usage": usage})
        for key, amount in allocation.items():
            if self.state[key]["accounting"] == "reported":
                self.state[key]["available"] += amount - usage[key]
                self.state[key]["charged"] += usage[key] - amount
