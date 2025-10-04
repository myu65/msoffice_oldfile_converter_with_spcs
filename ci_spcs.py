#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ci_spcs.py
Snowflake Container Services (SPCS) CI ユーティリティ
- Compute Pool 作成（XS など）
- Job Service 実行（spec をステージから参照）
"""

import argparse
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime
from pathlib import Path, PurePosixPath

# ---------- utils ----------
def run(cmd, *, check=True, capture=False, shell=False) -> str:
    if isinstance(cmd, list):
        printable = " ".join(shlex.quote(str(c)) for c in cmd)
    else:
        printable = cmd
    print(f"+ {printable}")
    p = subprocess.run(cmd, check=check, capture_output=capture, text=True, shell=shell)
    if capture:
        return (p.stdout or "").strip()
    return ""

def ensure(cmd_name: str):
    from shutil import which
    if not which(cmd_name):
        print(f"[ERROR] `{cmd_name}` が見つかりません。インストールしてください。", file=sys.stderr)
        sys.exit(1)

# ---------- spec staging ----------
def stage_spec_template(stage: str, local_spec_path: str, dest_hint: str,
                        connection=None, job_name: str | None = None,
                        database: str | None = None, schema: str | None = None) -> str:
    abs_spec = Path(local_spec_path).resolve()
    if not abs_spec.is_file():
        print(f"[ERROR] spec テンプレートが見つかりません: {abs_spec}", file=sys.stderr)
        sys.exit(3)

    dest_norm = (dest_hint or str(abs_spec.name)).replace("\\", "/").strip("/")
    remote_dir = ""
    base_name = abs_spec.stem
    suffix = abs_spec.suffix or ".yaml"

    if dest_norm:
        if dest_norm.endswith("/"):
            remote_dir = dest_norm.rstrip("/")
        else:
            last_segment = dest_norm.split("/")[-1]
            if "." in last_segment:
                pp = PurePosixPath(dest_norm)
                remote_dir = "" if pp.parent == PurePosixPath(".") else pp.parent.as_posix()
                base_name = pp.stem
                suffix = pp.suffix or suffix
            else:
                remote_dir = dest_norm

    safe_job = ""
    if job_name:
        safe_job = re.sub(r"[^A-Za-z0-9_-]+", "-", job_name).strip("-_")

    timestamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
    rand = uuid.uuid4().hex[:8]
    name_parts = [base_name]
    if safe_job:
        name_parts.append(safe_job)
    name_parts.extend([timestamp, rand])
    remote_filename = "_".join(part for part in name_parts if part) + suffix
    remote_rel_path = remote_filename if not remote_dir else f"{remote_dir}/{remote_filename}"

    target = stage.rstrip("/")
    if remote_dir:
        target += "/" + remote_dir

    tmp_dir = tempfile.TemporaryDirectory()
    renamed = Path(tmp_dir.name) / remote_filename
    shutil.copy2(abs_spec, renamed)

    statements: list[str] = []
    if database:
        statements.append(f"USE DATABASE {database};")
    if schema:
        statements.append(f"USE SCHEMA {schema};")
    statements.append(f"PUT file://{renamed} {target} AUTO_COMPRESS=FALSE OVERWRITE=TRUE;")
    sql = " ".join(statements)
    cmd = ["snow", "sql", "-q", sql]
    if connection:
        cmd += ["--connection", connection]
    try:
        run(cmd)
    finally:
        tmp_dir.cleanup()

    return remote_rel_path


# ---------- SPCS ops ----------
def create_compute_pool(name: str, size: str, min_nodes=1, max_nodes=1, connection=None):
    """
    size: XS / S / M / L … → CPU_X64_XS 形式に変換
    """
    family = f"CPU_X64_{size.upper()}"
    sql = f"""
    CREATE COMPUTE POOL IF NOT EXISTS {name}
      MIN_NODES = {min_nodes}
      MAX_NODES = {max_nodes}
      INSTANCE_FAMILY = {family}
      AUTO_RESUME = TRUE
      AUTO_SUSPEND_SECS = 300;
    """
    cmd = ["snow", "sql", "-q", sql]
    if connection:
        cmd += ["--connection", connection]
    run(cmd)

def execute_job_service(pool: str, stage: str, spec_file: str,
                        name="DOC_JOB", async_exec=True, replicas=1,
                        connection=None, database: str | None = None, schema: str | None = None):
    async_opt = "TRUE" if async_exec else "FALSE"
    statements: list[str] = []
    if database:
        statements.append(f"USE DATABASE {database};")
    if schema:
        statements.append(f"USE SCHEMA {schema};")
    statements.append(
        f"""
        EXECUTE JOB SERVICE
          IN COMPUTE POOL {pool}
          FROM {stage}
          SPECIFICATION_FILE='{spec_file}'
          NAME={name}
          ASYNC={async_opt}
          REPLICAS={replicas};
        """.strip()
    )
    sql = " ".join(statements)
    cmd = ["snow", "sql", "-q", sql]
    if connection:
        cmd += ["--connection", connection]
    run(cmd)

def wait_for_service(name: str, connection=None, database: str | None = None, schema: str | None = None):
    statements: list[str] = []
    if database:
        statements.append(f"USE DATABASE {database};")
    if schema:
        statements.append(f"USE SCHEMA {schema};")
    statements.append(f"SELECT SYSTEM$WAIT_FOR_SERVICES('{name}');")
    sql = " ".join(statements)
    cmd = ["snow", "sql", "-q", sql]
    if connection:
        cmd += ["--connection", connection]
    run(cmd)

def get_logs(name: str, container="lo", connection=None,
             database: str | None = None, schema: str | None = None):
    statements: list[str] = []
    if database:
        statements.append(f"USE DATABASE {database};")
    if schema:
        statements.append(f"USE SCHEMA {schema};")
    statements.append(f"SELECT SYSTEM$GET_SERVICE_LOGS('{name}', 0, '{container}');")
    sql = " ".join(statements)
    cmd = ["snow", "sql", "-q", sql]
    if connection:
        cmd += ["--connection", connection]
    run(cmd)

# ---------- main ----------
def main():
    ensure("snow")

    ap = argparse.ArgumentParser(description="SPCS CI Helper")
    sub = ap.add_subparsers(dest="cmd", required=True)

    # pool
    sp = sub.add_parser("pool", help="Compute Pool create")
    sp.add_argument("--name", required=True)
    sp.add_argument("--size", default="XS")
    sp.add_argument("--min-nodes", type=int, default=1)
    sp.add_argument("--max-nodes", type=int, default=1)
    sp.add_argument("--connection")

    # job
    sj = sub.add_parser("job", help="Execute Job Service")
    sj.add_argument("--pool", required=True)
    sj.add_argument("--stage", required=True)       # e.g. @DOC_STAGE
    sj.add_argument("--spec", required=True,
                    help="spec テンプレート (ローカルパスまたは既存ステージパス)")
    sj.add_argument("--name", default=None,
                    help="Job Service 名。省略すると自動でタイムスタンプ付き名を生成")
    sj.add_argument("--replicas", type=int, default=1)
    sj.add_argument("--sync", action="store_true")  # デフォは async
    sj.add_argument("--connection")
    sj.add_argument("--database", help="Job 実行時に USE DATABASE する対象")
    sj.add_argument("--schema", help="Job 実行時に USE SCHEMA する対象")

    # logs
    sl = sub.add_parser("logs", help="Get logs")
    sl.add_argument("--name", required=True)
    sl.add_argument("--container", default="lo")
    sl.add_argument("--connection")
    sl.add_argument("--database", help="ログ取得前に USE DATABASE する対象")
    sl.add_argument("--schema", help="ログ取得前に USE SCHEMA する対象")

    args = ap.parse_args()

    if args.cmd == "pool":
        create_compute_pool(args.name, args.size,
                            args.min_nodes, args.max_nodes, args.connection)

    elif args.cmd == "job":
        job_name = args.name or f"DOC_JOB_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"

        spec_path_for_exec = args.spec
        if os.path.exists(args.spec):
            spec_path_for_exec = stage_spec_template(
                args.stage, args.spec, args.spec,
                connection=args.connection, job_name=job_name,
                database=args.database, schema=args.schema
            )
            print(f"[INFO] spec PUT -> {args.stage}/{spec_path_for_exec}")

        execute_job_service(args.pool, args.stage, spec_path_for_exec,
                            name=job_name, async_exec=(not args.sync),
                            replicas=args.replicas, connection=args.connection,
                            database=args.database, schema=args.schema)
        print(f"[INFO] job name: {job_name}")
        if args.sync:
            wait_for_service(job_name, args.connection,
                             database=args.database, schema=args.schema)

    elif args.cmd == "logs":
        get_logs(args.name, args.container, args.connection,
                 database=args.database, schema=args.schema)


if __name__ == "__main__":
    main()
