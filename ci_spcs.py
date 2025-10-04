#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ci_spcs.py
Snowflake Container Services (SPCS) CI ユーティリティ
- Compute Pool 作成（XS など）
- Job Service 実行（spec をステージから参照）
"""

import argparse, sys, shlex, subprocess

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
                        name="DOC_JOB", async_exec=True, replicas=1, connection=None):
    async_opt = "TRUE" if async_exec else "FALSE"
    sql = f"""
    EXECUTE JOB SERVICE
      IN COMPUTE POOL {pool}
      FROM {stage}
      SPECIFICATION_FILE='{spec_file}'
      NAME={name}
      ASYNC={async_opt}
      REPLICAS={replicas};
    """
    cmd = ["snow", "sql", "-q", sql]
    if connection:
        cmd += ["--connection", connection]
    run(cmd)

def wait_for_service(name: str, connection=None):
    sql = f"SELECT SYSTEM$WAIT_FOR_SERVICES('{name}');"
    cmd = ["snow", "sql", "-q", sql]
    if connection:
        cmd += ["--connection", connection]
    run(cmd)

def get_logs(name: str, container="lo", connection=None):
    sql = f"SELECT SYSTEM$GET_SERVICE_LOGS('{name}', 0, '{container}');"
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
    sj.add_argument("--spec", required=True)        # e.g. specs/lo_convert.yaml
    sj.add_argument("--name", default="DOC_JOB")
    sj.add_argument("--replicas", type=int, default=1)
    sj.add_argument("--sync", action="store_true")  # デフォは async
    sj.add_argument("--connection")

    # logs
    sl = sub.add_parser("logs", help="Get logs")
    sl.add_argument("--name", required=True)
    sl.add_argument("--container", default="lo")
    sl.add_argument("--connection")

    args = ap.parse_args()

    if args.cmd == "pool":
        create_compute_pool(args.name, args.size,
                            args.min_nodes, args.max_nodes, args.connection)

    elif args.cmd == "job":
        execute_job_service(args.pool, args.stage, args.spec,
                            name=args.name, async_exec=(not args.sync),
                            replicas=args.replicas, connection=args.connection)
        if args.sync:
            wait_for_service(args.name, args.connection)

    elif args.cmd == "logs":
        get_logs(args.name, args.container, args.connection)


if __name__ == "__main__":
    main()
