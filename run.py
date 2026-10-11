import argparse
from pathlib import Path

import uvicorn

from orchestrator.server import create_app


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Local Orchestrator — bez Dockera")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--public-url", help="Np. https://orchestrator.example.com")
    parser.add_argument("--data-dir", type=Path)
    args = parser.parse_args()
    print(f"\nLocal Orchestrator: http://127.0.0.1:{args.port}\nZatrzymanie: Ctrl+C\n", flush=True)
    uvicorn.run(create_app(args.data_dir, public_url=args.public_url), host=args.host, port=args.port, log_level="info")
