"""Dedicated resumable Agent runner: python -m app.precheck_worker [--once]."""
import argparse
import logging
import signal
import threading

from app.precheck_agent import claim, execute_step


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    stopping = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stopping.set())
    while not stopping.is_set():
        try:
            task = claim()
            if task:
                execute_step(task)
        except Exception:
            logging.exception("Precheck tick failed; lease recovery will retry")
            task = None
        if args.once:
            break
        stopping.wait(1 if task else 3)


if __name__ == "__main__":
    main()
