---
name: 入库助手
description: 家庭媒体入库控制台：清爽、可信、少打扰
colors:
  primary: "#4c5fd5"
  primary-hover: "#3e4ec4"
  primary-soft: "#eef0fc"
  primary-invert: "#ffffff"
  bg: "#f5f6fb"
  surface: "#ffffff"
  surface-2: "#f8f9fd"
  text: "#1f2937"
  text-strong: "#141829"
  muted: "#5f6b7a"
  border: "#e6e8f0"
  success: "#1d8a4e"
  warning: "#b57a00"
  danger: "#d03050"
  dark-bg: "#10121a"
  dark-surface: "#171a24"
  dark-primary: "#8b93ff"
typography:
  body:
    fontFamily: "-apple-system, BlinkMacSystemFont, Segoe UI, system-ui, PingFang SC, Hiragino Sans GB, Microsoft YaHei, sans-serif"
    fontSize: "16px"
    fontWeight: 400
    lineHeight: 1.5
  title:
    fontFamily: "-apple-system, BlinkMacSystemFont, Segoe UI, system-ui, PingFang SC, Hiragino Sans GB, Microsoft YaHei, sans-serif"
    fontSize: "28px"
    fontWeight: 700
    lineHeight: 1.2
    letterSpacing: "-0.02em"
  label:
    fontFamily: "-apple-system, BlinkMacSystemFont, Segoe UI, system-ui, PingFang SC, Hiragino Sans GB, Microsoft YaHei, sans-serif"
    fontSize: "12px"
    fontWeight: 600
    lineHeight: 1.35
  mono:
    fontFamily: "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace"
    fontSize: "12px"
    fontWeight: 400
    lineHeight: 1.55
rounded:
  sm: "6px"
  md: "8px"
  lg: "10px"
  pill: "999px"
spacing:
  sm: "8px"
  md: "16px"
  lg: "28px"
components:
  button-primary:
    backgroundColor: "{colors.primary}"
    textColor: "{colors.primary-invert}"
    rounded: "{rounded.md}"
    padding: "8px 14px"
  button-primary-hover:
    backgroundColor: "{colors.primary-hover}"
    textColor: "{colors.primary-invert}"
    rounded: "{rounded.md}"
  card:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.text}"
    rounded: "{rounded.lg}"
    padding: "16px 18px"
  chip:
    backgroundColor: "{colors.surface-2}"
    textColor: "{colors.muted}"
    rounded: "{rounded.sm}"
    padding: "1px 7px"
  input:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.text}"
    rounded: "{rounded.md}"
---

# Design System: 入库助手

## Overview

**Creative North Star: "The Quiet Media Desk"**

这是家庭媒体库旁边的一张安静工作台，不是 NAS 插件后台。默认克制：浅纸色底、细描边、几乎没有阴影；只有任务卡住、质量告警、解锁花费积分时才提高音量。暗色模式改成分层表面，而不是把浅色反相。

文字用系统中西文黑体，不加载网红英文字体。品牌落在精确的靛蓝主色、表格数字和确认文案上，不靠装饰。

**Key Characteristics:**
- 异常优先，首页只回答现在是否正常
- 亮色细边 + 一层轻阴影；暗色只用色阶分层
- 主色少用，留给链接、主按钮和当前导航
- 危险操作先说后果，再让人确认

## Colors

一组冷灰纸色托住偶尔出现的靛蓝操作色。语义色只表示成功、等待、危险，不拿来铺背景。

### Primary
- **Desk Indigo** (`{colors.primary}`): 链接、主按钮、当前菜单、版本胶囊。暗色改用 `{colors.dark-primary}`，主按钮字改成 `{colors.dark-bg}` 以保住对比。

### Neutral
- **Paper** (`{colors.bg}`): 页面底。
- **Sheet** (`{colors.surface}`): 卡片、顶栏、页脚。
- **Ink** (`{colors.text-strong}`): 标题。
- **Body** (`{colors.text}`): 正文。
- **Quiet** (`{colors.muted}`): 辅助说明，对比保持可读。
- **Hairline** (`{colors.border}`): 默认分割。

### Named Rules
**The One Accent Rule.** 主色出现面积不超过一屏的一成。统计数字用墨色，不用主色。

## Typography

**Display Font:** 系统黑体（苹方 / Segoe / San Francisco）
**Body Font:** 同上
**Label/Mono Font:** ui-monospace，只给日志、版本号、命令

**Character:** 像打印在纸上的运维说明：标题紧、正文清楚、不演戏。

### Hierarchy
- **Title** (700, 28px, -0.02em): 页面 `h1`。
- **Body** (400, 16px): 说明和表单。
- **Label** (600, 12px): 统计标签、页脚产品名。
- **Mono** (12px / 1.55): 日志控制台；控制台在两种主题下都保持深底。

### Named Rules
**The No Costume Mono Rule.** 等宽字体只用于日志、版本和命令，不拿来装「很技术」。

## Layout

内容区最大 1240px，左右 28px，窄屏收到 18px。四列指标在 860px 收成两列，520px 单列。桌面表格、窄屏改卡片。触控目标不小于 44px。尊重 `viewport-fit` 安全区。

## Elevation & Depth

默认是平的。亮色卡片只有一层几乎看不见的纸影；暗色把同一层略加深，主要靠更深的表面色分层。

### Shadow Vocabulary
- **Card rest** (`box-shadow: 0 1px 2px rgba(20, 24, 41, 0.04)`): 亮色指标卡和海报。
- **Card rest dark** (`box-shadow: 0 1px 2px rgba(0, 0, 0, 0.2)`): 暗色同一位置，不另加光晕。

### Named Rules
**The Flat-By-Default Rule.** 休息状态用边框。阴影不作为装饰光晕。

## Shapes

卡片 10px，按钮 8px，小标签 6px，版本号和评分用胶囊。海报圆角跟卡片走。不要硬偏移阴影。

## Components

### Buttons
- **Shape:** 8px，主按钮白字压靛蓝；暗色主按钮用深字压浅靛蓝。
- **Hover / Focus:** 主色略加压；焦点是 2px `{colors.primary-hover}` 环。
- **Secondary:** 描边，不铺主色。

### Chips
- **Style:** 浅底细边 11px；种类芯片用主色软底。

### Cards / Containers
- **Corner Style:** 10px
- **Background:** `{colors.surface}`
- **Border:** 1px `{colors.border}`
- **Internal Padding:** 16–18px

### Inputs / Fields
- **Style:** 每项都有可见 label，placeholder 只举例子。
- **Focus:** 与主色焦点环一致。

### Navigation
侧栏 220px，折叠 64px。当前项用软主色底，而不是粗彩条。

### Log console
深底终端，两种主题都不改成浅色。

## Do's and Don'ts

### Do:
- **Do** 用 token，不要在组件里另写一套蓝。
- **Do** 给每个输入可见名称。
- **Do** 在会改任务或容器的操作前说明后果。
- **Do** 窄屏把宽表收成卡片。

### Don't:
- **Don't** 用 Inter / Geist / Plus Jakarta 这类网红英文字体。
- **Don't** 把内部 action key 写在按钮上。
- **Don't** 把底层日志堆到首页。
- **Don't** 为了「更炫」加渐变字或玻璃拟态。
