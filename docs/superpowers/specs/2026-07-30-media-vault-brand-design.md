# 媒体仓 Logo 与 Icon 设计

## 背景

Vue 管理台顶部目前使用蓝色 `CMS` 文字块作为临时品牌标识，浏览器标签没有项目图标。用户已在三套视觉方案中确认采用“媒体仓”：用资料库轮廓表达媒体入库，用播放三角表达 STRM/播放能力。

## 目标

- 为 cms-tg-ingest 建立一套与现有蓝灰色后台 UI 一致的轻量品牌资产。
- 在顶部品牌区显示 Logo，并为浏览器标签和移动端快捷方式提供 Icon。
- 保持小尺寸清晰，不改变现有业务布局、导航、配色体系或交互。

## 视觉规范

主图形使用 `64 × 64` SVG 视图框：

- 外框：圆角方形，主色 `#1D4ED8`，圆角半径 16。
- 媒体仓：白色描边资料库轮廓，描边宽度 4，包含顶部格线。
- 播放符号：白色实心三角，置于资料库下半部。
- 不使用渐变、阴影、发光、动画或多余装饰。

同一几何图形用于 Header、favicon 和移动端图标，避免不同尺寸出现品牌差异。Header 中保留现有“入库助手”文字和“115 · CMS · Emby 工作流”说明。

## 资产与接入

新增公共品牌目录 `frontend/public/brand/`：

- `logo-mark.svg`：唯一 SVG 主资产，供 Header 和 favicon 使用。
- `favicon-32.png`：由同一 SVG 图形生成的 32 × 32 PNG，作为浏览器回退图标。
- `apple-touch-icon.png`：由同一 SVG 图形生成的 180 × 180 PNG，供移动端快捷方式使用。

修改 `frontend/index.html`：

- 增加 SVG favicon 链接。
- 增加 32 px PNG favicon 回退链接。
- 增加 Apple Touch Icon 链接。
- 增加与 Logo 主色一致的 `theme-color`。
- 保留现有页面标题“CMS 入库助手”。

修改 `frontend/src/App.vue`：

- 用 `logo-mark.svg` 替换当前 `CMS` 文字块。
- 通过 `import.meta.env.BASE_URL` 生成 `/app/` 基路径下的静态资源地址，兼容当前 Vite 配置。
- Logo 图片作为相邻品牌文字的装饰图形，使用空 `alt`，不重复屏幕阅读器文本。

修改 `frontend/src/styles.css`：

- Header Logo 固定为 38 × 38 px。
- 保持现有 Header 高度、品牌间距和移动端布局不变。
- 删除只服务于旧 `CMS` 文字块的 `.brand-mark` 样式。

## 异常与兼容

- SVG 和 PNG 都随 Vite 构建产物打包，不依赖网络、字体或第三方图标服务。
- 如果浏览器不支持 SVG favicon，可回退到 32 px PNG；页面标题和品牌文字始终可见。
- 不新增运行时请求、后端配置、数据库字段或 Docker 挂载。

## 测试与验收

- 先增加前端静态契约测试，确认 Header 使用品牌资产且 `index.html` 包含 favicon、Apple Touch Icon 和主题色。
- 运行完整前端测试和 Vite 生产构建，确认 `/app/` 基路径生成正确。
- 检查构建后的 `frontend/dist/brand/` 包含 SVG、32 px PNG 和 180 px PNG。
- 在桌面宽度和移动端宽度确认 Header 高度、文字和图标不溢出。
- 在 38 px、32 px 和 16 px 尺寸确认图形仍可辨识。

## 非目标

- 不重构后台页面或导航。
- 不增加启动动画、Logo 切换、深色模式版本或品牌配置项。
- 不修改旧 Python 页面、Telegram 消息、Docker Hub/GitHub 介绍或应用名称。

## 成功标准

打开 `/app/` 时，顶部品牌区显示“媒体仓”Logo，浏览器标签显示同款 favicon，移动端快捷方式可使用 180 px 图标；现有管理台功能、布局和构建流程保持不变。
