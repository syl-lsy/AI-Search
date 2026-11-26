#!/usr/bin/env bash
# jieba 分词插件构建配方固化（M3 B18 基建批，设计 plan-m3-b18-es-write-20261125.md §2 插件交付四件之一）
#
# 来源：探针 docs/research/m3-probe-es-jieba-20261122.md §3 实测配方（2026-11-25 基建批按此重建并入库）。
# 用法（仓库根跑）：bash scripts/build_es_jieba_plugin.sh
#
# 步骤：临时目录克隆 sing1ee/elasticsearch-jieba-plugin 8.4.1 分支（子模块 SSH→HTTPS 改写）
#   → **源 commit / 子模块 PIN 硬校验（不等即 FATAL exit 1——上游漂移/投毒拒绝构建）**
#   → gradle 7.6 下载（腾讯云镜像）+ sha256 校验 → ES 8.4.1 镜像内置 JDK18 一次性容器构建
#   → 产物解包 + **逐字稳定件（三 dic + plugin-descriptor.properties）sha256 硬校验**
#   → 清空后覆盖 deploy/es/plugins/jieba/ → docker compose build elasticsearch 重建薄镜像。
# 供应链细节见 deploy/es/PROVENANCE.md。
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK_DIR="${B18_BUILD_DIR:-/tmp/b18_es_build}"
REPO_URL="https://github.com/sing1ee/elasticsearch-jieba-plugin.git"
SUBMODULE_URL="https://github.com/sing1ee/jieba-analysis.git"
BRANCH="8.4.1"
# 源 commit 钉版（deploy/es/PROVENANCE.md「上游与版本钉版」表；浅克隆 HEAD 必须逐字相等）
PLUGIN_COMMIT="cbca4e7ebc5fe6f6b1e5e1804a6daf0da7039c7e"
# 分词内核子模块钉版（PROVENANCE 同表；master 分支不可构建，8.4.1 分支指针为作者实测组合）
SUBMODULE_PIN="4c16d01ea549c8e11315f49c0922ac4f8a07accc"
GRADLE_ZIP_URL="https://mirrors.cloud.tencent.com/gradle/gradle-7.6-bin.zip"
GRADLE_SHA256="7ba68c54029790ab444b39d7e293d3236b2632631fb5f2e012bb28b4ff669e4b"
ES_IMAGE="docker.elastic.co/elasticsearch/elasticsearch:8.4.1"
OUT_DIR="${REPO_ROOT}/deploy/es/plugins/jieba"

mkdir -p "${WORK_DIR}"

echo "==> [1/5] clone ${REPO_URL} @ ${BRANCH} + 供应链钉版校验"
rm -rf "${WORK_DIR}/repo"
git clone --depth 1 --branch "${BRANCH}" "${REPO_URL}" "${WORK_DIR}/repo"
git -C "${WORK_DIR}/repo" submodule set-url jieba-analysis "${SUBMODULE_URL}"
git -C "${WORK_DIR}/repo" submodule update --init --recursive
HEAD_SHA="$(git -C "${WORK_DIR}/repo" rev-parse HEAD)"
if [ "${HEAD_SHA}" != "${PLUGIN_COMMIT}" ]; then
    echo "FATAL: 插件源 commit 漂移: 实际 ${HEAD_SHA} != 钉版 ${PLUGIN_COMMIT}" >&2
    echo "       （deploy/es/PROVENANCE.md 钉版；分支 HEAD 被上游改写/投毒时拒绝构建）" >&2
    exit 1
fi
echo "    源 commit OK: ${HEAD_SHA}"
PIN="$(git -C "${WORK_DIR}/repo/jieba-analysis" rev-parse HEAD)"
if [ "${PIN}" != "${SUBMODULE_PIN}" ]; then
    echo "FATAL: 子模块 jieba-analysis commit 漂移: 实际 ${PIN} != 钉版 ${SUBMODULE_PIN}" >&2
    echo "       （deploy/es/PROVENANCE.md 钉版；子模块指针变动时拒绝构建）" >&2
    exit 1
fi
echo "    子模块 PIN OK: jieba-analysis @ ${PIN}"

echo "==> [2/5] gradle 7.6 下载 + sha256 校验"
if [ ! -f "${WORK_DIR}/gradle-7.6-bin.zip" ] \
   || [ "$(shasum -a 256 "${WORK_DIR}/gradle-7.6-bin.zip" | awk '{print $1}')" != "${GRADLE_SHA256}" ]; then
    curl -sL -o "${WORK_DIR}/gradle-7.6-bin.zip" "${GRADLE_ZIP_URL}"
fi
ACTUAL_SHA="$(shasum -a 256 "${WORK_DIR}/gradle-7.6-bin.zip" | awk '{print $1}')"
if [ "${ACTUAL_SHA}" != "${GRADLE_SHA256}" ]; then
    echo "FATAL: gradle zip sha256 mismatch: ${ACTUAL_SHA} != ${GRADLE_SHA256}" >&2
    exit 1
fi
echo "    sha256 OK: ${ACTUAL_SHA}"

echo "==> [3/5] 一次性容器构建（${ES_IMAGE} 内置 JDK18，gradle clean pz）"
docker run --rm --name jieba-build-b18 \
    -v "${WORK_DIR}/repo:/src" \
    -v "${WORK_DIR}/gradle-7.6-bin.zip:/tmp/gradle-7.6-bin.zip:ro" \
    --entrypoint /bin/bash "${ES_IMAGE}" \
    -c "set -e; export JAVA_HOME=/usr/share/elasticsearch/jdk; export PATH=\$JAVA_HOME/bin:/tmp/gradle-7.6/bin:\$PATH; \
        cd /tmp && jar xf /tmp/gradle-7.6-bin.zip && chmod +x /tmp/gradle-7.6/bin/gradle && \
        cd /src && gradle clean pz --no-daemon && echo BUILD-DONE && ls -la build/distributions/"

echo "==> [4/5] 产物解包 + 逐字稳定件 sha256 硬校验 → 覆盖 ${OUT_DIR}"
rm -rf "${WORK_DIR}/extract" && mkdir -p "${WORK_DIR}/extract"
unzip -o -q "${WORK_DIR}/repo/build/distributions/elasticsearch-jieba-plugin-1.0.0.zip" -d "${WORK_DIR}/extract"
EXTRACT_DIR="${WORK_DIR}/extract/jieba"
# 逐字稳定件（纯文本/词库/descriptor——重建不改写）：sha256 必须与钉版逐字相等，
# 不符即 FATAL（上游换词库/改 descriptor 不许静默入库）。jar 含 gradle 时间戳、
# 非位级可复现，不设硬校验，仅回显供人工核对（PROVENANCE「产物清单」）。
verify_pinned() {  # $1=相对 EXTRACT_DIR 路径  $2=钉版 sha256
    local rel="$1" expected="$2" actual
    if [ ! -f "${EXTRACT_DIR}/${rel}" ]; then
        echo "FATAL: 构建产物缺件: ${rel}（上游打包结构变更？）" >&2
        exit 1
    fi
    actual="$(shasum -a 256 "${EXTRACT_DIR}/${rel}" | awk '{print $1}')"
    if [ "${actual}" != "${expected}" ]; then
        echo "FATAL: 产物 sha256 不符: ${rel}" >&2
        echo "       实际 ${actual}" >&2
        echo "       钉版 ${expected}（deploy/es/PROVENANCE.md 产物清单；换词库/改 descriptor 须先审）" >&2
        exit 1
    fi
    echo "    sha256 OK: ${rel}"
}
verify_pinned "plugin-descriptor.properties" "2b02af135208f1a0757001271e9eb8ce179dad8e2aa70e898184b509bf2e794a"
verify_pinned "dic/sougou.dict" "33ea8a53f1e4d4b46b9ba948cbb4e0a6aff0655c27e88c1588c9a03c76b820b4"
verify_pinned "dic/user.dict" "30a6be2bbdd2d5638b980feec401f36ed86b4378de84ab489233048bd26170f9"
verify_pinned "dic/stopwords.txt" "c131d3ecc65ec900e71e04ebd8c6f885ea0f5d3bba466c6ec43451a2bbe6cf82"
# 先清空再覆盖：上游删件（如某 dic 退役）后旧文件不得残留入库
rm -rf "${OUT_DIR}" && mkdir -p "${OUT_DIR}"
cp -R "${EXTRACT_DIR}/." "${OUT_DIR}/"
echo "    入库文件："
find "${OUT_DIR}" -type f -exec ls -la {} \;
echo "    产物 sha256（jar 未硬校验，对照 PROVENANCE 产物清单人工核对）："
( cd "${OUT_DIR}" && find . -type f | sort | while read -r f; do
    printf '      %s  %s\n' "$(shasum -a 256 "${f}" | awk '{print $1}')" "${f#./}"
done )

echo "==> [5/5] 重建薄镜像（compose --project-directory=REPO_ROOT）"
docker compose --project-directory "${REPO_ROOT}" \
    -f "${REPO_ROOT}/deploy/docker-compose.yml" build elasticsearch

echo "DONE：插件已入库并重建 ai-search/elasticsearch-jieba 镜像；起栈/重建服务："
echo "  docker compose --project-directory ${REPO_ROOT} -f ${REPO_ROOT}/deploy/docker-compose.yml up -d --force-recreate elasticsearch"
