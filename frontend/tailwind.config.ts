import type { Config } from 'tailwindcss'

/**
 * Tailwind ↔ tokens.css 映射（frontend-api.md §6 冻结映射表——值逐字对齐 docs/design/tokens.css）。
 * 语义色 badge 类（.badge-success/warning/danger/kb）、.chip.active、.skeleton、.caret、.dot-* 等组件基类
 * 不在此重复定义——由 src/styles/tokens.css 原文进全局层承担（§6 注）。
 */
export default {
  content: ['./index.html', './src/**/*.{vue,ts}'],
  theme: {
    extend: {
      colors: {
        bg: { primary: '#191a1a', sidebar: '#141516' },
        surface: {
          DEFAULT: '#202222',
          hover: '#26282a',
          active: '#2d3031',
          ghost: 'rgba(255,255,255,.04)',
        },
        line: { DEFAULT: '#2e3230', strong: '#3d4144' },
        txt: {
          primary: '#f2f2ee',
          secondary: '#a0a6ad',
          tertiary: '#7d858c',
          faint: '#565d63',
        },
        accent: {
          DEFAULT: '#20b8cd',
          hover: '#47c5d6',
          bg: 'rgba(32,184,205,.12)',
          border: 'rgba(32,184,205,.35)',
        },
        ok: '#3fb450',
        warn: '#e0a33e',
        danger: '#e5484d',
        kb: '#8f7ff0',
      },
      borderRadius: { sm: '8px', md: '12px', lg: '16px', xl: '20px' },
      spacing: { sidebar: '248px', sources: '320px', searchcard: '640px', content: '768px' },
      fontSize: {
        xs: ['12px', '1.5'],
        sm: ['13px', '1.55'],
        base: ['15px', '1.6'],
        md: ['16px', '1.7'],
        lg: ['18px', '1.6'],
        xl: ['20px', '1.5'],
        '2xl': ['26px', '1.4'],
        '3xl': ['32px', '1.3'],
      },
      boxShadow: {
        card: 'var(--shadow-card)',
        floating: 'var(--shadow-floating)',
        focus: 'var(--shadow-focus)',
      },
    },
  },
} satisfies Config
