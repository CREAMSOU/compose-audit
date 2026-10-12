#!/usr/bin/env python3
"""
compose-audit 自检脚本。只用标准库，不依赖 docker。

    python selftest.py

覆盖三类回归：
  1. 密钥判定 —— 名字判据 + 值内嵌凭据 + 开关类不该误判
  2. 打码安全 —— JSON 输出里绝不出现明文口令
  3. compose 解析 —— YAML 锚点合并、env_file 长语法、COMPOSE_PROFILES 插值

测试用的 compose 是临时生成的，不触碰任何真实项目。
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))


def load_module():
    spec = importlib.util.spec_from_file_location(
        "compose_audit", os.path.join(HERE, "compose_audit.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


PASS = FAIL = 0
FAILED: list[str] = []


def check(name: str, got, want) -> None:
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        FAILED.append(name)
        print(f"  [FAIL] {name}\n         got  = {got!r}\n         want = {want!r}")


# ---------------------------------------------------------------------------
# 1. 密钥判定
# ---------------------------------------------------------------------------

def test_secret_detection(ca) -> None:
    print("\n[1] 密钥判定")
    cases = [
        ("REDISCLI_AUTH", "example-pw-123", True, "裸口令名"),
        ("CELERY_BROKER_URL", "redis://:pw123@redis:6379/1", True, "URL 内嵌口令"),
        ("DATABASE_URL", "postgresql://u:p@db:5432/x", True, "URL 内嵌口令"),
        ("POSTGRES_PASSWORD", "example-pw-123", True, "PASSWORD"),
        ("SECRET_KEY", "sk-abc", True, "SECRET"),
        ("AWS_ACCESS_KEY", "AKIAxxx", True, "ACCESS_KEY"),
        ("WEAVIATE_ENABLE_TOKENIZER_GSE", "true", False, "开关，不该打码"),
        ("INDEXING_MAX_SEGMENTATION_TOKENS_LENGTH", "4000", False, "数值，不该打码"),
        ("WEAVIATE_TOKENIZATION", "gse", False, "配置项，不该打码"),
        ("WEAVIATE_AUTHORIZATION_ADMINLIST_USERS", "a@b.c", False, "用户名单，不该打码"),
        ("SSRF_PROXY_ALLOW_PRIVATE_DOMAINS", "true", False, "开关，不该打码"),
        ("AUTHENTICATION_APIKEY_ALLOWED_KEYS", "sk-a sk-b", True, "密钥名单"),
        ("SOME_ENABLE_FLAG", "redis://:pw@h/1", True, "开关名但值内嵌口令→兜底"),
        ("PORT", "8080", False, "普通端口"),
        ("LOG_LEVEL", "INFO", False, "普通配置"),
        ("WEAVIATE_AUTHENTICATION_ANONYMOUS_ACCESS_ENABLED", "true", False, "AUTHENTICATION 开关"),
    ]
    for name, val, want, desc in cases:
        check(f"{name} ({desc})", ca.is_secret_pair(name, val), want)


def test_scrub(ca) -> None:
    print("\n[2] URL 凭据擦除")
    check("redis URL 口令被擦",
          "pw123" in ca.scrub_value("redis://:pw123@redis:6379/1"), False)
    check("redis URL 主机保留",
          "redis:6379" in ca.scrub_value("redis://:pw123@redis:6379/1"), True)
    check("无凭据的值不动",
          ca.scrub_value("postgres:15-alpine"), "postgres:15-alpine")


# ---------------------------------------------------------------------------
# 3. compose 解析
# ---------------------------------------------------------------------------

COMPOSE_FIXTURE = """\
x-shared: &shared
  env_file:
    - path: ./envs/base.env
      required: false
    - path: ./envs/common.env
      required: false

services:
  api:
    <<: *shared
    image: example/api:1.0
    environment:
      - DB_PASSWORD=secret123
      - LOG_LEVEL=info
    volumes:
      - ./volumes/app:/app/storage

  weaviate:
    image: example/weaviate:1.0
    profiles:
      - weaviate
    volumes:
      - ./volumes/weaviate:/var/lib/weaviate

  disabled:
    image: example/unused:1.0
    profiles:
      - never_enabled
    volumes:
      - ./volumes/nope:/nope
"""

ENV_FIXTURE = """\
VECTOR_STORE=weaviate
COMPOSE_PROFILES=${VECTOR_STORE:-weaviate},other
MY_PASSWORD=topsecret
REDISCLI_AUTH=redispw
"""


def test_parser(ca) -> None:
    print("\n[3] compose 解析")
    tmp = tempfile.mkdtemp(prefix="compose_audit_test_")
    try:
        os.makedirs(os.path.join(tmp, "envs"))
        with open(os.path.join(tmp, "docker-compose.yaml"), "w", encoding="utf-8") as f:
            f.write(COMPOSE_FIXTURE)
        with open(os.path.join(tmp, ".env"), "w", encoding="utf-8") as f:
            f.write(ENV_FIXTURE)

        cfg = ca.scan_compose_file(os.path.join(tmp, "docker-compose.yaml"))
        svcs = cfg["services"]

        # 锚点合并：api 的 env_file 应来自 *shared 的两条
        api_ef = svcs["api"].get("env_file") or []
        check("锚点合并 env_file 条数", len(api_ef), 2)
        check("锚点 env_file 路径干净",
              all("path:" not in e for e in api_ef), True)

        # 多行 environment 解析
        check("environment 解析 DB_PASSWORD",
              (svcs["api"].get("env_vars") or {}).get("DB_PASSWORD"), "secret123")

        # COMPOSE_PROFILES 变量插值 → weaviate 应被判定为启用
        check("profile 插值后 weaviate active", svcs["weaviate"].get("active"), True)
        check("未启用 profile 的服务被排除", svcs["disabled"].get("active"), False)

        # 挂载解析
        check("api 挂载解析",
              any("volumes/app" in v for v in (svcs["api"].get("volumes") or [])), True)

        # 报告里必须没有明文口令
        rpt = ca.audit(tmp, None, False)
        md = ca.render(rpt)
        js = ca.render_json(rpt)
        check("Markdown 无明文 topsecret", "topsecret" in md, False)
        check("Markdown 无明文 redispw", "redispw" in md, False)
        check("JSON 无明文 topsecret", "topsecret" in js, False)
        check("JSON 无明文 redispw", "redispw" in js, False)
        check("JSON 有 REDACTED 痕迹", "REDACTED" in js, True)
        check("JSON 本身可解析", isinstance(json.loads(js), dict), True)

        # 密钥名清单应含名字判据与值判据两类
        names = {k for k, _ in rpt["secrets"]}
        check("密钥表含 MY_PASSWORD", "MY_PASSWORD" in names, True)
        check("密钥表含 REDISCLI_AUTH", "REDISCLI_AUTH" in names, True)
        check("密钥表不含 LOG_LEVEL", "LOG_LEVEL" in names, False)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_no_docker_equivalence(ca) -> None:
    """两条路径在同一个真实 compose 上结论必须一致（体积/挂载数）。"""
    print("\n[4] 双路径一致性（需要真实 compose 目录，可选）")
    target = os.environ.get("COMPOSE_AUDIT_TARGET")
    if not target or not os.path.isdir(target):
        print("  [SKIP] 未设置 COMPOSE_AUDIT_TARGET，跳过")
        return
    yml = ca.find_compose_file(target)
    if not yml:
        print("  [SKIP] 目标目录里没有 compose 文件")
        return
    a = ca.audit(target, None, False)
    try:
        b = ca.audit(target, None, True)
    except Exception as e:                                   # noqa: BLE001
        print(f"  [SKIP] docker 路径不可用：{e}")
        return
    check("挂载数一致", len(a["mounts"]), len(b["mounts"]))
    check("服务数一致", len(a["services"]), len(b["services"]))
    check("不可重建体积一致",
          sum(m["size"] for m in a["mounts"] if m["kind"] == "keep"),
          sum(m["size"] for m in b["mounts"] if m["kind"] == "keep"))


def main() -> int:
    ca = load_module()
    print(f"compose-audit v{ca.VERSION} 自检")
    test_secret_detection(ca)
    test_scrub(ca)
    test_parser(ca)
    test_no_docker_equivalence(ca)
    print(f"\n{'=' * 46}")
    print(f"结果：{PASS} 通过，{FAIL} 失败")
    if FAILED:
        print("失败项：")
        for n in FAILED:
            print(f"  - {n}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
