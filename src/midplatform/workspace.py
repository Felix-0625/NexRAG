"""本地工作区设置，以及由管理员管控的业务连接。"""

from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import psycopg

from src.config import settings
from src.midplatform.ops_store import dumps, get_ops_store, new_id, now
from src.midplatform.readonly import ensure_postgres_reader

IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def get_setting(key: str, default=None):
    """读取工作区设置（JSON 反序列化）；键不存在时返回 default。"""
    row = get_ops_store().query_one("SELECT value FROM workspace_settings WHERE key=?", (key,))
    return json.loads(row["value"]) if row else default


def set_setting(key: str, value: Any) -> None:
    """以 JSON 形式写入（或覆盖）一项工作区设置。"""
    get_ops_store().execute(
        "INSERT OR REPLACE INTO workspace_settings VALUES (?,?)", (key, dumps(value))
    )


def export_root() -> Path:
    """返回导出文件根目录：优先用管理员绑定的目录，否则用配置的 EXPORTS_DIR，并解析为绝对路径。"""
    return Path(get_setting("exports_dir", settings.EXPORTS_DIR)).expanduser().resolve()


def bind_export_root(folder: str) -> str:
    """绑定导出根目录并返回其绝对路径。

    只接受绝对路径；目录不存在时创建，并先试写一个临时文件确认可写，否则抛出 ValueError 或 OSError。
    """
    if not folder.strip():
        raise ValueError("请选择产物文件夹")
    target = Path(folder).expanduser()
    if not target.is_absolute():
        raise ValueError("产物目录需要使用本机绝对路径")
    target = target.resolve()
    target.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryFile(dir=target):
        pass
    set_setting("exports_dir", str(target))
    return str(target)


def pick_folder() -> str | None:
    """在本机（仅 Windows）弹出系统文件夹选择框，返回所选路径；用户取消时返回 None，其他平台或失败抛出 ValueError。"""
    if sys.platform != "win32":
        raise ValueError("当前平台请直接填写本机文件夹绝对路径")
    script = """
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
Add-Type -AssemblyName System.Windows.Forms
$dialog = New-Object System.Windows.Forms.FolderBrowserDialog
$dialog.Description = '选择 NexRAG 全局产物文件夹'
$dialog.ShowNewFolderButton = $true
$owner = New-Object System.Windows.Forms.Form
$owner.TopMost = $true
$owner.ShowInTaskbar = $false
$owner.Opacity = 0
$owner.Show()
if ($dialog.ShowDialog($owner) -eq [System.Windows.Forms.DialogResult]::OK) {
    Write-Output $dialog.SelectedPath
}
$dialog.Dispose()
$owner.Dispose()
"""
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-STA", "-Command", script],
        capture_output=True,
        encoding="utf-8",
        timeout=180,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if result.returncode:
        raise ValueError("无法打开本机文件夹选择器，请直接填写路径")
    return result.stdout.strip() or None


def discover(kind: str, address: str) -> list[dict]:
    """用只读连接检查结构；绝不创建缺失的 SQLite 数据库。"""
    if kind == "sqlite":
        path = Path(address).expanduser().resolve()
        if not path.is_file():
            raise ValueError("SQLite 文件不存在")
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5) as conn:
            tables = []
            for (name,) in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ):
                if not IDENTIFIER.fullmatch(name):
                    continue
                info = list(conn.execute(f'PRAGMA table_info("{name}")'))
                columns = [
                    {"name": row[1], "type": row[2] or "TEXT"}
                    for row in info
                    if IDENTIFIER.fullmatch(row[1])
                ]
                tables.append(
                    {
                        "name": name,
                        "columns": columns,
                        "primary_key": [
                            row[1] for row in sorted(info, key=lambda r: r[5]) if row[5]
                        ],
                    }
                )
            return tables
    if kind != "postgresql":
        raise ValueError("请选择 SQLite 或 PostgreSQL")
    with psycopg.connect(
        address, connect_timeout=5, options="-c default_transaction_read_only=on"
    ) as conn:
        ensure_postgres_reader(conn)
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT t.relname,a.attname
                FROM pg_catalog.pg_index i
                JOIN pg_catalog.pg_class t ON t.oid=i.indrelid
                JOIN pg_catalog.pg_namespace n ON n.oid=t.relnamespace
                JOIN LATERAL unnest(i.indkey) WITH ORDINALITY k(attnum,position) ON true
                JOIN pg_catalog.pg_attribute a ON a.attrelid=t.oid AND a.attnum=k.attnum
                WHERE n.nspname='public' AND i.indisprimary
                ORDER BY t.relname,k.position
            """)
            keys: dict[str, list] = {}
            for table, column in cursor:
                keys.setdefault(table, []).append(column)
            cursor.execute(
                "SELECT table_name,column_name,data_type FROM information_schema.columns WHERE table_schema='public' ORDER BY table_name,ordinal_position"
            )
            tables: dict[str, list] = {}
            for name, column, column_type in cursor:
                if IDENTIFIER.fullmatch(name) and IDENTIFIER.fullmatch(column):
                    tables.setdefault(name, []).append({"name": column, "type": column_type})
            return [
                {"name": name, "columns": columns, "primary_key": keys.get(name, [])}
                for name, columns in tables.items()
            ]


def active_connection() -> dict | None:
    """返回当前生效的业务数据连接记录（含完整配置，其中可能含 DSN）；使用默认数据时返回 None。仅限服务端内部使用。"""
    identifier = get_setting("active_connection")
    row = (
        get_ops_store().query_one("SELECT * FROM business_connections WHERE id=?", (identifier,))
        if identifier
        else None
    )
    return {**row, **json.loads(row["config"])} if row else None


def public_connections() -> list[dict]:
    """返回连接的公开清单（默认数据在最前），只含 id、名称、类型和是否生效，不含地址或凭据。"""
    active = get_setting("active_connection")
    rows = get_ops_store().query(
        "SELECT id,name,kind,created_at FROM business_connections ORDER BY created_at DESC"
    )
    return [
        {"id": "default", "name": "默认业务数据", "kind": "configured", "active": not active}
    ] + [{**row, "active": row["id"] == active} for row in rows]


def save_connection(name: str, kind: str, address: str, selected: list[dict]) -> dict:
    """登记业务数据连接：对所选 1 至 30 张表按源库实际结构生成带租户字段的视图和元数据，主键取自源库而非客户端。

    表或租户字段与源库结构不符时抛出 ValueError；新连接不会自动生效，返回其公开信息。
    """
    structure = {table["name"]: table for table in discover(kind, address)}
    if not selected or len(selected) > 30:
        raise ValueError("请选择 1–30 张业务表，并为每张表指定租户字段")
    views, metadata = {}, {}
    for table in selected:
        name_in_db, tenant = table["name"], table["tenant_column"]
        discovered = structure.get(name_in_db, {})
        columns = discovered.get("columns")
        if not columns or tenant not in {item["name"] for item in columns}:
            raise ValueError("业务表或租户字段与数据库结构不符")
        public_columns = {}
        for column in columns:
            col_type = column["type"].lower()
            public_columns[column["name"]] = (
                "INTEGER"
                if "int" in col_type
                else "REAL"
                if any(t in col_type for t in ("real", "float", "double"))
                else "NUMERIC"
                if any(t in col_type for t in ("numeric", "decimal"))
                else "TEXT"
            )
        fields = ", ".join(f'"{column}"' for column in public_columns)
        source = f'"public"."{name_in_db}"' if kind == "postgresql" else f'"{name_in_db}"'
        views[name_in_db] = {
            "columns": public_columns,
            "source_sql": f'SELECT "{tenant}" AS __tenant_id, {fields} FROM {source}',
            # 绝不采信客户端声称的键。去发现源库真实的约束，
            # 并在租户快照中再次强制执行。
            "primary_key": discovered.get("primary_key", []),
            "grain": name_in_db,
        }
        metadata[name_in_db] = {
            "description": name_in_db,
            "columns": {
                column: {"type": column_type, "description": column}
                for column, column_type in public_columns.items()
            },
        }
    identifier = new_id()
    folder = Path(settings.OPS_DB_PATH).resolve().parent / "business-profiles" / identifier
    folder.mkdir(parents=True, exist_ok=True)
    profile = {"type": kind, "views": views}
    if kind == "sqlite":
        profile["database_path"] = str(Path(address).expanduser().resolve())
    source_path, metadata_path = folder / "source.json", folder / "metadata.json"
    source_path.write_text(dumps(profile), encoding="utf-8")
    metadata_path.write_text(dumps({"tables": metadata, "metrics": {}}), encoding="utf-8")
    config = {
        "source_path": str(source_path),
        "metadata_path": str(metadata_path),
        "dsn": address if kind == "postgresql" else None,
    }
    get_ops_store().execute(
        "INSERT INTO business_connections VALUES (?,?,?,?,?)",
        (identifier, name, kind, dumps(config), now()),
    )
    return {"id": identifier, "name": name, "kind": kind, "active": False}
