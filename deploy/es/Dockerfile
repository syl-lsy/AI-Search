# Elasticsearch + jieba 中文分词插件（M3 B18 基建，设计 plan-m3-b18-es-write-20261125.md §2）
#
# 自建薄镜像叠加：基底锁 tag docker.elastic.co/elasticsearch/elasticsearch:8.4.1 + 插件产物
# 目录直拷（手动解压安装形态——插件 zip 非标准结构，`elasticsearch-plugin install` 不适用，
# 探针 m3-probe-es-jieba-20261122.md §3 波折 3 实证）。
# 供应链与构建配方见 PROVENANCE.md 与 scripts/build_es_jieba_plugin.sh。
#
# ⚠️ 版本互锁：插件 plugin-descriptor.properties 硬校验 elasticsearch.version=8.4.1——
#    基底升版必须重构建插件（改动 build.gradle 2 行 + descriptor 1 行后跑构建脚本，R23）。
FROM docker.elastic.co/elasticsearch/elasticsearch:8.4.1

USER root
COPY plugins/jieba/ /usr/share/elasticsearch/plugins/jieba/
# 属主钉 uid=1000(elasticsearch):gid=0（官方 ES 镜像插件目录约定；探针实测镜像默认 uid=1000）
RUN chown -R 1000:0 /usr/share/elasticsearch/plugins/jieba \
    && chmod -R u+rwX,go+rX /usr/share/elasticsearch/plugins/jieba
USER elasticsearch
