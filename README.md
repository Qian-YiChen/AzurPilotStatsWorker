# AzurPilot Stats Worker — B 节点

本地 Python 识别服务：持久化接收 A 的批次，从 123 盘下载图片，复用 Alas 的科研和掉落识别模块，将结构化结果和未知图标候选上报 C。无需 Redis，不复制客户端代码。

## 安装与启动

推荐使用自带识别环境的 Docker 镜像：

```sh
# 复制 .env.example 为 .env，填写节点令牌、C 地址和 123 盘凭据。
docker compose pull
docker compose up -d
```

镜像 `ghcr.io/wess09/azurpilotstatsworker:latest` 包含 Python、OCR 依赖、基础模板和模型。Dockerfile 固定使用公开客户端提交 `c0747b09bad2a2eb356aec26f20e4ffbdc2d3978`，其源码与许可证位于镜像 `/opt/alas/`；构建不读取宿主机客户端目录或配置。每次镜像构建都在断网条件下验证真实 OCR、候选提取、模板补齐和科研刷新入口；科研图像识别不依赖 Android 设备控制模块。

Compose 自动设置容器内 `ALAS_ROOT / ALAS_PYTHON` 和监听地址，不需要挂载宿主机 Python 环境。任务和结构化结果保存在 `worker-state` 卷，删除容器不会丢失。`WORKER_IMAGE` 可指定版本；支持 `latest / main / sha-<完整提交号>`，`v*` 标签发布版本镜像。原生开发仍可使用：

```sh
uv sync --locked
# 复制 .env.example 为 .env 并填写配置，由 uv 加载环境：
uv run --env-file .env python -m worker.app
```

需要 Python 3.12+。`ALAS_ROOT` 指向可用的 AzurLaneAutoScript/AzurPilot 工作目录，包含 `module/`、`assets/` 和本地 OCR 模型；`ALAS_PYTHON` 指向已安装该客户端依赖的解释器，Windows 例如 `../AzurLaneAutoScript/.venv/Scripts/python.exe`。识别在独立子进程执行，不启动游戏或设备自动化。请固定客户端版本并先跑真实适配测试，更新客户端后重新验证。

设置 `WORKER_TOKEN` 与 A 相同，`STATISTICS_TOKEN` 与 C 相同。填写 123 盘开放平台凭据和 C 地址。配置 `UPLOADER_WS_URL=wss://上传域名/internal/v1/worker/connect`，由 B 主动连接 A，A 按半小时时段推送已上传的样本。B 提交 SQLite 后才确认；断线每 5 秒重连，重复批次复用已有任务。健康检查的 `uploader_connected` 表示连接状态。

A 只有 HTTP/NAT 入口时，使用 SSH 转发加密传输（见 [部署示例](deploy/reverse-ssh.md)），将 URL 指向本机 `ws://127.0.0.1:18092/internal/v1/worker/connect`。B 容器使用 Linux host 网络并监听 `127.0.0.1:8091` 才能访问宿主机转发端口；标准桥接网络不能通过容器 loopback 访问宿主机。不要在公网明文 WS 中发送节点令牌。

通过 `deploy/cloudflared.example.yml` 公开 B 本机 HTTP 端口，供 C 原图预览。兼容的 `/internal/v1/batches` 仍接收旧版 A 主动连接，要求相同 Bearer 认证；反向模式无需 A 访问 B 的 Cloudflare 域名。默认监听本机，不公开裸端口。

**每个 DATA_DIR 只运行一个调度/API 进程。** 调度器内部运行有界的常驻 OCR 子进程池；不要启动多个 uvicorn worker 争用同一数据库。生产应使用进程管理器重启服务，并限制日志大小。

## 并发与吞吐

默认 `AUTO_TUNE=true`，无需指定固定路数。每 5 秒结合容器 CPU 配额、进程亲和性、内存压力、其他程序的 CPU 使用和各阶段积压调整并发。识别有积压且算力空闲时增开常驻进程；内存紧张时减少活动识别并回收闲置模型。每进程 `OCR_THREADS=1`，避免 OpenCV、BLAS 和 ONNX 各自扩张线程池互相争抢。

下载、兼容校验、识别、回传分别使用独立线程池，SQLite 领取和管理通信也有独立执行器。只在有实际执行槽位时领取任务，不提前锁住第二条等待队列。下载以有限预取维持识别供给，识别积压时主动降下载并发；123 返回 429 时所有下载共享冷却窗口、遵守 `Retry-After` 并减半下载并发。

`DOWNLOAD_WORKERS / VERIFY_WORKERS / OCR_SLOTS / OCR_PROCESSES / DELIVER_WORKERS` 默认 0，表示按硬件推导上限；正值可设置人为上限。`DOWNLOAD_AHEAD=0` 自动推导预取组数，`WORK_MAX_BYTES=0` 自动设置原图暂存额度（最多 2 GiB 或可用磁盘的四分之一，至少容纳一组）。下载前按组预留实际文件字节，下载失败和识别完成后立即释放，磁盘保留至少 128 MiB。`AUTO_TUNE=false` 使用推导/配置的固定上限，`PIPELINE=inline` 仅用于兼容回退。

B 优先把原图流式写入有额度的 `/dev/shm` 内存暂存，子进程直接读取同一文件；额度默认不超过内存预算的 10%、512 MiB 和共享内存的 75%。内存不足、额度满或大组自动回退磁盘。`MEMORY_CACHE_BYTES=0` 自动，`-1` 关闭；Docker 示例设置 `shm_size: 512m`。缓存只存在于 B，SQLite 和结构化结果仍持久化，重启后重新下载缺失原图。`performance.memory_cached / memory_cache_limit` 显示额度占用。

下载时一次性流式核对大小和 SHA-256，完成后直接进入识别，不重复读盘哈希。识别进程缓存基础图标、已发布模板解码和 OCR 模型，每次任务仍使用独立解析状态。区服改变、超时、崩溃或处理满 `OCR_PROCESS_MAX_JOBS` 后重建。解析结果持久化后立即删除原图；C 查询版本和上传结果在回传池完成，C 故障不会占用识别槽，结果持续重试并复用已保存版本。

`/healthz` 的 `performance` 提供各阶段当前活动数、自动并发、硬件上限、CPU/内存压力、成功图片速率、平均耗时、429 冷却和暂存预留。速率按本进程启动后的成功处理数计算。每 30 秒打印相同的资源与队列摘要。不要给容器设置过低的 CPU/内存限制，否则自动调度只能使用该限制内的资源。

SQLite 原子领取任务；同一图片组的首次识别、重识别和回传互斥。重启保留批次、结果和版本；需要原图的中间任务重新下载，已保存结果只重传。停止时等待正在执行的任务保存结果。所有统计继续使用原始 `collected_at`。

## 存储与恢复

SQLite 以 WAL/FULL 保存批次、待办、结果及进度，事务提交后才确认 A。重复批次沿用原 ID；相同图片组不会再次建立初始识别任务。每个活动任务下载当前组所需文件到独立临时目录（每组最多 500 MiB），只逐张解码，不常驻整批原图。成功、失败或进程重启后均清理本地原图；失败重试重新下载。

`MAX_FILE_BYTES / MAX_GROUP_BYTES` 默认 10 MiB / 500 MiB；修改大小限制时三节点使用相同配置。

C 离线时保留结构化结果和小候选图，重传不重复 OCR。C 接收后清除本地候选图副本；SQLite 仍保留任务/结果历史，须备份并监控容量。123 盘是唯一长期原图存储。

123 盘下载走官方 [download_info](https://123yunpan.yuque.com/org-wiki-123yunpan-muaork/cr6ced/fnf60phsushn8ip2)；逐块落盘并校验大小和 SHA-256。确认账户开放平台下载额度能覆盖实际流量，额度/鉴权失败保留待办，不丢任务。

也可配置 `WEBDAV_URL`、`WEBDAV_USERNAME`、`WEBDAV_PASSWORD`，改用 WebDAV 下载，无需 B 的 OpenAPI 凭据。目录必须对应 A 的上传目录，例如 `https://webdav.123pan.cn/webdav`。B 直接请求 `<sha256>.png`，不扫描整个目录；继续逐块落盘、校验大小和 SHA-256，并在处理结束后清理本地原图。凭据单独保存在 `.env`，不放进 URL，跨域 CDN 重定向不携带 WebDAV 认证。

日志分别记录 `Original downloaded`、`OCR completed` 和 `Result delivered`，包含图片组哈希及下载/识别耗时。启动日志显示 OCR 进程数、每进程线程数及任务槽位；半小时内的全部任务会先持久化接收，后台分别显示原图张数和上报次数。

## 识别与模板

输入为 PNG，宽 1280、高为 720 的整数倍，每文件最多 10 帧。科研完成截图应包括队列项目卡和领奖帧；刷新截图场景用 `research_refresh`。通用掉落使用获得道具弹窗，`opsi_*`、`commission*` 场景选择对应基础模板。

未知物品自动裁剪 49×49 图标，附源文件、帧、槽位和坐标。候选需要在 C 后台人工命名分类；不会猜测其语义。缺失模板、项目或数量异常产生 `needs_review`，不进入正式统计。

旧客户端把填队列时的列表归档误标为领奖。如果没有任何领奖帧，且所有原图都已由同一实例单独提交为科研列表，则标为 `invalid / duplicate_research_list_capture`，避免重复计算出现次数。后台手动重识别优先于普通积压任务，同一来源仍串行，其他来源并行。

每 30 秒同步 C 心跳、模板和指令；发布包校验 SHA-256，任务固定使用开始时的版本。试识别使用草稿且不入统计。发布、回滚和手动重处理独立于 A 的半小时调度。`POST /internal/v1/preview` 仅供 C 认证调用，按需下载并返回一帧，响应后删除临时文件。

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
## 历史 API 和时段统计

A 的 `POST /api/upload[/]` 接收旧 AP 的 `file/device_id/genre/combat_count`，B 保留海域、侵蚀等级及 meow/log/scan 标签。同一组不同帧的相同大世界掉落累加；科研奖励页保持原去重规则。无 genre 的旧客户端截图尝试自动分类，无法确定时进入复核。

科研刷新读取五个项目和倾向期数，参照 AzurStats 的图像检查与多数期数规则。统计使用原始 `collected_at`，A 在 :00/:30 派发已完成上传的数据；重试、重识别不改写采集时间。C 的半小时时段查询不使用派发时间作为观察时间。
