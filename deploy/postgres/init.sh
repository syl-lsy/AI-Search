#!/usr/bin/env bash
# =============================================================================
# AI Search postgres 首次初始化（compose bind → /docker-entrypoint-initdb.d/init.sh）
#
# 语义：official postgres 镜像 entrypoint 仅在数据目录为空的【首次启动】执行本脚本。
# 职责（docs/ARCHITECTURE.md §5 postgres 行 / docs/DEVELOPMENT-PROCESS.md §3.1 第 1 项）：
#   1. 建三角色：ai_search / langfuse / litellm（LOGIN，与库同名）
#   2. 建三库：  ai_search / langfuse / litellm（owner = 同名角色）
#   3. ai_search 库装 pgvector：CREATE EXTENSION vector
# 密码纪律：一律取容器 env（.env 经 compose 注入 postgres 容器——见 deploy/docker-compose.yml
#   postgres.environment），【禁止写死任何密码】；密码经 psql :'var' 引用传参，
#   由 psql 负责安全引用转义（不怕特殊字符），脚本与日志均不回显密码值。
# 变量映射（容器 env ← .env）：
#   DB_PASSWORD          → 角色/库 ai_search（同时是超级用户引导密码 POSTGRES_PASSWORD，compose 注入）
#   LANGFUSE_DB_PASSWORD → 角色/库 langfuse（compose DATABASE_URL 引用）
#   LITELLM_DB_PASSWORD  → 角色/库 litellm（compose DATABASE_URL 引用）
# =============================================================================

set -euo pipefail

# 缺变量即失败（initdb 阶段失败 = 容器起不来，显式报错优于静默错配）
: "${DB_PASSWORD:?DB_PASSWORD not set in container env (from .env via compose)}"
: "${LANGFUSE_DB_PASSWORD:?LANGFUSE_DB_PASSWORD not set in container env (from .env via compose)}"
: "${LITELLM_DB_PASSWORD:?LITELLM_DB_PASSWORD not set in container env (from .env via compose)}"

POSTGRES_USER="${POSTGRES_USER:-postgres}"
POSTGRES_DB="${POSTGRES_DB:-$POSTGRES_USER}"

run_sql() { psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" "$@"; }

# ── 1) 三角色（密码经 psql 变量 :'var' 传入，不拼进 SQL 文本、不回显）──
run_sql -v pw_ai="$DB_PASSWORD" -v pw_lf="$LANGFUSE_DB_PASSWORD" -v pw_lt="$LITELLM_DB_PASSWORD" <<-'EOSQL'
  CREATE ROLE ai_search WITH LOGIN PASSWORD :'pw_ai';
  CREATE ROLE langfuse  WITH LOGIN PASSWORD :'pw_lf';
  CREATE ROLE litellm   WITH LOGIN PASSWORD :'pw_lt';
EOSQL

# ── 2) 三库（owner = 同名角色；应用 DSN / compose DATABASE_URL 按此拼装）──
run_sql -c 'CREATE DATABASE ai_search OWNER ai_search;'
run_sql -c 'CREATE DATABASE langfuse  OWNER langfuse;'
run_sql -c 'CREATE DATABASE litellm   OWNER litellm;'

# ── 3) pgvector（仅 ai_search 库；langfuse/litellm 库无向量需求）──
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname ai_search \
  -c 'CREATE EXTENSION IF NOT EXISTS vector;'

# 首次初始化完成提示（不含任何凭据值）
echo "ai_search postgres init done: databases=ai_search,langfuse,litellm; roles=ai_search,langfuse,litellm; ext=vector(ai_search)"
