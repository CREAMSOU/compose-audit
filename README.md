# compose-audit

> 拆掉一个 docker compose 项目之前，先搞清楚：哪些东西删了就回不来。

[![Python](https://img.shields.io/badge/python-3.8%2B-3776AB)](#)
[![Dependencies](https://img.shields.io/badge/dependencies-none-brightgreen)](#)
[![License](https://img.shields.io/badge/license-MIT-green)](./LICENSE)

一个自建服务（Dify / Nextcloud / 自搭的各种 stack）用了半年，想卸载。`docker compose down -v` 一条命令下去很爽，但那条命令会连带删掉：数据库、上传的文件、向量库索引、以及几十条只存在于 `.env` 里的密钥。

这份工具做的事很简单：**只读地扫一遍 compose 文件和数据目录，按「删了能不能重建」分成三档，输出一份 Markdown 报告**。它不删东西、不改配置。

## 它回答什么问题

| 你想知道的 | 报告里对应哪一块 |
| --- | --- |
| 哪些目录装了不可重建的数据？ | 「不能重建的：宿主机上的持久化数据」 |
| 哪些目录复制一下就能备份？ | 同上——这些是宿主机目录，`cp -r` 就行，不需要 docker 在跑 |
| 哪些删了无所谓（依赖、缓存）？ | 「看着可以重建的挂载」 |
| 有没有密钥会被一起删掉？ | 「看着像密钥的变量」——只列变量名，**不打值** |
| 镜像和源码要重新拉多久？ | 「结论速览」里的镜像数和源码目录体积 |

## 用法

只需要 Python 3.8+，**没有任何第三方依赖**。

```bash
# 审计当前目录的 compose 项目
python compose_audit.py

# 指定目录
python compose_audit.py /path/to/project

# 报告写到文件
python compose_audit.py /path/to/project -o audit.md

# 输出 JSON（方便接到别的脚本里）
python compose_audit.py /path/to/project --json

# 完全不碰 docker（CI 里、docker 没装、或者不想拉起 daemon 时用）
python compose_audit.py /path/to/project --no-docker
```

### 参数

| 参数 | 说明 |
| --- | --- |
| `path` | compose 目录，或直接给 compose 文件。默认当前目录 |
| `-f, --file` | 显式指定 compose 文件 |
| `-o, --out` | 把报告写到文件（默认打印到终端） |
| `--json` | 输出 JSON 而不是 Markdown |
| `--no-docker` | 只用内置扫描器，不调用 docker CLI |
| `--version` | 版本号 |

## 两条数据路径

| | `docker compose config`（默认） | 内置扫描器（`--no-docker`） |
| --- | --- | --- |
| 依赖 | 需要 docker CLI（**不需要 daemon 在跑**） | 纯标准库 |
| 覆盖范围 | 解析后的最终配置，含 YAML 锚点、profile 过滤 | 直接读 compose 文件，自己实现子集解析 |
| 何时用 | 默认，最准 | docker 不可用，或想零依赖跑 |

两条路径的结果应当一致（挂载数、服务数、不可重建体积）。自检里有一项专门校验这点。

## 输出示例

`example-report.md` 是一份真实跑出来的报告（拿 Dify 的 compose 做样本，路径已脱敏），可以直接打开看格式。

```markdown
## 结论速览

| 项目 | 数量 | 体积 | 删了能重建吗 |
| --- | ---: | ---: | --- |
| 服务（已启用） | 13 | — | — |
| 镜像 | 10 | 需 `docker images` 查 | 能，重新拉 |
| 源码目录 `..`（含 .git） | — | 1.4 GB | 能，重新 clone |
| 宿主机挂载目录 | 18 | 915.2 MB | **部分不能** |
| ↳ 疑似持久化数据 | 5 | 915.2 MB | **不能，先归档** |

## 不能重建的：宿主机上的持久化数据

| 疑似数据目录 | 挂载到容器 | 体积 | 文件数 |
| --- | --- | ---: | ---: |
| `volumes/app/storage` | `/app/api/storage` | 287.9 MB | 7,657 |
| `volumes/db/data` | `/var/lib/postgresql/data` | 236.2 MB | 2,435 |
```

## 判定逻辑

**数据目录 vs 可重建目录**，看挂载的**容器内路径**：

- 命中 `postgres` / `data` / `storage` / `uploads` / `weaviate` / `qdrant` / `minio` 这类词 → 判为「疑似持久化数据」，删了可能回不来
- 命中 `dependencies` / `cache` / `config` / `cert` / `static` 这类词 → 判为「看着可重建」
- 两边都不沾 → 归入「需要人工确认」

**密钥识别**按三条并行判断（任一命中即算）：

1. 变量名匹配 `SECRET|PASSWORD|TOKEN|API_KEY|ACCESS_KEY|...`
2. 变量名匹配 `AUTH|PWD` 等裸凭据名（`REDISCLI_AUTH`）——但排除 `AUTHORIZATION` / `AUTHENTICATION` 这种开关和用户名单
3. **值里内嵌凭据**：`redis://:口令@host` 这种，变量名可能完全人畜无害（`CELERY_BROKER_URL`），仍然要打码

第 3 条是实践里最容易漏的一类。很多 compose 的密码既不在 `PASSWORD` 变量里，也不在 `.env` 的显眼位置，而是藏在连接串中间。

## 安全性

- **只读**：工具不删除、不移动、不修改任何文件，只读 compose 配置 + `os.stat` 目录体积 + 调用 `docker compose config`（只输出配置，不启动容器）。
- **密钥打码**：Markdown 报告只列**变量名**；JSON 输出会把识别为密钥的**值**替换成 `***REDACTED***`。这个输出经常被重定向到文件或贴给别人看，所以默认就不能带明文。
- 但请仍然把报告当成敏感文件对待——变量名本身也泄露了你的技术栈。

## 自检

```bash
python selftest.py
```

覆盖密钥判定、URL 凭据擦除、YAML 锚点合并、`env_file` 长语法、`COMPOSE_PROFILES` 变量插值、以及「报告里不出现明文口令」。全部用临时生成的测试数据，不碰你的真实项目。

想额外验证两条数据路径的一致性，给个真实项目目录：

```bash
COMPOSE_AUDIT_TARGET=/path/to/project python selftest.py
```

## 注意

- 工具给的是**判断建议**，不是保证。分类依据是挂载路径的关键词，遇到自定义命名（比如数据目录叫 `blob`）可能判错——「需要人工确认」那一档就是给这种情况留的。
- 报告里的体积是**当前磁盘占用**。容器里的命名卷（named volume）不在宿主机文件系统里，工具只能提示你「命名卷逐个确认」，无法直接量出体积——要看那些得用 `docker system df -v`。
- 环境变量文件如果 compose 引用了但磁盘上不存在，属于正常现象（很多 stack 在首次部署时才生成），工具会在报告里单独标注这类引用数。

## License

[MIT](./LICENSE)
