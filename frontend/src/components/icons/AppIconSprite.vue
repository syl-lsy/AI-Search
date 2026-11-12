<script setup lang="ts">
// AppIconSprite — 全站图标 symbol 库（docs/design/01~04 HTML 内联 defs 的并集，
// 路径原文复制；tokens.css .stroke 基类承担描边）。在 App.vue 挂载一次即可全站 <use> 引用。
</script>

<template>
  <svg width="0" height="0" style="position: absolute" aria-hidden="true" focusable="false">
    <defs>
      <symbol id="i-logo" viewBox="0 0 24 24"><path class="stroke" d="M12 2.5v19M2.5 12h19M5.3 5.3l13.4 13.4M18.7 5.3L5.3 18.7" /></symbol>
      <symbol id="i-plus" viewBox="0 0 24 24"><path class="stroke" d="M12 5v14M5 12h14" /></symbol>
      <symbol id="i-chat" viewBox="0 0 24 24"><path class="stroke" d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z" /></symbol>
      <symbol id="i-book" viewBox="0 0 24 24"><path class="stroke" d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z" /></symbol>
      <symbol id="i-clock" viewBox="0 0 24 24"><circle class="stroke" cx="12" cy="12" r="9" /><path class="stroke" d="M12 7v5l3 3" /></symbol>
      <symbol id="i-bell" viewBox="0 0 24 24"><path class="stroke" d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9" /><path class="stroke" d="M13.7 21a2 2 0 0 1-3.4 0" /></symbol>
      <symbol id="i-search" viewBox="0 0 24 24"><circle class="stroke" cx="11" cy="11" r="7" /><path class="stroke" d="M21 21l-4.3-4.3" /></symbol>
      <symbol id="i-panel" viewBox="0 0 24 24"><rect class="stroke" x="3" y="4" width="18" height="16" rx="2" /><path class="stroke" d="M9.5 4v16" /></symbol>
      <symbol id="i-mic" viewBox="0 0 24 24"><rect class="stroke" x="9" y="2.5" width="6" height="11" rx="3" /><path class="stroke" d="M5 11a7 7 0 0 0 14 0M12 18v3.5" /></symbol>
      <symbol id="i-arrow-up" viewBox="0 0 24 24"><path class="stroke" d="M12 19V5M5.5 11.5L12 5l6.5 6.5" /></symbol>
      <symbol id="i-arrow-down" viewBox="0 0 24 24"><path class="stroke" d="M12 5v14M5.5 12.5L12 19l6.5-6.5" /></symbol>
      <symbol id="i-back" viewBox="0 0 24 24"><path class="stroke" d="M19 12H5M11.5 5.5L5 12l6.5 6.5" /></symbol>
      <symbol id="i-chev-d" viewBox="0 0 24 24"><path class="stroke" d="M6 9.5l6 6 6-6" /></symbol>
      <symbol id="i-chev-r" viewBox="0 0 24 24"><path class="stroke" d="M9.5 6l6 6-6 6" /></symbol>
      <symbol id="i-menu" viewBox="0 0 24 24"><path class="stroke" d="M4 7h16M4 12h16M4 17h16" /></symbol>
      <symbol id="i-zap" viewBox="0 0 24 24"><path class="stroke" d="M13 2L3.5 14H11l-1 8L19.5 10H12l1-8z" /></symbol>
      <symbol id="i-wave" viewBox="0 0 24 24"><path class="stroke" d="M3 12h3l3-8 4 16 3-8h5" /></symbol>
      <symbol id="i-globe" viewBox="0 0 24 24"><circle class="stroke" cx="12" cy="12" r="9" /><path class="stroke" d="M3 12h18M12 3c2.5 2.6 4 5.6 4 9s-1.5 6.4-4 9c-2.5-2.6-4-5.6-4-9s1.5-6.4 4-9z" /></symbol>
      <symbol id="i-shield" viewBox="0 0 24 24"><path class="stroke" d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" /><path class="stroke" d="M9 11.5l2 2 4-4" /></symbol>
      <symbol id="i-db" viewBox="0 0 24 24"><ellipse class="stroke" cx="12" cy="5" rx="9" ry="3" /><path class="stroke" d="M3 5v14c0 1.7 4 3 9 3s9-1.3 9-3V5M3 12c0 1.7 4 3 9 3s9-1.3 9-3" /></symbol>
      <symbol id="i-file" viewBox="0 0 24 24"><path class="stroke" d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8l-6-6z" /><path class="stroke" d="M14 2v6h6" /></symbol>
      <symbol id="i-image" viewBox="0 0 24 24"><rect class="stroke" x="3" y="3" width="18" height="18" rx="2" /><circle class="stroke" cx="8.5" cy="8.5" r="1.5" /><path class="stroke" d="M21 15l-5-5L5 21" /></symbol>
      <symbol id="i-link" viewBox="0 0 24 24"><path class="stroke" d="M10 13a5 5 0 0 0 7.5.5l3-3a5 5 0 0 0-7-7l-1.7 1.7" /><path class="stroke" d="M14 11a5 5 0 0 0-7.5-.5l-3 3a5 5 0 0 0 7 7l1.7-1.7" /></symbol>
      <symbol id="i-download" viewBox="0 0 24 24"><path class="stroke" d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3" /></symbol>
      <symbol id="i-share" viewBox="0 0 24 24"><path class="stroke" d="M4 12v8a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-8M16 6l-4-4-4 4M12 2v13" /></symbol>
      <symbol id="i-more" viewBox="0 0 24 24"><circle cx="5" cy="12" r="1.4" /><circle cx="12" cy="12" r="1.4" /><circle cx="19" cy="12" r="1.4" /></symbol>
      <symbol id="i-check" viewBox="0 0 24 24"><path class="stroke" d="M20 6L9 17l-5-5" /></symbol>
      <symbol id="i-camera" viewBox="0 0 24 24"><path class="stroke" d="M23 19a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4l2-3h6l2 3h4a2 2 0 0 1 2 2z" /><circle class="stroke" cx="12" cy="13" r="4" /></symbol>
      <symbol id="i-stop" viewBox="0 0 24 24"><rect x="6.5" y="6.5" width="11" height="11" rx="2" fill="currentColor" /></symbol>
      <symbol id="i-attach" viewBox="0 0 24 24"><path class="stroke" d="M20.4 11.7l-8.5 8.5a5.5 5.5 0 0 1-7.8-7.8l9.2-9.2a3.7 3.7 0 0 1 5.2 5.2l-9.2 9.2a1.8 1.8 0 0 1-2.6-2.6l8.5-8.5" /></symbol>
      <symbol id="i-ext" viewBox="0 0 24 24"><path class="stroke" d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6M15 3h6v6M10 14L21 3" /></symbol>
      <symbol id="i-upload" viewBox="0 0 24 24"><path class="stroke" d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M17 8l-5-5-5 5M12 3v12" /></symbol>
      <symbol id="i-trash" viewBox="0 0 24 24"><path class="stroke" d="M3 6h18M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6" /></symbol>
      <symbol id="i-alert" viewBox="0 0 24 24"><path class="stroke" d="M10.3 3.9L1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z" /><path class="stroke" d="M12 9v4M12 17h.01" /></symbol>
      <symbol id="i-refresh" viewBox="0 0 24 24"><path class="stroke" d="M20 12a8 8 0 1 1-2.3-5.6M20 3v4h-4" /></symbol>
      <symbol id="i-filter" viewBox="0 0 24 24"><path class="stroke" d="M4 6h16M7 12h10M10 18h4" /></symbol>
      <symbol id="i-archive" viewBox="0 0 24 24"><rect class="stroke" x="3" y="4" width="18" height="5" rx="1" /><path class="stroke" d="M5 9v10a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V9M10 13h4" /></symbol>
    </defs>
  </svg>
</template>
