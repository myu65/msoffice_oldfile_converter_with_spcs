#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ci_push.py
- Snowflake SIR へ Docker イメージを push
-（任意）spec.yaml をステージへ PUT
要件:
  - snow (Snowflake CLI v2)
  - docker
使い方例:
  uv run python ci_push.py \
    --repo SNOWFLAKE_LEARNING_DB.DATA_TEST.DOC_TOOLS \
    --image lo-convert --tag latest \
    --build \
    --put-spec --spec-path specs/lo_convert.yaml \
    --stage @DOC_STAGE --spec-dest specs/lo_convert.yaml \
    --connection XVRALMC-UX32060
"""

import argparse
import os
import shlex
import socket
import subprocess
import sys


# -------------------- utils --------------------
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


def parse_repo(dot_name: str):
    parts = dot_name.split(".")
    if len(parts) != 3:
        print("[ERROR] --repo は DB.SCHEMA.REPO 形式で指定してください。", file=sys.stderr)
        sys.exit(2)
    return parts[0], parts[1], parts[2]


# -------------------- snow ops --------------------
def spcs_registry_login(connection: str | None):
    cmd = ["snow", "spcs", "image-registry", "login"]
    if connection:
        cmd += ["--connection", connection]
    run(cmd)


def get_registry_host(connection: str | None) -> str:
    """
    公式コマンドでレジストリURL(ホスト)を取得する。
    フォーマット: <org>-<account>.registry.snowflakecomputing.com
    """
    cmd = ["snow", "spcs", "image-registry", "url"]
    if connection:
        cmd += ["--connection", connection]
    out = run(cmd, capture=True)
    host = (out or "").strip()
    if not host:
        print("[ERROR] registry URL が取得できません。接続/権限を確認してください。", file=sys.stderr)
        sys.exit(2)
    return host


def create_image_repo(repo_name: str, db: str, schema: str, connection: str | None):
    cmd = ["snow", "spcs", "image-repository", "create", repo_name, "--if-not-exists"]
    if connection:
        cmd += ["--connection", connection]
    if db:
        cmd += ["--database", db]
    if schema:
        cmd += ["--schema", schema]
    run(cmd)


def put_spec(stage: str, local_spec_path: str, dest_path: str, connection: str | None):
    abs_spec = os.path.abspath(local_spec_path)
    if not os.path.exists(abs_spec):
        print(f"[ERROR] spec ファイルが見つかりません: {abs_spec}", file=sys.stderr)
        sys.exit(3)
    target = stage.rstrip("/") + "/" + dest_path.lstrip("/")
    sql = f"PUT file://{abs_spec} {target} AUTO_COMPRESS=FALSE OVERWRITE=TRUE;"
    cmd = ["snow", "sql", "-q", sql]
    if connection:
        cmd += ["--connection", connection]
    run(cmd)


# -------------------- docker ops --------------------
def docker_build(local_ref: str, dockerfile: str, context: str):
    run(["docker", "build", "-t", local_ref, "-f", dockerfile, context])


def docker_tag_push(local_ref: str, host: str, db: str, schema: str, repo: str, image: str, tag: str) -> str:
    # ドメイン名は大文字混ざってもOKだが、環境差吸収のため一律小文字に
    host_l = host.lower()
    # パスは Docker の仕様上 小文字が安全
    path_l = f"{db}/{schema}/{repo}/{image}:{tag}".lower()
    remote_ref = f"{host_l}/{path_l}"

    # DNS 事前チェック（原因切り分け用）
    try:
        socket.gethostbyname(host_l)
    except Exception as e:
        print(f"[ERROR] DNS でレジストリが解決できません: {host_l} ({e})", file=sys.stderr)
        sys.exit(2)

    run(["docker", "tag", local_ref, remote_ref])
    run(["docker", "push", remote_ref])
    return remote_ref


# -------------------- main --------------------
def main():
    ensure("snow")
    ensure("docker")

    ap = argparse.ArgumentParser(description="Build/Push Docker image to Snowflake SIR (+ optional PUT spec)")
    ap.add_argument("--repo", required=True, help="DB.SCHEMA.REPO")
    ap.add_argument("--image", default="lo-convert")
    ap.add_argument("--tag", default="latest")
    ap.add_argument("--build", action="store_true")
    ap.add_argument("--dockerfile", default="Dockerfile")
    ap.add_argument("--context", default=".")
    ap.add_argument("--put-spec", action="store_true")
    ap.add_argument("--spec-path", default="specs/lo_convert.yaml")
    ap.add_argument("--stage", default="@DOC_STAGE")
    ap.add_argument("--spec-dest", default="specs/lo_convert.yaml")
    ap.add_argument("--connection", help="snow CLI connection 名（例: XVRALMC-UX32060）")
    args = ap.parse_args()

    # 1) Docker レジストリログイン（接続プロファイル使用）
    spcs_registry_login(args.connection)

    # 2) 正しいレジストリホスト（URL）を CLI から取得
    host = get_registry_host(args.connection)
    print(f"[INFO] registry host: {host}")

    # 3) イメージリポジトリ作成（無ければ）
    db, schema, repo = parse_repo(args.repo)
    create_image_repo(repo, db, schema, args.connection)

    # 4) ビルド（必要なら）
    local_ref = f"{args.image}:{args.tag}"
    if args.build:
        docker_build(local_ref, args.dockerfile, args.context)

    # 5) tag & push（host/path を小文字に正規化）
    remote_ref = docker_tag_push(local_ref, host, db, schema, repo, args.image, args.tag)
    print(f"[INFO] pushed: {remote_ref}")

    # 6) spec をステージに PUT（任意）
    if args.put_spec:
        put_spec(args.stage, args.spec_path, args.spec_dest, args.connection)
        print(f"[INFO] spec PUT -> {args.stage}/{args.spec_dest}")

    print("✅ 完了")


if __name__ == "__main__":
    main()
