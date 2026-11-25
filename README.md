# 无线具身智能实验平台

同一个 Git 仓库部署 OAI gNB、UE、5G 核心网、OpenPI π₀.₅-LIBERO 和 LIBERO/MuJoCo。OAI、ASN.1 编译器、SIMDe、OpenPI、LIBERO、LeRobot 通过 [Git submodule](.gitmodules) 固定提交。部署器初始化所需 submodule、编译程序、安装依赖并启动服务。

## 单机首次部署

目标是带 NVIDIA GPU 的 **Ubuntu 22.04 x86_64** 主机。需要具有 sudo 权限的账号，以及访问系统软件源、GitHub、容器镜像仓库、PyPI、PyTorch 和 OpenPI 权重存储的网络。主机应支持 AVX2、Linux TUN 和 SCTP，磁盘需容纳模型缓存、容器镜像和编译产物。权重缓存约 12 GB，建议为首次构建预留至少 50 GB。已用 L40S 验证推理。

在主机 clone 此仓库后，进入仓库目录，用普通实验账号运行：

```bash
./bin/lab deploy --smoke
```

默认使用第一块 NVIDIA GPU。首次部署可以指定 `--gpu 1` 或 GPU UUID，并用 `--jobs 8` 调整编译并行度。

首次部署会完成以下步骤。

1. 准备主机依赖、Docker Engine、Docker Compose 插件 和 NVIDIA Container Toolkit。若 NVIDIA 驱动需要重启才能加载，按终端提示重启后重跑同一条命令。
2. 拉取固定源码，在本机生成 `.state/ran/build/nr-softmodem`（gNB）和 `.state/ran/build/nr-uesoftmodem`（UE），并构建 VRTSIM 运行库。
3. 安装 Python 3.10 仿真环境、MuJoCo、robosuite 和 OpenPI 客户端，载入 LIBERO 场景并验证离屏渲染。
4. 启动 OAI 核心网和 GPU 推理容器。π₀.₅ 权重由 OpenPI 首次启动时下载到 `.state/edge/model-cache`。
5. 启动 gNB 和 UE，经 `oaitun_ue1` 检查推理服务。加 `--smoke` 时继续运行一次放碗回合，并将结果、录像和分段计时写入 `runs/`。

单机模式下，gNB 的 N2/N3 地址是核心网 Docker bridge 的网关，UE 和仿真共用 UE 网络命名空间。gNB 与 UE 通过本机 VRTSIM Unix socket 交换 IQ 数据。首次运行自动生成 `local/site.json`，保存联动的 UE 订阅、容器网段和 GPU 选择。设置更改后用同一站点文件重新部署。单机默认使用 OSMesa 做无头渲染，需要 EGL 时可改站点的 `mujoco_gl`。

若现有网络占用了默认网段或端口，先初始化站点，再执行部署。

```bash
./bin/lab init --topology single --bridge-subnet 172.30.218.0/26 --policy-host-port 18825 --gpu 0
./bin/lab plan
./bin/lab deploy --smoke
```

如果部署主机需要通过内部镜像站访问 Docker Hub，可运行 `VLA_DOCKER_MIRROR=https://your-mirror.example ./bin/lab deploy --smoke`。部署器会保留其他 Docker 配置，镜像仍按原摘要匹配。 [Docker Hub 镜像源配置](https://docs.docker.com/docker-hub/image-library/mirror/)

## 单独准备或启动组件

| 目的 | 操作 |
| --- | --- |
| 只准备 gNB 和 UE 程序 | `./bin/lab deps ran`，再运行 `./bin/lab prepare ran` |
| 只启动 gNB | 核心网就绪后运行 `sudo ./bin/lab network ran`，再运行 `sudo ./bin/lab radio gnb` |
| 在第二个终端启动 UE | `sudo ./bin/lab radio ue` |
| 部署核心网 | `./bin/lab deploy core` |
| 在核心网 bridge 上部署边缘服务 | `./bin/lab deploy edge` |
| 准备并检查仿真 | `./bin/lab deploy sim` |
| 源码已准备且希望前台运行 gNB 与 UE | `sudo ./bin/lab radio both` |
| 运行单次回合 | `./bin/lab sim run bowl`、`./bin/lab sim run two-objects` 或 `./bin/lab sim run drawer` |
| 查看部署状态 | `./bin/lab status ran`、`./bin/lab status core`、`./bin/lab status edge` |

`deploy` 用 systemd 的瞬态服务管理 gNB 和 UE。切换到前台启动前，用 `./bin/lab down ran` 停止它。手动前台启动时用 Ctrl+C 停止。完整停止顺序是 `down ran`、`down edge`、`down core`，数据库卷和权重缓存会保留。重启后运行同一条部署命令恢复 RAN。只重新启动无线侧可运行 `./bin/lab deploy ran`。

如果希望先取源码，可以运行 `git submodule update --init --depth 1`。顶层仓库的 Gitlink 决定精确版本。需要 OAI 源码时查看 `vendor/oai/`，需要可执行程序时使用编译输出目录。MuJoCo 随锁定的 Python wheel 安装。核心网使用固定镜像摘要，推理服务使用 [Python 依赖锁](edge/requirements.lock)，仿真使用 [仿真依赖锁](sim/requirements.lock)。

## 双机部署

保持原实验拓扑。Linux A 运行 gNB、UE 和 LIBERO/MuJoCo，Linux B 运行核心网和 GPU 推理服务。两台服务器需要互通 IPv4，并允许 IPIP（IP 协议号 4）。核心网侧使用 Docker 的 iptables 后端。

先生成一份双机站点文件，使用真实的互通地址，复制 `local/site.json` 到两台服务器的 checkout 下。

```bash
./bin/lab init --topology dual --ran-ip 192.0.2.10 --edge-ip 192.0.2.20 --gpu 0
./bin/lab plan
```

### Linux B · 核心网与边缘

```bash
./bin/lab deploy core
./bin/lab deploy edge
```

### Linux A · 无线与仿真

```bash
./bin/lab deploy sim
./bin/lab deploy ran
./bin/lab sim probe
./bin/lab sim run bowl
```

双机的 `deploy core` 和 `deploy ran` 会配置各自的 N2/N3 隧道。如果已经用旧版创建过站点文件，带双机地址的旧配置可继续使用。只替换 RAN 时保留原核心网订阅所对应的 IMSI、K 和 OPc。可用 `./bin/lab --site /path/to/site.json ...` 选择其他站点。双机拓扑和原数据面实验使用一致，核心网部署覆盖数据业务和压测。

## 验收、实验和缓存

- `sim probe` 先验证策略端点路由确实走 UE TUN，再访问 `/healthz`。加 `--throughput` 测 ext-dn 的上下行吞吐。可用 `sim run bowl --host 127.0.0.1 --port 8000` 执行本机策略直连对照。
- `sim run bowl --init-state 1 --seed 7` 设置基准初始状态和种子。默认每轮采用状态 0、种子 7，并执行模型动作序列的前 5 步。客户端等待推理期间仿真步进暂停，墙钟时间和仿真时间应分开统计。
- 前台 gNB 支持 `--profile full`、`--profile ul32` 和 `--profile dl4`。`full` 对应 106 PRB，`ul32` 保留连续上行 PRB 0–31 与上侧 PRB 98–105，`dl4` 将下行 MCS 上限设为 4。
- 支持保留和复用已有 OpenPI 缓存。给 `deploy edge` 设置 `VLA_MODEL_CACHE` 指向已有权重，或用 `VLA_POLICY_IMAGE` 选择本机兼容镜像。镜像需在容器内监听 8000 并提供 `/healthz`。`VLA_POLICY_WHEELHOUSE`、`POLICY_PIP_INDEX`、`POLICY_WHEEL_ORIGIN` 可用于已有 wheel 缓存或镜像源。锁内文件仍按 SHA-256 校验。
- 可用 `VLA_SOURCE_ARCHIVES` 复用 OpenPI、LIBERO、LeRobot 的 GitHub 源码归档缓存，命名为 `<组件>-<commit>.tar.gz`；其余组件仍走 Git submodule。默认尝试 submodule。OpenPI、LIBERO 或 LeRobot 的 Git 下载失败时，部署器自动从 GitHub 下载并核对同一提交的源码归档。
- `local/`、`.state/`、`runs/` 和抓包由 Git 忽略。若已存在其他核心网实例，使用独立的站点名、Docker 网段、bridge 名和回环端口。单台主机同时运行一套 VRTSIM radio。

## 参考与许可证

系统依赖安装采用官方软件源。 [Docker Engine](https://docs.docker.com/engine/install/ubuntu/) · [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html) · [Ubuntu NVIDIA 驱动](https://ubuntu.com/server/docs/how-to/graphics/install-nvidia-drivers/)

OAI 派生的配置和数据库模板使用 [OAI CSSL](licenses/OAI-CSSL)。单任务 LIBERO 客户端基于 Apache-2.0 OpenPI 示例，见 [OpenPI LICENSE](licenses/OpenPI-LICENSE)。上游代码、镜像和模型遵循各发布方的许可证。

## 验证记录

2026-11-25 的单机验证从 Git clone 初始化了 OAI、SIMDe、ASN.1 编译器，并从源码生成 gNB 和 UE。在隔离的 Ubuntu 环境中完成 PDU 会话建立，经 `oaitun_ue1` 使用已有 NVIDIA 驱动、固定核心网镜像和 π₀.₅ 权重缓存完成放碗回合。回合成功，生成了 MP4 和 18 条推理请求记录。

另一个空白 Ubuntu 虚拟机验证了 Docker Engine 与 Compose 插件的自动安装和 daemon 启动。测试网络到 Docker Hub 的连接被重置，冷拉镜像可使用具备访问条件的网络或镜像站。空白机上的 NVIDIA 驱动安装流程仍需在具有物理 GPU 的机器上验证。
