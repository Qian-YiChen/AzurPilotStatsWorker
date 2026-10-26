# AzurPilot Stats Worker — B 节点

本地 Python 识别服务：持久化接收 A 的批次，从 123 盘下载图片，复用 Alas 的科研和掉落识别模块，将结构化结果和未知图标候选上报 C。无需 Redis，不复制客户端代码。

## 安装与启动

推荐使用自带识别环境的 Docker 镜像：

```sh
# 复制 .env.example 为 .env，填写节点令牌、C 地址和 123 盘凭据。
docker compose pull
docker compose up -d
```

镜像 `ghcr.io/wess09/azurpilotstatsworker:latest` 包含 Python、OCR 依赖、基础模板和模型。Dockerfile 固定使用公开客户端提交 `d0ce990caf24b821e4250909ada78016f95d31d0`，其源码与许可证位于镜像 `/opt/alas/`；构建不读取宿主机客户端目录或配置。每次镜像构建都在断网条件下验证真实 OCR、候选提取和模板补齐。

Compose 自动设置容器内 `ALAS_ROOT / ALAS_PYTHON` 和监听地址，不需要挂载宿主机 Python 环境。任务和结构化结果保存在 `worker-state` 卷，删除容器不会丢失。`WORKER_IMAGE` 可指定版本；支持 `latest / main / sha-<完整提交号>`，`v*` 标签发布版本镜像。原生开发仍可使用：

```sh
uv sync --locked
# 复制 .env.example 为 .env 并填写配置，由 uv 加载环境：
uv run --env-file .env python -m worker.app
```

需要 Python 3.12+。`ALAS_ROOT` 指向可用的 AzurLaneAutoScript/AzurPilot 工作目录，包含 `module/`、`assets/` 和本地 OCR 模型；`ALAS_PYTHON` 指向已安装该客户端依赖的解释器，Windows 例如 `../AzurLaneAutoScript/.venv/Scripts/python.exe`。识别在独立子进程执行，不启动游戏或设备自动化。请固定客户端版本并先跑真实适配测试，更新客户端后重新验证。

设置 `WORKER_TOKEN` 与 A 相同，`STATISTICS_TOKEN` 与 C 相同。填写 123 盘开放平台凭据和 C 地址。通过 `deploy/cloudflared.example.yml` 对已有 Tunnel 配置公开 B 的本地端口。WebSocket 地址为 `wss://recognizer.example.com/internal/v1/batches`，必须携带 `Authorization: Bearer <WORKER_TOKEN>`。默认监听本机，不直接公开裸端口；如启用 Cloudflare Access，A 还需配置服务令牌，C 的原图预览链路也须获准通过 Access。

**每个 DATA_DIR 只运行一个进程、一个识别执行器。** 不要启动多个 uvicorn worker。生产应使用进程管理器重启服务，并限制日志大小。

## 存储与恢复

SQLite 以 WAL/FULL 保存批次、待办、结果及进度，事务提交后才确认 A。重复批次沿用原 ID；相同图片组不会再次建立初始识别任务。处理时下载当前组所需文件到临时工作目录（最多 500 MiB），只逐张解码，不常驻整批原图。成功、失败或进程重启后均清理本地原图；失败重试重新下载。

`MAX_FILE_BYTES / MAX_GROUP_BYTES` 默认 10 MiB / 500 MiB；修改大小限制时三节点使用相同配置。

C 离线时保留结构化结果和小候选图，重传不重复 OCR。C 接收后清除本地候选图副本；SQLite 仍保留任务/结果历史，须备份并监控容量。123 盘是唯一长期原图存储。

123 盘下载走官方 [download_info](https://123yunpan.yuque.com/org-wiki-123yunpan-muaork/cr6ced/fnf60phsushn8ip2)；逐块落盘并校验大小和 SHA-256。确认账户开放平台下载额度能覆盖实际流量，额度/鉴权失败保留待办，不丢任务。

## 识别与模板

输入为 PNG，宽 1280、高为 720 的整数倍，每文件最多 10 帧。科研完成截图应包括队列项目卡和领奖帧；刷新截图场景用 `research_refresh`。通用掉落使用获得道具弹窗，`opsi_*`、`commission*` 场景选择对应基础模板。

未知物品自动裁剪 49×49 图标，附源文件、帧、槽位和坐标。候选需要在 C 后台人工命名分类；不会猜测其语义。缺失模板、项目或数量异常产生 `needs_review`，不进入正式统计。

每 30 秒同步 C 心跳、模板和指令；发布包校验 SHA-256，任务固定使用开始时的版本。试识别使用草稿且不入统计。发布、回滚和手动重处理独立于 A 的 30 份门槛。`POST /internal/v1/preview` 仅供 C 认证调用，按需下载并返回一帧，响应后删除临时文件。

## 验证

```sh
uv run pytest -q
# 加载 ALAS_ROOT、ALAS_PYTHON 后包含真实 OCR 和补充模板闭环测试：
uv run pytest tests/test_alas_adapter.py -q
# 显式配置 STATISTICS_TEST_URL/TOKEN/ADMIN，指向全新专用 C 数据库后：
uv run pytest tests/test_c_integration.py -q
```

常规测试无需云盘凭据；真实 OCR 测试使用客户端已有资源构造截图，不提交玩家截图。覆盖鉴权、事务确认、重启恢复、结果重试、临时图清理、模板快照和指令进度。

B→C 联调运行真实 OCR、HTTP 及 PostgreSQL，用本地文件替换云盘下载，验证候选分类、发布、重跑和重复结果只统计一次。不要对生产环境运行该测试。
