from __future__ import annotations

import argparse

from .api import build_server


def main() -> None:
    parser = argparse.ArgumentParser(description="Run offline tide-aware scheduling service")
    parser.add_argument("--database", default="data/tide-scheduler.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    server = build_server(args.database, args.host, args.port)
    print(f"Tide scheduler listening on http://{args.host}:{args.port} (database={args.database})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
