import { createRoot } from 'react-dom/client';
import { Widget } from './Widget';
import styles from './widget.css';

const config = window.TAYUNE_TEA_COMPANION;
if (config && !document.getElementById('tayune-tea-companion')) {
  // Shadow DOM让主题样式与客服样式互不影响，不改变搜索、购物车或结账。
  const host = document.createElement('div');
  host.id = 'tayune-tea-companion';
  const shadow = host.attachShadow({ mode: 'open' });
  const style = document.createElement('style');
  style.textContent = styles;
  const mount = document.createElement('div');
  shadow.append(style, mount);
  document.body.appendChild(host);
  createRoot(mount).render(<Widget config={config} />);
}
