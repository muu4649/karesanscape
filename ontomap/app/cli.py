"""ontomap-app serve: 画面を開く。Mac アプリからは --ready-file / --parent-pid 付きで起動される。"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import threading
import time
from pathlib import Path

VERSION = "0.1.0"


def _free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex((host, port)) != 0


def serve(host: str = "127.0.0.1", port: int | None = None, open_browser: bool = True, ready_file: str | None = None, parent_pid: int | None = None) -> None:
    """画面を開く。Mac アプリから呼ばれたときは空きポートで開き、URL を ready_file に書く。"""
    import uvicorn

    from .engine import home
    from .server import app

    if host not in ("127.0.0.1", "localhost", "::1"):
        raise SystemExit("手元の PC (127.0.0.1) でだけ動かします")
    home()
    if ready_file:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, 0))
        p = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))

        def announce() -> None:
            while not server.started and not server.should_exit:
                time.sleep(0.05)
            if server.started:
                tmp = Path(ready_file + ".tmp")
                tmp.write_text(json.dumps({"url": f"http://127.0.0.1:{p}/"}), encoding="utf-8")
                tmp.replace(ready_file)

        def watch_parent() -> None:
            while not server.should_exit:
                try:
                    os.kill(parent_pid, 0)
                except OSError:
                    server.should_exit = True
                    return
                time.sleep(1.0)

        threading.Thread(target=announce, daemon=True).start()
        if parent_pid:
            threading.Thread(target=watch_parent, daemon=True).start()
        print(f"OntoMap: http://127.0.0.1:{p}/  (保存先 {home()})", flush=True)
        server.run(sockets=[sock])
        return
    port = port or 8800
    if not _free(host, port):
        port = next((q for q in range(port + 1, port + 30) if _free(host, q)), None) or 0
        if not port:
            raise SystemExit("空いているポートが見つかりません。--port で指定してください")
    url = f"http://127.0.0.1:{port}/"
    if open_browser:
        import webbrowser

        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    print(f"OntoMap: {url}  (保存先 {home()})  止めるには Ctrl+C", flush=True)
    uvicorn.run(app, host=host, port=port, log_level="warning")


def main(argv: list[str] | None = None) -> int:
    """エントリポイント。"""
    ap = argparse.ArgumentParser(prog="ontomap-app", description="特許俯瞰マップ（OntoMap）")
    ap.add_argument("--version", action="version", version=VERSION)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("serve", help="画面を開く")
    sp.add_argument("--port", type=int, default=None)
    sp.add_argument("--no-browser", action="store_true")
    sp.add_argument("--ready-file", help=argparse.SUPPRESS)
    sp.add_argument("--parent-pid", type=int, help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    if a.cmd == "serve":
        serve(port=a.port, open_browser=not a.no_browser, ready_file=a.ready_file, parent_pid=a.parent_pid)
    return 0


if __name__ == "__main__":
    sys.exit(main())
