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

`RECOGNITION_WORKERS=0` 根据 CPU 配额与内存自动选择 OCR 进程数；也可显式设置 1–32。每进程默认 `OCR_THREADS=1`，避免多个模型线程池争抢核心。`PREFETCH_JOBS=0` 在 OCR 进程数之外预取半数任务（至少一个），使下载、识别和结果回传重叠；工作任务数始终有上限，原图流式落盘。健康检查返回实际 `recognition_workers / job_slots`。

识别进程保留 OCR 模型、模块和有界基础图标缓存，每份任务仍创建独立解析状态并使用固定模板快照。区服改变、超时、崩溃或处理满 `OCR_PROCESS_MAX_JOBS`（默认 1000）后重建进程。模板发布和回滚不要求重启模型，不会继承上一任务的在线模板或原图。

SQLite 原子领取任务；同一图片组的初次识别、重识别和结果回传互斥，其他图片组并行。停止服务时等待已领取任务保存结果，重启后恢复未完成任务。多路下载会预留正在写入的磁盘空间，保留至少 128 MiB 空闲空间。

## 存储与恢复

SQLite 以 WAL/FULL 保存批次、待办、结果及进度，事务提交后才确认 A。重复批次沿用原 ID；相同图片组不会再次建立初始识别任务。每个活动任务下载当前组所需文件到独立临时目录（每组最多 500 MiB），只逐张解码，不常驻整批原图。成功、失败或进程重启后均清理本地原图；失败重试重新下载。

`MAX_FILE_BYTES / MAX_GROUP_BYTES` 默认 10 MiB / 500 MiB；修改大小限制时三节点使用相同配置。

C 离线时保留结构化结果和小候选图，重传不重复 OCR。C 接收后清除本地候选图副本；SQLite 仍保留任务/结果历史，须备份并监控容量。123 盘是唯一长期原图存储。

123 盘下载走官方 [download_info](https://123yunpan.yuque.com/org-wiki-123yunpan-muaork/cr6ced/fnf60phsushn8ip2)；逐块落盘并校验大小和 SHA-256。确认账户开放平台下载额度能覆盖实际流量，额度/鉴权失败保留待办，不丢任务。

也可配置 `WEBDAV_URL`、`WEBDAV_USERNAME`、`WEBDAV_PASSWORD`，改用 WebDAV 下载，无需 B 的 OpenAPI 凭据。目录必须对应 A 的上传目录，例如 `https://webdav.123pan.cn/webdav`。B 直接请求 `<sha256>.png`，不扫描整个目录；继续逐块落盘、校验大小和 SHA-256，并在处理结束后清理本地原图。凭据单独保存在 `.env`，不放进 URL，跨域 CDN 重定向不携带 WebDAV 认证。

日志分别记录 `Original downloaded`、`OCR completed` 和 `Result delivered`，包含图片组哈希及下载/识别耗时。启动日志显示 OCR 进程数、每进程线程数及任务槽位；半小时内的全部任务会先持久化接收，后台分别显示原图张数和上报次数。

## 识别与模板

输入为 PNG，宽 1280、高为 720 的整数倍，每文件最多 10 帧。科研完成截图应包括队列项目卡和领奖帧；刷新截图场景用 `research_refresh`。通用掉落使用获得道具弹窗，`opsi_*`、`commission*` 场景选择对应基础模板。

混合批次中的每个 `items[]` 是独立图片组，B 按组建立任务，并根据该组的 `project / scene` 选择解析器。委托只在本组内按 `files[].index` 及文件内从上到下的帧序配对，不跨组、跨设备或跨场景补图；`collected_at` 是组级采集时间，不用于重排组内截图。客户端按实际截图顺序追加图片并分配索引；返回委托列表后可直接结束提交，无需额外上传退出页。

委托场景按文件索引及文件内帧序处理，用成功／大成功页开启一条记录，读取名称区域，再关联下一张收获页；返回委托列表时结束。只接受一张成功页与一张收获页的配对，缺收获页或出现多张收获页时整条委托无效。损坏图片、名称失败、未知物品或数量失败只跳过对应委托，后续有效委托仍上报。成功事件的 `project` 保存规范化中文名称。名称表含 107 个中文名称，另有 94 个名称的英／日／繁中别名；不在名称表中的委托跳过，新增名称需更新 `worker/commission_names.json`。

每组识别结果沿用 `POST /internal/v1/statistics/results` 的 `{"results": [result]}` 结构，保留组哈希、原始 `source`、结果版本和模板版本。`source.metadata.project` 保持 `drop`、`scene` 保持委托来源，`collected_at` 保持原采集时间；每次完整领取输出一条 `event_type=drop` 的事件，`events[].project` 为规范化委托名称，`items` 保存该次领取的物品 ID、数量和标签，`series / focus_series / duration_seconds` 均为 0。同组多次领取输出多个事件，C 可按名称分别统计。`skipped_frames` 仅写 B 日志，不进入 C 请求。重传复用已持久化的结果及版本，不重新 OCR。

同一图片组内，成功状态、规范化名称以及全部原始物品条目的名称、标签、数量都相同的完整记录只保留一次；物品排列顺序不影响去重，任意字段不同则分别保留。去重使用合并前的原始条目，因此 `[Coins:1, Coins:2]` 与 `[Coins:3]` 是不同记录；上报 C 时才按物品与标签合并数量。去重集合不会跨识别任务复用，整组重传仍使用已有哈希与版本幂等机制。先完成配对校验再去重，无效记录不占用去重键。每组最多输出 1,000 条去重后的有效事件，超出时保留已接受的事件，余下记录跳过并写入 `commission_event_limit` 日志，不清空已识别的结果。

本版按显示名称统计，不做基于奖励区间的同名版本推断，`duration_seconds` 保持 0，只处理成功页对应单张收获页的流程。部分成功的结果只包含完整事件，仍返回 `complete` 以保留 C 的正式统计；失败原因记录在 B 的 `Commission frames skipped` 日志，不混入 C 的成功结果诊断字段。没有有效事件时，纯配对错误返回 `invalid`；存在未分类画面、名称、数量或图像识别失败时返回 `needs_review`。状态判断独立于有界跳过日志，不因日志达到上限而遗漏识别失败。此口径的分母只包含完整识别并去重后的委托，可能受识别成功率影响。

按当前“失败委托跳过”的口径，委托分支暂不上报这些失败记录的未知物品候选图；其他场景仍使用原有候选模板闭环。名称区域只执行一次整行 OCR，罗马数字在同一结果中规范化，不再单独识别或补全后缀。名称不完整或多语言别名存在歧义时跳过，不推断委托版本。

委托页使用固定客户端自带的 `COMMISSION_DONE / COMMISSION_PERFECT / COMMISSION_NAME` 资源和共享 OCR 接口，无需另装专用模型。中文名称来自本地 `collection_template.json` 配置快照；多语言别名参考固定版本的 [AzurStats 委托数据](https://github.com/LmeSzinc/azur-stats-server/blob/3a743f091b0a98d42a8dc04c3d16be9552a9c7d6/AzurStats/commission/data.py)，来源与快照摘要保存在名称表中。已有名称裁剪和数量样本可验证局部识别，完整真实序列的准确率仍需补充样本验收。

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
