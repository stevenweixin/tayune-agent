# TAYUNE Tea Companion

WordPress悬浮客服插件。右下角淡蓝/粉紫珠光玻璃球、内部流动波带，无球外文案；点击展开磨砂玻璃面板。首页额外避让视频暂停按钮。
样式通过Shadow DOM隔离，不更改主题、普通搜索、商品或结账。账户/结账页不加载本插件。

## 当前发布范围

2026-10-21安装并启用，当前版本1.1.2，**仅前端**。用户明确暂不公开聊天。
PHP配置中的`chatEnabled`为`false`；输入/发送禁用，浏览商品和联系店铺可用。
打开面板、运行动画不申请会话或调用模型，不以模拟回答冒充Agent。
隧道仍只公开健康检查。不要仅把开关改为true就声称真实问答上线。

## 文件与构建

1.2.0是准备中的公开聊天版本，PHP配置已改为true，尚未上传到真实网站。
真实网站仍使用1.1.2关闭聊天配置；发布状态以`docs/public/RELEASE.md`为准。
开放前需要同步隧道精确路径白名单，不公开demo、OpenAPI或Milvus。

- `tayune_assistant.php`：页面范围、资源加载、公开配置，不含密钥。
- `src/Orb.jsx`：已确认的GPU动效，最多30fps，支持暂停/减少动态效果/隐藏时暂停。
- 面板白色底层不透明度58%，背景模糊28px；暂停控件位于面板顶部。减少透明度偏好或不支持模糊时用实色降级。
- `src/Widget.jsx`：会话界面和API接线；开关关闭时send在入口返回。
- `src/widget.css`：仅作用于插件Shadow DOM。
- `src/entry.jsx`：创建独立挂载点，不读取现有表单或账户信息。
- `assets/tea_companion.js`及LEGAL文件：已构建资源，保留第三方许可证。
- 旧`search_assistant.js/css`不再加载，保留作历史实现，不一并打包。

在本目录使用Node18或更新版本：

```bash
npm ci
npm run build
php -l tayune_assistant.php
```

安装包只含一个顶层目录`tayune-tea-companion/`，其中放PHP入口和assets内的JS/LEGAL文件。
不要打包node_modules、源码参考、.env、模型、会话库、隧道凭据或网站备份。
运行时不依赖React CDN，也不要求WordPress服务器安装Node。

## 验证与回滚

未登录访客检查悬浮球、打开/关闭、暂停/恢复、手机布局及普通搜索。
当前应看见Chat coming soon且无法输入问题，不应出现任何Agent回答。
WordPress后台“插件 → TAYUNE Tea Companion → 停用”即可移除界面；不删除数据表，不影响购物。
如页面被缓存，需要按站点现有缓存流程刷新。后续更新提高插件和资源版本，防止旧JS被复用。

正式开放聊天前需单独复核API限额、超时、错误响应、隐私提示、真实商品/知识问答，
再经用户确认同步开关及隧道路径。不要公开Milvus、SQLite、demo或OpenAPI。
