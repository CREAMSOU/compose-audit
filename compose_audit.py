#!/usr/bin/env python3
"""
compose-audit — 审计一个 docker compose 项目，分清哪些东西删了就回不来。

用法:
    python compose_audit.py <compose目录或yml> [--json] [-o report.md] [--no-docker]

只依赖 Python 标准库。优先用 `docker compose config --format json` 拿到规范化配置，
拿不到就退回内置的轻量 compose 扫描器（两者都不需要 docker daemon 在跑）。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime

VERSION = "0.1.0"

COMPOSE_NAMES = [
    "docker-compose.yml", "docker-compose.yaml",
    "compose.yml", "compose.yaml",
]

# 挂载目标里出现这些词，基本可以断定这份目录装的是"删了就回不来"的数据
DATA_HINTS = [
    "postgres", "mysql", "mariadb", "mongo", "redis", "db", "data",
    "storage", "uploads", "upload", "files", "media", "assets",
    "weaviate", "qdrant", "milvus", "chroma", "minio", "elasticsearch",
    "opensearch", "pgdata", "backup", "logs", "state", "plugins",
]

# 这些词出现在挂载目标里，通常是能重建的（依赖、缓存、配置模板）
REBUILDABLE_HINTS = [
    "dependencies", "cache", "node_modules", "tmp", "temp", "conf", "config",
    "cert", "static", "dist",
]

SECRET_HINTS = re.compile(
    r"(SECRET|PASSWORD|PASSWD|TOKEN|APIKEY|API_KEY|ACCESS_KEY|PRIVATE|CREDENTIAL|_KEY$|^KEY)",
    re.I,
)

# 变量名不含密钥词、但值里内嵌了凭据的情况：
#   redis://:口令@主机:6379/1  ·  postgresql://用户:口令@db/app
#   明文口令本身（SECRET_KEY=xxx 之外，形如 xxxPASSWORD=、AUTH= 也算）
_CRED_IN_URL = re.compile(r"://[^/\s:@]*:([^/\s:@]+)@")
_SECRETISH_VALUE = re.compile(
    r"(?i)\b(password|passwd|pwd|secret|token|apikey|api_key|auth)\s*[=:]\s*\S+"
)
# 变量名里只要出现了这些词，值本身就是凭据（REDISCLI_AUTH=example-pw-123、PGPASSWORD=xxx）
# AUTH 用负向断言排除 AUTHENTICATION / AUTHORIZATION / AUTHORS —— 它们是开关和用户名单，
# 打码会把 "true"/"admin@example.com" 这种无害值也涂掉，读者反而看不出配置意图。
BARE_CRED_NAME = re.compile(
    r"(PASSWORD|PASSWD|PASSPHRASE|SECRET|TOKEN|APIKEY|API_KEY|ACCESS_KEY|"
    r"PRIVATE_KEY|CREDENTIAL|AUTH(?!ENTICATION|ORIZATION|ORS)|PWD)",
    re.I,
)


def scrub_value(value: str) -> str:
    """把值里内嵌的凭据挖掉，保留结构（主机/库名还看得见，口令看不见）。"""
    s = str(value)
    # redis://:pwd@host -> redis://:***@host
    s = _CRED_IN_URL.sub(lambda m: m.group(0).replace(m.group(1), "***REDACTED***"), s)
    return s


def value_looks_secret(value: str) -> bool:
    """值里是否内嵌了凭据（就算变量名叫 CELERY_BROKER_URL 也要打码）。"""
    v = str(value or "")
    if not v:
        return False
    if _CRED_IN_URL.search(v):
        return True
    return bool(_SECRETISH_VALUE.search(v))


# 名字里带 TOKEN/SECRET 但其实是开关或数值，不是凭据：
#   ENABLE_TOKENIZER_GSE / X_TOKEN_LIMIT / TOKENIZATION / ALLOW_PRIVATE_IPS
# 不排掉它们，报告会出现 "TOKENIZATION: ***REDACTED***" 这种把配置涂掉的噪音。
_NOT_SECRET_NAME = re.compile(
    r"(ENABLE|DISABLE|TOKENIZ|TOKEN_LIMIT|MAX_TOKENS?|_TOKENS?_|"
    r"ALLOW_PRIVATE|ALLOW_ANONYMOUS|AUTH_VERSION|_ENABLED$|_ENABLED_)",
    re.I,
)


def is_secret_pair(name: str, value: str = "") -> bool:
    """变量名或变量值任一指向凭据，就算密钥项。"""
    if _NOT_SECRET_NAME.search(name):
        # 开关类名字直接放行，但值内嵌 URL 凭据的仍然要打码
        return value_looks_secret(value)
    if SECRET_HINTS.search(name) or BARE_CRED_NAME.search(name):
        return True
    return value_looks_secret(value)

# compose 里的变量插值：${VAR} / ${VAR:-默认值} / $VAR
_VAR_RE = re.compile(
    r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}|\$([A-Za-z_][A-Za-z0-9_]*)"
)


def load_env(compose_dir: str) -> dict:
    """os.environ + compose 目录下的 .env（.env 不覆盖已有环境变量）。"""
    env = dict(os.environ)
    p = os.path.join(compose_dir, ".env")
    if os.path.isfile(p):
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                for ln in f:
                    ln = ln.strip()
                    if not ln or ln.startswith("#") or "=" not in ln:
                        continue
                    k, _, v = ln.partition("=")
                    env.setdefault(k.strip(), v.strip().strip("\"'"))
        except OSError:
            pass
    return env


def subst(s: str, env: dict) -> str:
    """展开 compose 里的 ${VAR:-默认值}。取不到值又没有默认值就留空。"""
    if not s or "$" not in s:
        return s

    def rep(m):
        name = m.group(1) or m.group(3)
        default = m.group(2)
        v = env.get(name)
        if v is None or v == "":
            return default if default is not None else ""
        return v

    return _VAR_RE.sub(rep, s)


def active_profiles(env: dict) -> set:
    """COMPOSE_PROFILES 里启用的 profile 名。

    这里必须做变量插值：Dify 写的是
        COMPOSE_PROFILES=${VECTOR_STORE:-weaviate},${DB_TYPE:-postgresql},collaboration
    不展开的话拿到的是字面量 "${VECTOR_STORE:-weaviate}"，
    结果 weaviate / postgres 这些服务会被误判成"未启用"而不计入审计。
    """
    raw = subst(env.get("COMPOSE_PROFILES") or "", env)
    return {x.strip() for x in raw.split(",") if x.strip()}


# --------------------------------------------------------------------------
# 轻量 compose 扫描器（docker CLI 不可用时的兜底）
# --------------------------------------------------------------------------

def _strip_comment(line: str) -> str:
    """去掉行尾注释，但不动引号里的 #。"""
    out, quote = [], None
    for ch in line:
        if quote:
            if ch == quote:
                quote = None
            out.append(ch)
        elif ch in "\"'":
            quote = ch
            out.append(ch)
        elif ch == "#":
            break
        else:
            out.append(ch)
    return "".join(out).rstrip()


def _unquote(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        return s[1:-1]
    return s


def scan_compose_file(path: str) -> dict:
    """扫描 compose 文件，抽出每个服务的 image / volumes / env_file / build。

    不是通用 YAML 解析器，只认 compose 文件实际会用的那几种写法。
    额外支持 YAML 锚点（`x-xxx: &name` 定义 + `<<: *name` 合并），
    否则 Dify 这类把 env_file 全塞进锚点的 compose 会扫出空结果。
    """
    services: dict[str, dict] = {}
    top_volumes: list[str] = []
    anchors: dict[str, dict] = {}     # 锚点名 -> 该锚点收集到的字段
    section = None          # 'services' | 'volumes' | ...
    cur_service = None
    cur_key = None          # 当前正在收集的多行列表（volumes / env_file ...）
    long_item: dict | None = None   # 长语法 volume 项
    cur_anchor = None       # 正在解析 x- 顶层锚点

    with open(path, "r", encoding="utf-8", errors="replace") as f:
        raw_lines = f.readlines()

    for raw in raw_lines:
        line = _strip_comment(raw.rstrip("\n"))
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        body = line.strip()

        # 顶层键
        if indent == 0 and body.endswith(":"):
            section = body[:-1].strip()
            cur_service, cur_key = None, None
            continue

        # 顶层锚点定义：`x-shared-api-worker-config: &shared-api-worker-config`
        if indent == 0 and ":" in body:
            head, _, tail = body.partition(":")
            if head.startswith("x-"):
                m = re.search(r"&([\w.-]+)", tail)
                section = None
                cur_service, cur_key = None, None
                if m:
                    cur_anchor = m.group(1)
                    anchors.setdefault(cur_anchor, {"env_file": [], "volumes": [],
                                                    "env_vars": {}, "profiles": []})
                    # 锚点体长这样：`env_file:` 与冒号同行时也要认
                    rest = tail.split("&" + cur_anchor, 1)[-1].strip()
                    if rest.startswith("env_file") or rest.startswith("volumes"):
                        cur_key = rest.split(":", 1)[0].strip()
                        section = f"__anchor__{cur_anchor}"
                    else:
                        section = f"__anchor__{cur_anchor}"
                continue

        # 锚点体（顶层 x- 之后的缩进行，直到下一个顶层键）
        if section and section.startswith("__anchor__"):
            aname = section[len("__anchor__"):]
            tgt = anchors.setdefault(aname, {"env_file": [], "volumes": [],
                                             "env_vars": {}, "profiles": []})
            if body.startswith("- "):
                item = _unquote(body[2:])
                if cur_key == "env_file":
                    if item.startswith("path:"):
                        item = _unquote(item[5:].strip())
                    tgt["env_file"].append(item)
                elif cur_key == "environment" and "=" in item:
                    k, _, v = item.partition("=")
                    tgt["env_vars"][k.strip()] = v.strip()
                continue
            if ":" in body:
                key, _, val = body.partition(":")
                key, val = key.strip(), _unquote(val.strip())
                if key in ("required", "path"):
                    continue
                if key in ("volumes", "env_file", "environment", "profiles") and not val:
                    cur_key = key
                    continue
                cur_key = None
                if key == "env_file" and val:
                    tgt["env_file"].append(val)
                elif key == "image" and val:
                    tgt.setdefault("image", val)
            continue

        if section == "services":
            # 服务名（2 空格缩进 + 冒号结尾）
            if indent == 2 and body.endswith(":"):
                cur_service = body[:-1].strip()
                services[cur_service] = {"image": None, "build": None,
                                         "volumes": [], "env_file": [],
                                         "env_vars": {}, "profiles": []}
                cur_key, long_item = None, None
                continue

            if cur_service is None:
                continue

            # 服务内的列表项
            if body.startswith("- "):
                item = _unquote(body[2:])
                if cur_key == "volumes":
                    # 长语法：- type: bind 之后跟随 source:/target:
                    if ":" in item and not item.startswith(".") and not item.startswith("/") \
                            and not item.startswith("~") and "=" not in item:
                        key, _, val = item.partition(":")
                        if key.strip() in ("type", "source", "target", "read_only"):
                            long_item = {key.strip(): _unquote(val)}
                            services[cur_service]["volumes"].append(long_item)
                            continue
                    if long_item is not None:
                        long_item = None
                    services[cur_service]["volumes"].append(item)
                elif cur_key == "environment":
                    if "=" in item:
                        k, _, v = item.partition("=")
                        services[cur_service]["env_vars"][k.strip()] = v.strip()
                    else:
                        services[cur_service]["env_vars"][item] = ""
                elif cur_key == "env_file":
                    # 长语法 `- path: ./x.env` / `- path: ./x.env` + 缩进续行
                    if item.startswith("path:"):
                        item = _unquote(item[5:].strip())
                    services[cur_service]["env_file"].append(item)
                elif cur_key == "profiles":
                    services[cur_service]["profiles"].append(item)
                continue

            # 服务内的 key: value
            if indent >= 4 and ":" in body:
                key, _, val = body.partition(":")
                key, val = key.strip(), _unquote(val.strip())

                # 长语法 volume 的续行（indent 更深的 source:/target:）
                if long_item is not None and key in ("source", "target", "type", "read_only"):
                    long_item[key] = val
                    continue

                # env_file 长语法的续行（`- path: x` 下面跟 `required: false`）
                if cur_key == "env_file" and key in ("required", "path"):
                    continue

                # YAML 合并键：`<<: *shared-api-worker-config` —— 把锚点的字段并进来
                if key == "<<":
                    for aref in re.findall(r"\*([\w.-]+)", val):
                        src = anchors.get(aref)
                        if not src:
                            continue
                        for ef in src.get("env_file") or []:
                            if ef not in services[cur_service]["env_file"]:
                                services[cur_service]["env_file"].append(ef)
                        for vv in src.get("volumes") or []:
                            if vv not in services[cur_service]["volumes"]:
                                services[cur_service]["volumes"].append(vv)
                        for kk, vv in (src.get("env_vars") or {}).items():
                            services[cur_service]["env_vars"].setdefault(kk, vv)
                    cur_key, long_item = None, None
                    continue

                if key in ("volumes", "env_file", "environment", "profiles") and not val:
                    cur_key, long_item = key, None
                    continue

                cur_key, long_item = None, None
                if key == "image" and val:
                    services[cur_service]["image"] = val
                elif key == "build":
                    services[cur_service]["build"] = val or "."
                elif key == "volumes" and val:
                    services[cur_service]["volumes"].append(val)
                elif key == "env_file" and val:
                    services[cur_service]["env_file"].append(val)
                elif key == "environment" and val:
                    for pair in val.split(","):
                        if "=" in pair:
                            k, _, v = pair.partition("=")
                            services[cur_service]["env_vars"][k.strip()] = v.strip()
                elif key == "profiles" and val:
                    services[cur_service]["profiles"].append(val)

        elif section == "volumes":
            if indent == 2 and body.endswith(":"):
                top_volumes.append(body[:-1].strip())
            elif indent == 2 and ":" not in body:
                top_volumes.append(_unquote(body))

    # 归一化：展开 ${VAR:-默认}、把长语法 volume 压成 "source:target"、算 profile 是否启用
    env = load_env(os.path.dirname(os.path.abspath(path)) or ".")
    act = active_profiles(env)
    for svc in services.values():
        files = []
        for v in svc.get("volumes") or []:
            if isinstance(v, dict):
                src, tgt = v.get("source", ""), v.get("target", "")
                if src or tgt:
                    files.append(f"{src}:{tgt}")
            elif isinstance(v, str) and v:
                files.append(v)
        svc["volumes"] = [subst(v, env) for v in files]
        svc["env_file"] = [d for e in (svc.get("env_file") or [])
                           if isinstance(e, str) and (d := subst(e, env))]
        svc["profiles"] = [p for p in (svc.get("profiles") or []) if isinstance(p, str)]
        svc["env_vars"] = {k: subst(str(v), env)
                           for k, v in (svc.get("env_vars") or {}).items()}
        svc["image"] = subst(svc.get("image"), env) if svc.get("image") else None
        svc["build"] = subst(svc.get("build"), env) if svc.get("build") else None
        svc["active"] = (not svc["profiles"]) or bool(set(svc["profiles"]) & act)

    return {"services": services, "named_volumes": top_volumes, "source": "scanner"}


# --------------------------------------------------------------------------
# 优先路径：docker compose config --format json
# --------------------------------------------------------------------------

def load_via_docker(compose_dir: str, compose_file: str | None) -> dict | None:
    if not shutil.which("docker"):
        return None
    cmd = ["docker", "compose"]
    if compose_file:
        cmd += ["-f", compose_file]
    cmd += ["config", "--format", "json"]
    try:
        p = subprocess.run(cmd, cwd=compose_dir, capture_output=True,
                           text=True, timeout=90)
    except Exception:
        return None
    if p.returncode != 0 or not p.stdout.strip():
        return None
    try:
        cfg = json.loads(p.stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(cfg, dict) or "services" not in cfg:
        return None

    services = {}
    for name, svc in (cfg.get("services") or {}).items():
        vols = []
        for v in (svc.get("volumes") or []):
            if isinstance(v, dict):
                if v.get("type") == "bind":
                    vols.append(f"{v.get('source')}:{v.get('target')}")
                else:
                    vols.append(f"{v.get('source')}:{v.get('target')}")
            else:
                vols.append(str(v))
        envs = []
        for e in (svc.get("env_file") or []):
            envs.append(e.get("path") if isinstance(e, dict) else str(e))
        env_vars = svc.get("environment") or {}
        if isinstance(env_vars, list):      # 少数情况下是 ["K=V", ...]
            env_vars = dict(p.split("=", 1) for p in env_vars if "=" in str(p))
        services[name] = {
            "image": svc.get("image"),
            "build": (svc.get("build") or {}).get("context") if isinstance(svc.get("build"), dict) else svc.get("build"),
            "volumes": vols,
            "env_file": envs,
            "env_vars": {k: str(v) for k, v in (env_vars or {}).items()},
            "profiles": svc.get("profiles") or [],
            "active": True,      # docker 已经把未启用的 profile 过滤掉了
        }
    return {"services": services,
            "named_volumes": sorted((cfg.get("volumes") or {}).keys()),
            "source": "docker compose config"}


# --------------------------------------------------------------------------
# 目录测量
# --------------------------------------------------------------------------

def dir_stats(path: str, max_files: int = 300_000) -> tuple[int, int]:
    """返回 (总字节, 文件数)。超过 max_files 就不再数下去，避免卡死。"""
    total = files = 0
    for root, _dirs, names in os.walk(path, onerror=lambda e: None):
        for n in names:
            fp = os.path.join(root, n)
            try:
                total += os.path.getsize(fp)
                files += 1
            except OSError:
                pass
            if files >= max_files:
                return total, files
    return total, files


def fmt_size(n: int) -> str:
    step = 1024.0
    val = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if val < step or unit == "TB":
            return f"{val:.1f} {unit}" if unit != "B" else f"{int(val)} B"
        val /= step
    return f"{val:.1f} TB"


# --------------------------------------------------------------------------
# 资产分类
# --------------------------------------------------------------------------

def classify_mount(host_path: str, target: str) -> tuple[str, str]:
    """返回 (类别, 理由)。类别: keep / maybe / rebuildable"""
    t = (target or "").lower()
    if any(h in t for h in REBUILDABLE_HINTS):
        return "rebuildable", "挂载目标看着是依赖/缓存/配置，通常可重建"
    if any(h in t for h in DATA_HINTS):
        return "keep", "挂载目标像是持久化数据，删了可能回不来"
    return "maybe", "看不出用途，建议人工确认"


def split_mount(spec: str) -> tuple[str, str, str]:
    """把 volume 串拆成 (host, target, mode)。命名卷的 host 会返回空。

    注意不能直接 split(":")——Windows 的宿主机路径自带盘符冒号（D:\\data:/x）。
    容器侧目标一定是以 "/" 开头的 Linux 路径，所以从右往左找 ":\\/" 来切。
    """
    if not spec:
        return "", "", ""
    s = spec.strip()

    mode = ""
    m = re.search(r":(ro|rw|z|Z|cached|delegated|consistent)$", s)
    if m:
        mode = m.group(1)
        s = s[: m.start()]

    idx = s.rfind(":/")
    if idx == -1:
        return "", s, mode          # 切不出来（残缺或命名卷），当没有宿主路径处理

    host, target = s[:idx].strip(), s[idx + 1:].strip()
    if not host:
        return "", target, mode
    # 宿主侧必须是路径；否则是命名卷
    if not host.startswith((".", "/", "~", "$")) and not re.match(r"^[A-Za-z]:[\\/]", host):
        return "", target, mode
    return host, target, mode


# --------------------------------------------------------------------------
# 审计主流程
# --------------------------------------------------------------------------

def audit(compose_dir: str, compose_file: str | None, use_docker: bool) -> dict:
    cfg = load_via_docker(compose_dir, compose_file) if use_docker else None
    if cfg is None:
        cf = compose_file or find_compose_file(compose_dir)
        if not cf:
            raise SystemExit(f"在 {compose_dir} 下找不到 compose 文件")
        cfg = scan_compose_file(cf)

    services_all = cfg["services"]
    services = {k: v for k, v in services_all.items() if v.get("active", True)}
    inactive = sorted(
        ({"name": k, "profiles": v.get("profiles") or []}
         for k, v in services_all.items() if not v.get("active", True)),
        key=lambda d: d["name"],
    )

    mounts = []          # 宿主机上的绑定挂载
    seen = set()
    for sname, svc in services.items():
        for spec in svc.get("volumes") or []:
            host, target, _mode = split_mount(spec)
            if not host:
                continue
            hp = os.path.expanduser(host)
            if not os.path.isabs(hp):
                hp = os.path.normpath(os.path.join(compose_dir, hp))
            key = os.path.normcase(hp)
            if key in seen:
                continue
            seen.add(key)
            exists = os.path.isdir(hp)
            size, files = dir_stats(hp) if exists else (0, 0)
            kind, why = classify_mount(hp, target)
            mounts.append({
                "service": sname, "host": hp, "target": target,
                "rel": os.path.relpath(hp, compose_dir) if exists else host,
                "exists": exists, "size": size, "files": files,
                "kind": kind, "why": why,
            })
    mounts.sort(key=lambda m: -m["size"])

    images = sorted({s["image"] for s in services.values() if s.get("image")})

    env_files, secrets = [], []
    # compose 目录下的 .env 是默认插值来源，服务即使没写 env_file 也强烈依赖它
    default_env = os.path.join(compose_dir, ".env")
    if os.path.isfile(default_env):
        env_files.append(default_env)
    for sname, svc in services.items():
        for ef in svc.get("env_file") or []:
            efp = ef if os.path.isabs(ef) else os.path.join(compose_dir, ef)
            if efp not in env_files:
                env_files.append(efp)
            if os.path.isfile(efp):
                try:
                    for ln in open(efp, encoding="utf-8", errors="replace"):
                        ln = ln.strip()
                        if not ln or ln.startswith("#") or "=" not in ln:
                            continue
                        k, _, v = ln.partition("=")
                        if v.strip() and is_secret_pair(k.strip(), v.strip()):
                            secrets.append((k.strip(), f".env 文件（{os.path.basename(efp)}）"))
                except OSError:
                    pass
        # docker compose config 会把 env_file 内联进 environment，所以这里也要扫
        for k, v in (svc.get("env_vars") or {}).items():
            if not str(v).strip():
                continue
            if not is_secret_pair(k, v):
                continue
            if SECRET_HINTS.search(k):
                secrets.append((k, f"服务 {sname} 的 environment"))
            elif value_looks_secret(v):
                # 名字人畜无害（CELERY_BROKER_URL），值里藏着口令
                secrets.append((k, f"服务 {sname} 的 environment（值内嵌凭据）"))
            else:
                secrets.append((k, f"服务 {sname} 的 environment（名字含凭据词）"))
    secrets = sorted(set(secrets))

    # .env 里的密钥项：即使没有服务显式 env_file 引用它，也要单独扫一遍
    if os.path.isfile(default_env):
        try:
            for ln in open(default_env, encoding="utf-8", errors="replace"):
                ln = ln.strip()
                if not ln or ln.startswith("#") or "=" not in ln:
                    continue
                k, _, v = ln.partition("=")
                if v.strip() and is_secret_pair(k.strip(), v.strip()):
                    secrets.append((k.strip(), "`.env`（compose 默认插值文件）"))
        except OSError:
            pass
    secrets = sorted(set(secrets))

    # 源码目录体积：从 compose 目录往上找 git 根
    code_size, git_root = 0, None
    probe = compose_dir
    for _ in range(5):
        if os.path.isdir(os.path.join(probe, ".git")):
            git_root = probe
            code_size = dir_stats(probe)[0]
            break
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent

    return {
        "compose_dir": compose_dir,
        "source": cfg["source"],
        "services": services,
        "inactive": inactive,
        "images": images,
        "named_volumes": cfg.get("named_volumes") or [],
        "mounts": mounts,
        "env_files": env_files,
        "secrets": secrets,
        "code_size": code_size,
        "git_root": git_root,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def find_compose_file(d: str) -> str | None:
    for n in COMPOSE_NAMES:
        p = os.path.join(d, n)
        if os.path.isfile(p):
            return p
    return None


# --------------------------------------------------------------------------
# 报告
# --------------------------------------------------------------------------

def render(report: dict) -> str:
    r = report
    keep = [m for m in r["mounts"] if m["kind"] == "keep"]
    maybe = [m for m in r["mounts"] if m["kind"] == "maybe"]
    rebuild = [m for m in r["mounts"] if m["kind"] == "rebuildable"]

    keep_total = sum(m["size"] for m in keep)
    all_total = sum(m["size"] for m in r["mounts"])

    L = []
    A = L.append
    A(f"# 服务栈审计报告：{os.path.basename(os.path.normpath(r['compose_dir']))}")
    A("")
    A(f"- 审计目录：`{r['compose_dir']}`")
    A(f"- 生成时间：{r['generated_at']}")
    A(f"- 数据来源：{r['source']}")
    A("")
    A("## 结论速览")
    A("")
    A("| 项目 | 数量 | 体积 | 删了能重建吗 |")
    A("| --- | ---: | ---: | --- |")
    A(f"| 服务（已启用） | {len(r['services'])} | — | — |")
    if r.get("inactive"):
        A(f"| 服务（被 profile 屏蔽） | {len(r['inactive'])} | — | 本次审计不计入 |")
    A(f"| 镜像 | {len(r['images'])} | 需 `docker images` 查 | 能，重新拉 |")
    code_label = "源码目录（含 .git）"
    if r.get("git_root"):
        code_label = f"源码目录 `{os.path.relpath(r['git_root'], r['compose_dir'])}`（含 .git）"
    A(f"| {code_label} | — | {fmt_size(r['code_size'])} | 能，重新 clone |")
    A(f"| 宿主机挂载目录 | {len(r['mounts'])} | {fmt_size(all_total)} | **部分不能** |")
    A(f"| ↳ 疑似持久化数据 | {len(keep)} | {fmt_size(keep_total)} | **不能，先归档** |")
    _ef_here = sorted({os.path.normcase(os.path.abspath(p)) for p in r["env_files"]
                       if os.path.isfile(p)})
    _ef_ref = len(r["env_files"]) - len(_ef_here)
    if _ef_ref:
        A(f"| 环境变量文件 | {len(_ef_here)} 个存在 | — | **不能，含密钥** |")
        A(f"| ↳ compose 引用但不存在 | {_ef_ref} | — | 正常，通常在部署时才生成 |")
    else:
        A(f"| 环境变量文件 | {len(_ef_here)} | — | **不能，含密钥** |")
    A("")

    if keep:
        A("## 不能重建的：宿主机上的持久化数据")
        A("")
        A("| 疑似数据目录 | 挂载到容器 | 体积 | 文件数 | 判断依据 |")
        A("| --- | --- | ---: | ---: | --- |")
        for m in keep:
            rel = m["rel"] if m["exists"] else f"{m['rel']}（**不存在**）"
            A(f"| `{rel}` | `{m['target']}` | {fmt_size(m['size'])} | {m['files']:,} | {m['why']} |")
        A("")
        A(f"> 合计 **{fmt_size(keep_total)}**。这些目录在宿主机上（不是 Docker 命名卷），"
          f"直接复制目录就能冷备份，不需要 docker 在跑。")
        A("")

    if maybe:
        A("## 需要人工确认的挂载")
        A("")
        A("| 目录 | 挂载到容器 | 体积 | 文件数 |")
        A("| --- | --- | ---: | ---: |")
        for m in maybe:
            rel = m["rel"] if m["exists"] else f"{m['rel']}（**不存在**）"
            A(f"| `{rel}` | `{m['target']}` | {fmt_size(m['size'])} | {m['files']:,} |")
        A("")

    if rebuild:
        A("## 看着可以重建的挂载")
        A("")
        A("| 目录 | 挂载到容器 | 体积 | 判断依据 |")
        A("| --- | --- | ---: | --- |")
        for m in rebuild:
            rel = m["rel"] if m["exists"] else f"{m['rel']}（**不存在**）"
            A(f"| `{rel}` | `{m['target']}` | {fmt_size(m['size'])} | {m['why']} |")
        A("")

    if r["secrets"]:
        # 同一个变量会在 7 个服务里各出现一次，按变量名归组再看，否则表里全是重复行
        grouped: dict[str, list[str]] = {}
        for k, src in r["secrets"]:
            grouped.setdefault(k, [])
            if src not in grouped[k]:
                grouped[k].append(src)
        A("## 看着像密钥的变量（这些值删了不可恢复）")
        A("")
        A(f"共 **{len(grouped)}** 个变量名，出现在 **{len(r['secrets'])}** 处。")
        A("")
        A("| 变量名 | 出现位置 |")
        A("| --- | --- |")
        for k in sorted(grouped):
            where = grouped[k]
            if len(where) == 1:
                A(f"| `{k}` | {where[0]} |")
            else:
                shown = "、".join(where[:3])
                more = f" 等 {len(where)} 处" if len(where) > 3 else ""
                A(f"| `{k}` | {shown}{more} |")
        A("")
        A("> 归档时整份留，别外传、别提交进 Git。注意 compose 里的 `env_file` "
          "会在 `docker compose config` 输出里被内联成 `environment`，")
        A("> 所以上面有些条目标的是「服务 X 的 environment」而不是某个文件名——"
          "真正那份文件要自己顺着 compose 里的 `env_file:` 找。")
        A("")

    A("## 服务与镜像清单")
    A("")
    A("| 服务 | 镜像 | 宿主机挂载数 |")
    A("| --- | --- | ---: |")
    for name, svc in sorted(r["services"].items()):
        cnt = len([v for v in (svc.get("volumes") or []) if split_mount(v)[0]])
        img = svc.get("image") or (f"build: {svc.get('build')}" if svc.get("build") else "—")
        A(f"| `{name}` | `{img}` | {cnt} |")
    A("")

    if r.get("inactive"):
        A("## 被 profile 屏蔽的服务（本次未启用，不计入上面的统计）")
        A("")
        A("这些服务定义在 compose 文件里，但用 `profiles` 关着，`docker compose up` 不会启动它们。")
        A("要审计它们，先设置 `COMPOSE_PROFILES` 再跑一次。")
        A("")
        for d in r["inactive"]:
            prof = ", ".join(d["profiles"])
            A(f"- `{d['name']}`" + (f"（profile: `{prof}`）" if prof else ""))
        A("")

    if r["named_volumes"]:
        A("## Docker 命名卷（数据在 Docker 内部，得先进容器才看得到）")
        A("")
        for v in r["named_volumes"]:
            A(f"- `{v}`")
        A("")
        A("> 命名卷不在宿主机文件系统里。要保留只能 `docker run --rm -v <卷>:/from alpine tar cf - -C /from .` 之类导出。")
        A("")

    A("## 建议的下一步")
    A("")
    A("1. **先归档**上面那张「不能重建」表里的目录，归档后验证文件数对得上。")
    _ef_list = sorted({os.path.normcase(os.path.abspath(p)) for p in r["env_files"]
                       if os.path.isfile(p)})
    if _ef_list:
        A(f"2. 环境变量文件（{', '.join('`' + os.path.relpath(p, r['compose_dir']) + '`' for p in _ef_list)}）"
          f"整份留档，别只留变量名。")
    else:
        A("2. 没找到实际存在的环境变量文件——但密钥可能写在 compose 的 `environment:` 里，"
          "上面密钥表已列出变量名，值要自己去 compose/.env 里对。")
    A("3. 命名卷逐个确认：如果里面没有不可重建的数据，可以不管。")
    A("4. 镜像和源码目录可以最后删——它们都能重新拉回来。")
    A("5. 删之前跑一次 `docker compose down`，确认没有在跑的容器还挂着这些目录。")
    A("")
    A("---")
    A("")
    A(f"由 compose-audit v{VERSION} 生成。它只读文件和调 `docker compose config`，"
      f"不删任何东西、不改任何配置。")
    return "\n".join(L)


def render_json(report: dict) -> str:
    """JSON 版。密钥类变量的值一律打码——这个输出经常被重定向到文件或贴给别人。"""
    slim = dict(report)
    slim["mounts"] = [
        {**m, "size_h": fmt_size(m["size"])} for m in report["mounts"]
    ]

    def masked(svc: dict) -> dict:
        out = {}
        for k, v in (svc.get("env_vars") or {}).items():
            if is_secret_pair(k, v):
                out[k] = "***REDACTED***"
            else:
                out[k] = v
        return {**svc, "env_vars": out}

    slim["services"] = {k: masked(v) for k, v in report["services"].items()}
    slim["note"] = "environment 里看着像密钥的值已打码；Markdown 报告只列变量名，不含值。"
    return json.dumps(slim, ensure_ascii=False, indent=2)


# --------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="compose-audit",
        description="审计一个 docker compose 项目，分清哪些东西删了就回不来。",
    )
    ap.add_argument("path", nargs="?", default=".",
                    help="compose 目录，或直接指定 compose 文件（默认当前目录）")
    ap.add_argument("-f", "--file", help="显式指定 compose 文件")
    ap.add_argument("--json", action="store_true", help="输出 JSON 而不是 Markdown")
    ap.add_argument("-o", "--out", help="把报告写到文件（默认打印到终端）")
    ap.add_argument("--no-docker", action="store_true",
                    help="不调用 docker CLI，只用内置扫描器")
    ap.add_argument("--version", action="version", version=f"compose-audit {VERSION}")
    args = ap.parse_args(argv)

    p = os.path.abspath(args.path)
    if os.path.isfile(p):
        compose_dir, compose_file = os.path.dirname(p), p
    else:
        compose_dir, compose_file = p, args.file
        if compose_file and not os.path.isabs(compose_file):
            compose_file = os.path.join(compose_dir, compose_file)

    if not os.path.isdir(compose_dir):
        print(f"目录不存在：{compose_dir}", file=sys.stderr)
        return 2
    if not compose_file and not find_compose_file(compose_dir):
        print(f"在 {compose_dir} 下找不到 compose 文件"
              f"（{' / '.join(COMPOSE_NAMES)}）", file=sys.stderr)
        return 2

    report = audit(compose_dir, compose_file, use_docker=not args.no_docker)
    text = render_json(report) if args.json else render(report)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"报告已写入 {args.out}")
        if not args.json:
            print(f"  {len(report['mounts'])} 个宿主机挂载，"
                  f"{len([m for m in report['mounts'] if m['kind'] == 'keep'])} 个疑似不可重建")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
