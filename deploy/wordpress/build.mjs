import { build } from 'esbuild';

// 把React、图标和样式打成一个本地资源；访客不依赖第三方CDN。
await build({
  entryPoints: ['src/entry.jsx'],
  outfile: 'assets/tea_companion.js',
  bundle: true,
  minify: true,
  format: 'iife',
  target: ['es2020'],
  jsx: 'automatic',
  loader: { '.css': 'text' },
  define: { 'process.env.NODE_ENV': '"production"' },
  legalComments: 'linked',
});
