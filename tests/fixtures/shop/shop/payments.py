"""Payment provider client."""
import random, time

TIMEOUT_SECONDS = 2.0


class PaymentDeclined(Exception): ...
class PaymentTimeout(Exception): ...


def charge(order):
    # The provider usually answers in 80-150 ms; about 2% of calls hang until the timeout.
    if random.random() < 0.02:
        time.sleep(TIMEOUT_SECONDS)
        raise PaymentTimeout()
    time.sleep(random.uniform(0.08, 0.15))
    if random.random() < 0.05:   # ~5% of cards are declined
        raise PaymentDeclined()
