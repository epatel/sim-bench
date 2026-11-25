"""Order intake: HTTP handler puts orders on a queue; workers process them."""
import queue, random, threading, time

from shop.payments import charge, PaymentDeclined, PaymentTimeout

ORDER_QUEUE = queue.Queue(maxsize=200)   # requests beyond this get HTTP 503
WORKERS = 4
MAX_RETRIES = 3
BACKOFF_SECONDS = [0.5, 1.0, 2.0]


def handle_http_order(order):
    """POST /orders. Roughly 30 orders per second at peak."""
    try:
        ORDER_QUEUE.put_nowait(order)
        return 202
    except queue.Full:
        return 503


def worker_loop():
    while True:
        order = ORDER_QUEUE.get()
        validate(order)                 # ~5 ms of CPU
        for attempt in range(MAX_RETRIES + 1):
            try:
                charge(order)           # network call, see payments.py
                ship(order)
                break
            except PaymentTimeout:
                if attempt == MAX_RETRIES:
                    dead_letter(order)
                else:
                    time.sleep(BACKOFF_SECONDS[attempt])
            except PaymentDeclined:
                notify_customer(order)
                break


def validate(order): ...
def ship(order): ...
def dead_letter(order): ...
def notify_customer(order): ...


for _ in range(WORKERS):
    threading.Thread(target=worker_loop, daemon=True).start()
