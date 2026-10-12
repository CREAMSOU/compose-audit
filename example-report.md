# 服务栈审计报告：docker

- 审计目录：`/home/you/dify/docker`
- 生成时间：2026-10-12 16:42:09
- 数据来源：docker compose config

## 结论速览

| 项目 | 数量 | 体积 | 删了能重建吗 |
| --- | ---: | ---: | --- |
| 服务（已启用） | 13 | — | — |
| 镜像 | 10 | 需 `docker images` 查 | 能，重新拉 |
| 源码目录 `..`（含 .git） | — | 1.4 GB | 能，重新 clone |
| 宿主机挂载目录 | 18 | 915.2 MB | **部分不能** |
| ↳ 疑似持久化数据 | 5 | 915.2 MB | **不能，先归档** |
| 环境变量文件 | 1 | — | **不能，含密钥** |

## 不能重建的：宿主机上的持久化数据

| 疑似数据目录 | 挂载到容器 | 体积 | 文件数 | 判断依据 |
| --- | --- | ---: | ---: | --- |
| `volumes\app\storage` | `/app/api/storage` | 287.9 MB | 7,657 | 挂载目标像是持久化数据，删了可能回不来 |
| `volumes\weaviate` | `/var/lib/weaviate` | 265.1 MB | 199 | 挂载目标像是持久化数据，删了可能回不来 |
| `volumes\db\data` | `/var/lib/postgresql/data` | 236.2 MB | 2,435 | 挂载目标像是持久化数据，删了可能回不来 |
| `volumes\plugin_daemon` | `/app/storage` | 126.0 MB | 7,767 | 挂载目标像是持久化数据，删了可能回不来 |
| `volumes\redis\data` | `/data` | 2.4 KB | 1 | 挂载目标像是持久化数据，删了可能回不来 |

> 合计 **915.2 MB**。这些目录在宿主机上（不是 Docker 命名卷），直接复制目录就能冷备份，不需要 docker 在跑。

## 需要人工确认的挂载

| 目录 | 挂载到容器 | 体积 | 文件数 |
| --- | --- | ---: | ---: |
| `/home/you/dify/docker\nginx\docker-entrypoint.sh（**不存在**）` | `/docker-entrypoint-mount.sh` | 0 B | 0 |
| `nginx\ssl` | `/etc/ssl` | 0 B | 1 |
| `volumes\certbot\conf\live` | `/etc/letsencrypt/live` | 0 B | 0 |
| `volumes\certbot\conf` | `/etc/letsencrypt` | 0 B | 0 |
| `volumes\certbot\www` | `/var/www/html` | 0 B | 0 |
| `/home/you/dify/docker\ssrf_proxy\docker-entrypoint.sh（**不存在**）` | `/docker-entrypoint-mount.sh` | 0 B | 0 |

## 看着可以重建的挂载

| 目录 | 挂载到容器 | 体积 | 判断依据 |
| --- | --- | ---: | --- |
| `nginx\conf.d` | `/etc/nginx/conf.d` | 3.3 KB | 挂载目标看着是依赖/缓存/配置，通常可重建 |
| `volumes\sandbox\conf` | `/conf` | 1.2 KB | 挂载目标看着是依赖/缓存/配置，通常可重建 |
| `/home/you/dify/docker\nginx\nginx.conf.template（**不存在**）` | `/etc/nginx/nginx.conf.template` | 0 B | 挂载目标看着是依赖/缓存/配置，通常可重建 |
| `/home/you/dify/docker\nginx\proxy.conf.template（**不存在**）` | `/etc/nginx/proxy.conf.template` | 0 B | 挂载目标看着是依赖/缓存/配置，通常可重建 |
| `/home/you/dify/docker\nginx\https.conf.template（**不存在**）` | `/etc/nginx/https.conf.template` | 0 B | 挂载目标看着是依赖/缓存/配置，通常可重建 |
| `volumes\sandbox\dependencies` | `/dependencies` | 0 B | 挂载目标看着是依赖/缓存/配置，通常可重建 |
| `/home/you/dify/docker\ssrf_proxy\squid.conf.template（**不存在**）` | `/etc/squid/squid.conf.template` | 0 B | 挂载目标看着是依赖/缓存/配置，通常可重建 |

## 看着像密钥的变量（这些值删了不可恢复）

共 **18** 个变量名，出现在 **89** 处。

| 变量名 | 出现位置 |
| --- | --- |
| `API_KEY` | 服务 sandbox 的 environment |
| `AUTHENTICATION_APIKEY_ALLOWED_KEYS` | 服务 weaviate 的 environment |
| `AUTHENTICATION_APIKEY_USERS` | 服务 weaviate 的 environment |
| `CELERY_BROKER_URL` | `.env`（compose 默认插值文件）、服务 api 的 environment（值内嵌凭据）、服务 api_websocket 的 environment（值内嵌凭据） 等 8 处 |
| `CODE_EXECUTION_API_KEY` | `.env`（compose 默认插值文件）、服务 api 的 environment、服务 api_websocket 的 environment 等 8 处 |
| `DB_PASSWORD` | `.env`（compose 默认插值文件）、服务 api 的 environment、服务 api_websocket 的 environment 等 8 处 |
| `DIFY_INNER_API_KEY` | 服务 plugin_daemon 的 environment |
| `INNER_API_KEY_FOR_PLUGIN` | 服务 api 的 environment、服务 worker 的 environment |
| `PLUGIN_DAEMON_KEY` | `.env`（compose 默认插值文件）、服务 api 的 environment、服务 api_websocket 的 environment 等 8 处 |
| `PLUGIN_DIFY_INNER_API_KEY` | `.env`（compose 默认插值文件）、服务 api 的 environment、服务 api_websocket 的 environment 等 8 处 |
| `POSTGRES_PASSWORD` | 服务 db_postgres 的 environment |
| `REDISCLI_AUTH` | 服务 redis 的 environment（名字含凭据词） |
| `REDIS_PASSWORD` | `.env`（compose 默认插值文件）、服务 api 的 environment、服务 api_websocket 的 environment 等 8 处 |
| `SANDBOX_API_KEY` | `.env`（compose 默认插值文件）、服务 api 的 environment、服务 api_websocket 的 environment 等 8 处 |
| `SERVER_KEY` | 服务 plugin_daemon 的 environment |
| `WEAVIATE_API_KEY` | `.env`（compose 默认插值文件）、服务 api 的 environment、服务 api_websocket 的 environment 等 8 处 |
| `WEAVIATE_AUTHENTICATION_APIKEY_ALLOWED_KEYS` | `.env`（compose 默认插值文件）、服务 api 的 environment、服务 api_websocket 的 environment 等 8 处 |
| `WEAVIATE_AUTHENTICATION_APIKEY_USERS` | `.env`（compose 默认插值文件）、服务 api 的 environment、服务 api_websocket 的 environment 等 8 处 |

> 归档时整份留，别外传、别提交进 Git。注意 compose 里的 `env_file` 会在 `docker compose config` 输出里被内联成 `environment`，
> 所以上面有些条目标的是「服务 X 的 environment」而不是某个文件名——真正那份文件要自己顺着 compose 里的 `env_file:` 找。

## 服务与镜像清单

| 服务 | 镜像 | 宿主机挂载数 |
| --- | --- | ---: |
| `api` | `langgenius/dify-api:1.15.0` | 1 |
| `api_websocket` | `langgenius/dify-api:1.15.0` | 0 |
| `db_postgres` | `postgres:15-alpine` | 1 |
| `init_permissions` | `busybox:latest` | 1 |
| `nginx` | `nginx:latest` | 9 |
| `plugin_daemon` | `langgenius/dify-plugin-daemon:0.6.3-local` | 1 |
| `redis` | `redis:6-alpine` | 1 |
| `sandbox` | `langgenius/dify-sandbox:0.2.15` | 2 |
| `ssrf_proxy` | `ubuntu/squid:latest` | 2 |
| `weaviate` | `semitechnologies/weaviate:1.27.0` | 1 |
| `web` | `langgenius/dify-web:1.15.0` | 0 |
| `worker` | `langgenius/dify-api:1.15.0` | 1 |
| `worker_beat` | `langgenius/dify-api:1.15.0` | 0 |

## 建议的下一步

1. **先归档**上面那张「不能重建」表里的目录，归档后验证文件数对得上。
2. 环境变量文件（`.env`）整份留档，别只留变量名。
3. 命名卷逐个确认：如果里面没有不可重建的数据，可以不管。
4. 镜像和源码目录可以最后删——它们都能重新拉回来。
5. 删之前跑一次 `docker compose down`，确认没有在跑的容器还挂着这些目录。

---

由 compose-audit v0.1.0 生成。它只读文件和调 `docker compose config`，不删任何东西、不改任何配置。