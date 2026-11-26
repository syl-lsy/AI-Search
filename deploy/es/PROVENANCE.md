# ES jieba 插件供应链留档（PROVENANCE）

> M3 工单 B18 基建批（2026-11-25）。本目录 `plugins/jieba/` 为**自构建产物入库**（约 7.3MB），
> compose 经 `deploy/es/Dockerfile` 薄镜像叠加装载。ADR-014 供应链纪律的过渡形态——
> 全源 vendor + CI 构建归 M3+ 供应链工单（ARCH R22/R23 候选线）。

## 上游与版本钉版

| 项 | 钉值 | 说明 |
|---|---|---|
| 插件源 | `github.com/sing1ee/elasticsearch-jieba-plugin` @ **8.4.1 分支**，commit `cbca4e7ebc5fe6f6b1e5e1804a6daf0da7039c7e`（浅克隆 HEAD，2022-12-01 "8.4.1的支持"） | 首选 infinilabs/analysis-jieba 已官方下架（探针 §2 三重证据：release 目录空 / org 无仓库 / zip 404），改用社区备选 |
| 分词内核子模块 | `github.com/sing1ee/jieba-analysis` @ commit `4c16d01ea549c8e11315f49c0922ac4f8a07accc` | master 分支（ES 8.11.1）因子模块指针丢失不可构建（探针 §3 波折 1），8.4.1 分支指针为作者实测组合 |
| 构建工具 | gradle **7.6** 发行版，mirrors.cloud.tencent.com，sha256 `7ba68c54029790ab444b39d7e293d3236b2632631fb5f2e012bb28b4ff669e4b`（与官方一致） | Docker Hub registry 本机不可达（TUN 代理面），gradle:7.6-jdk18 镜像放弃，改用 ES 镜像内置 JDK18 |
| 构建环境 | `docker.elastic.co/elasticsearch/elasticsearch:8.4.1` 内置 OpenJDK 18.0.2；依赖仓库走 build.gradle 自带 `maven.aliyun.com` | 配方固化于 `scripts/build_es_jieba_plugin.sh` |
| 产物 | 构建产物 `elasticsearch-jieba-plugin-1.0.0.zip`（4,987,047B）解包后入库 **7 件**（路径 `deploy/es/plugins/jieba/`）：`elasticsearch-jieba-plugin-1.0.0.jar` + `jieba-analysis-1.0.1-SNAPSHOT.jar` + `plugin-descriptor.properties` + `plugin.xml` + `dic/{sougou.dict,user.dict,stopwords.txt}`。zip 与解包临时目录已在构建批清理，**以本文件「产物清单」的入库件与 sha256 为准**（zip 内清单不可再重数） | descriptor：`version=1.0.0` / `java.version=18` / **`elasticsearch.version=8.4.1`**（与基底镜像硬互锁） |

## 许可证

- 插件本体：**MIT**（`LICENSE` 首行 MIT License / Copyright (c) 2017 Cheng Zhang；GitHub API license 字段一致）
- 分词内核 jieba-analysis（sing1ee fork，上游 huaban）：**Apache-2.0**（LICENSE + pom.xml `<license>` 双证）

双宽松许可，商用/闭源集成无障碍。

## 重建方法

```bash
bash scripts/build_es_jieba_plugin.sh
```

脚本幂等：临时目录克隆（子模块 URL SSH→HTTPS 改写）→ **源 commit 与子模块 PIN 硬校验
（`cbca4e7…` / `4c16d01…`，任一不符 FATAL exit 1，与 gradle zip sha256 同风格）** →
gradle 7.6 下载 + sha256 校验 →
一次性容器构建（`gradle clean pz`）→ 产物解包 + **逐字稳定件 sha256 硬校验**（三 dic +
`plugin-descriptor.properties`，见下节产物清单；不符 FATAL exit 1）→ **清空后**覆盖
`deploy/es/plugins/jieba/`（防上游删件后旧文件残留）→
`docker compose --project-directory <repo 根> -f <repo 根>/deploy/docker-compose.yml build elasticsearch`
重建薄镜像。

## 产物清单（2026-11-26 主会话收尾批补录）

`shasum -a 256` 于入库产物（2026-11-25 构建批次）实测值；路径相对 `deploy/es/plugins/jieba/`：

| 文件 | sha256 |
|---|---|
| `elasticsearch-jieba-plugin-1.0.0.jar` | `82a0c479d38aa142c728b2469661c059ae78238391dfbbb6b583c5ba9b4c739c` |
| `jieba-analysis-1.0.1-SNAPSHOT.jar` | `2855df68d28e4b9e3299e0a43fc9f1f3d0ae51926297f0fc25b2bcc40307ab1a` |
| `plugin-descriptor.properties` | `2b02af135208f1a0757001271e9eb8ce179dad8e2aa70e898184b509bf2e794a` |
| `plugin.xml` | `76dea438f7423c6eee630dce73f6d8842f290e40749cad2e6fe3b700711148ed` |
| `dic/sougou.dict` | `33ea8a53f1e4d4b46b9ba948cbb4e0a6aff0655c27e88c1588c9a03c76b820b4` |
| `dic/user.dict` | `30a6be2bbdd2d5638b980feec401f36ed86b4378de84ab489233048bd26170f9` |
| `dic/stopwords.txt` | `c131d3ecc65ec900e71e04ebd8c6f885ea0f5d3bba466c6ec43451a2bbe6cf82` |

用途：例行重建核对产物是否被静默换装（上游 commit 硬校验只挡源码侧漂移）。
**执行形态分两级**（`scripts/build_es_jieba_plugin.sh` 内固化）：
- **硬校验（FATAL exit 1）**：`dic/sougou.dict`、`dic/user.dict`、`dic/stopwords.txt`、
  `plugin-descriptor.properties` 四件逐字稳定（纯文本/词库/descriptor，重建不改写）——
  sha256 已钉进构建脚本常量，不符即拒入库；
- **仅回显（人工核对）**：两 jar 含 gradle 时间戳，非位级可复现，重建后 sha256 与上表
  不同属预期。`plugin.xml` 随 jar 打包结构变化可能变动，列此备查、不设硬锁。

换词库/改 descriptor 须先审并同步改写脚本常量与本表（不得只改一边）。

## 风险如实留档（对应设计 §12 / ARCH R22/R23）

1. sing1ee 为单人维护项目（538 stars，bus factor=1）——升级/断供风险自担，vendor 化归 M3+；
2. ES 版本锁死 8.4.1（descriptor 硬校验 + master 分支不可构建）——server 升版 = client+插件+重构建三联动；
3. `sougou.dict`（3.68MB 搜狗词库转换）随包分发，词库质量归 M4 评测线；
4. 本文件与构建脚本为过渡性供应链留痕，不构成可复现构建的形式化证明（源仓库/tag 侧变动不在此监控内）。
