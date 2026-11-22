"""CLI 入口（M0 仅 serve 与 doctor，M1 接入下载命令）。"""

from __future__ import annotations

import argparse


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="subangler", description="字幕自动化下载整理工具")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="启动 Web 服务")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=8765)

    sub.add_parser("doctor", help="环境自检")

    args = parser.parse_args(argv)

    if args.command == "serve":
        import uvicorn

        uvicorn.run("subangler.main:app", host=args.host, port=args.port, log_level="info")
        return 0

    if args.command == "doctor":
        from .doctor import main as doctor_main

        return doctor_main([])

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
