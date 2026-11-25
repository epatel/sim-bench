"""Start the workbench: ``.venv/bin/python -m server [--port 8765] [--reload]``."""

import argparse

import uvicorn


def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m server")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--reload", action="store_true", help="restart on code changes")
    args = ap.parse_args()
    uvicorn.run("server.app:app", host=args.host, port=args.port, reload=args.reload)


if __name__ == "__main__":
    main()
