"""启动一个一次性的、需密码认证的回环 PostgreSQL 验收服务器。

这是可选的开发辅助工具，不是产品运行时依赖。需单独下载官方 EDB Windows ZIP，
核对其公布的校验和，再传入解压后的 pgsql 目录。集群数据和凭据都放在
被忽略的 .local-run 目录下。本工具不会安装任何操作系统服务。
"""

from __future__ import annotations

import argparse
import json
import secrets
import socket
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

from psycopg.conninfo import make_conninfo

ROOT = Path(__file__).resolve().parents[1]


def run(command, *, logfile):
    """运行命令，输出追加到日志文件并设 90 秒超时；返回码非零时抛出 RuntimeError。"""
    with logfile.open("ab") as stream:
        # 脱离终端的 Windows 服务器即使在 pg_ctl 退出后，也可能继承捕获管道。
        # 改用真实日志文件，避免无限等待管道 EOF。
        completed = subprocess.run(
            list(map(str, command)),
            stdin=subprocess.DEVNULL,
            stdout=stream,
            stderr=subprocess.STDOUT,
            timeout=90,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
    if completed.returncode:
        raise RuntimeError(f"PostgreSQL fixture command failed; inspect {logfile}")


def start(tools: Path) -> dict:
    """用解压后的 pgsql 目录在 .local-run 下初始化 scram 认证的一次性集群，随机密码，仅监听 127.0.0.1 的空闲端口并启动，返回元数据。"""
    tools = tools.resolve()
    executables = {name: tools / "bin" / f"{name}.exe" for name in ("initdb", "pg_ctl")}
    if not all(path.is_file() for path in executables.values()):
        raise ValueError("Pass an extracted official Windows pgsql directory")
    workspace = ROOT / ".local-run" / "postgres-acceptance"
    workspace.mkdir(parents=True, exist_ok=True)
    fixture = workspace / ("cluster-" + uuid4().hex[:12])
    fixture.mkdir()
    cluster = fixture / "data"
    password_file = fixture / "init-password.txt"
    password = secrets.token_urlsafe(32)
    password_file.write_text(password, encoding="utf8")
    log = fixture / "bootstrap.log"
    run(
        [
            executables["initdb"],
            "-D",
            cluster,
            "-U",
            "nexrag_fixture_admin",
            "--pwfile",
            password_file,
            "-A",
            "scram-sha-256",
            "--encoding=UTF8",
            "--locale=C",
        ],
        logfile=log,
    )
    password_file.unlink()
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    dsn = make_conninfo(
        host="127.0.0.1",
        port=port,
        user="nexrag_fixture_admin",
        password=password,
        dbname="postgres",
        connect_timeout=5,
    )
    admin_file = fixture / "admin-dsn.txt"
    admin_file.write_text(dsn, encoding="utf8")
    metadata = dict(
        tools=str(tools),
        directory=str(fixture),
        cluster=str(cluster),
        admin_dsn_file=str(admin_file),
        host="127.0.0.1",
        port=port,
    )
    (fixture / "fixture.json").write_text(json.dumps(metadata, indent=2), encoding="utf8")
    control(fixture, stop=False)
    return metadata


def control(directory: Path, *, stop: bool) -> dict:
    """只控制本工作区下被明确描述的 fixture。"""
    directory = directory.resolve()
    allowed = (ROOT / ".local-run" / "postgres-acceptance").resolve()
    if not directory.is_relative_to(allowed) or directory == allowed:
        raise ValueError("Fixture control is restricted to .local-run/postgres-acceptance")
    metadata = json.loads((directory / "fixture.json").read_text(encoding="utf8"))
    cluster = Path(metadata["cluster"]).resolve()
    if cluster != directory / "data" or not (cluster / "PG_VERSION").is_file():
        raise ValueError("Fixture metadata does not describe its own cluster")
    pg_ctl = Path(metadata["tools"]) / "bin" / "pg_ctl.exe"
    if not pg_ctl.resolve().is_relative_to(allowed):
        raise ValueError("Fixture tools must remain inside the isolated workspace")
    command = [pg_ctl, "-D", cluster, "-w"]
    if stop:
        command += ["-m", "fast", "stop"]
    else:
        port = int(metadata["port"])
        if not 1 <= port <= 65535:
            raise ValueError("Invalid loopback fixture port")
        command += ["-l", directory / "server.log", "-o", f"-h 127.0.0.1 -p {port}", "start"]
    run(command, logfile=directory / "bootstrap.log")
    return metadata


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--tools", type=Path)
    target.add_argument("--existing", type=Path)
    parser.add_argument("--stop", action="store_true")
    args = parser.parse_args()
    if args.stop and not args.existing:
        parser.error("--stop requires --existing")
    print(
        json.dumps(
            control(args.existing, stop=args.stop) if args.existing else start(args.tools), indent=2
        )
    )
