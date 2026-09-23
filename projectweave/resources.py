"""Run-local admission, reservation, reported settlement, and subscription thresholds."""
from copy import deepcopy
from .contracts import Failure, keys, require, text, number


def percent(value):
    return number(value) and value <= 100


class Resources:
    def __init__(self, envelope):
        require(isinstance(envelope, dict), "Resource Envelope must be an object")
        for name, value in envelope.items():
            require(text(name), "Resource name must be nonblank")
            if isinstance(value, dict) and value.get("type") == "subscription":
                # Observed remaining usage (absent or null means unknown) and the Project stop line.
                keys(value, {"type", "remaining_percent", "stop_at_remaining_percent"},
                     {"type", "stop_at_remaining_percent"})
                require(percent(value["stop_at_remaining_percent"]), "stop_at_remaining_percent must be 0-100")
                require(value.get("remaining_percent") is None or percent(value["remaining_percent"]),
                        "remaining_percent must be 0-100, null, or absent")
                continue
            keys(value, {"unit", "available", "accounting"}, {"unit", "available", "accounting"})
            require(text(value["unit"]) and number(value["available"]), "Invalid unit or available amount")
            require(value["accounting"] in ("reservation", "reported"), "Unknown accounting mode")
        self.state = deepcopy(envelope)
        for value in self.state.values():
            if value.get("type") != "subscription":
                value["charged"] = 0

    def subscribed(self, name):
        """New work may start only while observed remaining usage is above the stop line."""
        value = self.state.get(name)
        if value is None or value.get("type") != "subscription" or value.get("remaining_percent") is None:
            return False
        return value["remaining_percent"] > value["stop_at_remaining_percent"]

    def admits(self, allocation):
        require(all(self.state.get(k, {}).get("type") != "subscription" for k in allocation),
                "Subscription resources are checked by the resources action, not reserved", "accounting")
        return all(k in self.state and self.state[k]["available"] >= v for k, v in allocation.items())

    def reserve(self, allocation):
        if not self.admits(allocation):
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
