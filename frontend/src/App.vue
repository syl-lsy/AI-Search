<script setup lang="ts">
/**
 * App 根组件 = 组合面（vue-best-practices）：全局布局骨架（侧栏 + 顶栏 + RouterView）+
 * 图标 sprite + Toast 出口。页面级 feature 与取数全部下沉 views/ 与 stores/。
 * 布局/视觉 = docs/design/tokens.css 布局骨架（.app/.sidebar/.main/.topbar）+ 01~04 设计稿；
 * 侧栏「最近」= threadListStore（§1 侧栏行：GET /threads?limit=10，进入时拉取）；
 * 顶栏按路由差异化：home=自用版 pill / answer=返回+页签+导出（禁用留位）/ 其余=简洁；
 * <1080px 侧栏收抽屉（tokens.css body.sb-open 机制）。
 */
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'

import AppIconSprite from '@/components/icons/AppIconSprite.vue'
import UiIcon from '@/components/icons/UiIcon.vue'
import ModelSelect from '@/components/search/ModelSelect.vue'
import ToastHost from '@/components/common/ToastHost.vue'
import { usePrefsStore } from '@/stores/prefs'
import { useThreadListStore } from '@/stores/threadList'

const route = useRoute()
const prefs = usePrefsStore()
const listStore = useThreadListStore()

const isAnswer = computed(() => route.name === 'answer')
const isHome = computed(() => route.name === 'home')
const sidebarOpen = ref(false)

watch(sidebarOpen, (open) => {
  // tokens.css 抽屉机制以 body.sb-open 为选择器（<1080px）
  document.body.classList.toggle('sb-open', open)
})

const recentThreads = computed(() => listStore.items.slice(0, 10))

/** 会话入口：优先最近一条线程，无则去历史页 */
const chatTarget = computed(() => {
  const first = listStore.items[0]
  return first ? `/thread/${first.thread_id}` : '/threads'
})

function setLang(locale: 'zh-CN' | 'en-US'): void {
  prefs.setLang(locale)
}

onMounted(() => {
  // §1 侧栏行：GET /threads?limit=10（最近）；失败静默（侧栏为辅助导航）
  listStore.fetchThreads({ limit: 10 }).catch(() => undefined)
})
</script>

<template>
  <div class="app" data-testid="app-shell">
    <AppIconSprite />

    <!-- ================= 侧栏 ================= -->
    <aside class="sidebar" data-testid="sidebar">
      <div class="sb-head">
        <RouterLink class="sb-logo" to="/" title="AI Search" data-testid="sidebar-logo">
          <svg class="icon-lg" aria-hidden="true"><use href="#i-logo" /></svg>
        </RouterLink>
        <div class="sb-head-actions">
          <button type="button" class="icon-btn" :title="$t('common.notifications')" disabled>
            <UiIcon name="i-bell" />
          </button>
          <button type="button" class="icon-btn" :title="$t('common.searchPlaceholder')" disabled>
            <UiIcon name="i-search" />
          </button>
          <button
            type="button"
            class="icon-btn"
            :title="$t('common.collapseSidebar')"
            data-testid="sidebar-toggle"
            @click="sidebarOpen = !sidebarOpen"
          >
            <UiIcon name="i-panel" />
          </button>
        </div>
      </div>
      <nav class="sb-nav" aria-label="主导航">
        <RouterLink class="nav-item" to="/" data-testid="nav-home" @click="sidebarOpen = false">
          <UiIcon name="i-plus" />{{ $t('nav.home') }}
        </RouterLink>
        <RouterLink class="nav-item" :to="chatTarget" data-testid="nav-chat" @click="sidebarOpen = false">
          <UiIcon name="i-chat" />{{ $t('nav.chat') }}
        </RouterLink>
        <RouterLink class="nav-item" to="/library" data-testid="nav-library" @click="sidebarOpen = false">
          <UiIcon name="i-book" />{{ $t('nav.library') }}
        </RouterLink>
        <RouterLink class="nav-item" to="/threads" data-testid="nav-threads" @click="sidebarOpen = false">
          <UiIcon name="i-clock" />{{ $t('nav.threads') }}
        </RouterLink>

        <div class="sb-section">{{ $t('nav.recent') }} <UiIcon name="i-chev-d" size="sm" /></div>
        <RouterLink
          v-for="t in recentThreads"
          :key="t.thread_id"
          class="sb-recent"
          :to="`/thread/${t.thread_id}`"
          :title="t.title"
          @click="sidebarOpen = false"
        >
          {{ t.title }}
        </RouterLink>
      </nav>
      <div class="sb-foot">
        <span class="sb-plan"><UiIcon name="i-zap" size="sm" />{{ $t('common.planLabel') }}</span>
        <div class="sb-user">
          <span class="avatar">{{ $t('common.userInitial') }}</span>
          <div>
            <div class="u-name">{{ $t('common.userName') }}</div>
            <div class="u-sub">{{ $t('common.planLabel') }}</div>
          </div>
        </div>
      </div>
    </aside>
    <div v-if="sidebarOpen" class="sb-backdrop" data-testid="sb-backdrop" @click="sidebarOpen = false"></div>

    <!-- ================= 主区 ================= -->
    <div class="main">
      <header class="topbar" data-testid="topbar">
        <div class="tb-left">
          <button type="button" class="icon-btn sb-fab" :title="$t('common.openSidebar')" @click="sidebarOpen = true">
            <UiIcon name="i-menu" />
          </button>
          <template v-if="isAnswer">
            <button type="button" class="icon-btn" :title="$t('common.back')" @click="$router.back()">
              <UiIcon name="i-back" />
            </button>
            <nav class="tabs">
              <a class="tab active" href="#" @click.prevent>
                <UiIcon name="i-logo" size="sm" />{{ $t('answer.tabAnswer') }}
              </a>
              <!-- 链接/图片页签：M2 范围留位（渲染不激活） -->
              <span class="tab disabled"><UiIcon name="i-link" size="sm" />{{ $t('answer.tabLinks') }}</span>
              <span class="tab disabled"><UiIcon name="i-image" size="sm" />{{ $t('answer.tabImages') }}</span>
            </nav>
          </template>
          <span v-else-if="isHome" class="top-pill">{{ $t('common.planFree') }}</span>
        </div>
        <div class="tb-right">
          <template v-if="isAnswer">
            <!-- 导出（REQ-016 深度报告专属，后端链路 M2 留位——按钮常驻、禁用，设计说明 §5.2 口径） -->
            <button type="button" class="btn btn-bordered btn-sm" :title="$t('answer.exportMd')" disabled>
              <UiIcon name="i-download" size="sm" />{{ $t('answer.exportMd') }}
            </button>
            <button type="button" class="btn btn-bordered btn-sm" :title="$t('answer.share')" disabled>
              <UiIcon name="i-share" size="sm" />{{ $t('answer.share') }}
            </button>
          </template>
          <div class="seg" :title="$t('common.langTitle')">
            <button
              type="button"
              :class="{ on: prefs.lang === 'zh-CN' }"
              data-testid="lang-zh"
              @click="setLang('zh-CN')"
            >中</button>
            <button
              type="button"
              :class="{ on: prefs.lang === 'en-US' }"
              data-testid="lang-en"
              @click="setLang('en-US')"
            >EN</button>
          </div>
          <ModelSelect />
        </div>
      </header>

      <main class="page-host">
        <RouterView />
      </main>
    </div>

    <ToastHost />
  </div>
</template>

<style scoped>
.app {
  display: flex;
  height: 100vh;
  overflow: hidden;
}
.page-host {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-width: 0;
  min-height: 0;
}
.top-pill {
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
  height: 32px;
  padding: 0 var(--space-3);
  border-radius: var(--radius-full);
  border: 1px solid var(--color-accent-border);
  color: var(--color-accent);
  font-size: var(--text-sm);
}
.tab.disabled {
  color: var(--color-text-faint);
  cursor: default;
}
</style>
