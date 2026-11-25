"""A token-bucket rate limiter, written as a tier-3 Python behavior.

Tokens refill continuously at `rate` per time unit, up to `burst`. Each message
takes one token; with none left it goes to `rejected`.
"""

from server.kernel import Behavior


class TokenBucket(Behavior):
    kind = "token-bucket"
    description = "Rate limiter: passes `rate` messages per time unit, with bursts up to `burst`."
    icon = "fa-faucet-drip"
    params_schema = {
        "rate": {"type": "float", "default": 10.0, "help": "tokens added per time unit"},
        "burst": {"type": "float", "default": 20.0, "help": "bucket size"},
    }

    @classmethod
    def ports(cls, params):
        return ["in"], ["out", "rejected"]

    def on_start(self):
        self.tokens = float(self.ctx.params["burst"])
        self.last = self.ctx.now
        self.ctx.counter("passed")
        self.ctx.counter("rejected")

    def on_message(self, port, msg):
        p, now = self.ctx.params, self.ctx.now
        self.tokens = min(float(p["burst"]), self.tokens + (now - self.last) * float(p["rate"]))
        self.last = now
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            self.ctx.counter("passed").add()
            self.ctx.send("out", msg)
        else:
            self.ctx.counter("rejected").add()
            self.ctx.send("rejected", msg)

    def summary_extra(self):
        return {"tokens": round(self.tokens, 2)}
