import type { Config } from 'tailwindcss';

export default {
  content: ['./app/**/*.{ts,tsx}', './components/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        surface: '#0f1216',
        panel: '#171b21',
        edge: '#252b34',
        muted: '#8b95a3',
        ok: '#3fb950',
        warn: '#d29922',
        bad: '#f85149',
        info: '#58a6ff',
      },
      fontFamily: {
        mono: ['ui-monospace', 'SFMono-Regular', 'Menlo', 'monospace'],
      },
    },
  },
  plugins: [],
} satisfies Config;
