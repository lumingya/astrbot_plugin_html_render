# AstrBot HTML Render Plugin

[![AstrBot](https://img.shields.io/badge/AstrBot-Plugin-blue)](https://github.com/Soulter/AstrBot)
[![Python](https://img.shields.io/badge/Python-3.9+-green)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-yellow)](LICENSE)

把 AI 返回的文本、Markdown 或自定义 HTML 渲染成图片发送，支持 GIF、数学公式、模板热更新和自动渲染。

## 功能

- 支持外部 HTML 模板，修改 `templates/*.html` 后重新渲染即可生效
- 支持 Markdown 渲染，保留普通换行、代码块、列表、引用等结构
- 支持 LaTeX 数学公式，兼容 `$...$`、`$$...$$`、`\(...\)`、`\[...\]`
- 支持 `<render gif>` 录制 CSS 动画为 GIF
- 支持自动渲染全部回复，并可用 `auto_render_min_length` 跳过过短文本
- 支持按用户切换默认模板

## 重要变更

- 插件不再内置任何默认模板，也不会自动生成 `card/dialogue/novel`
- `templates` 目录里如果没有任何 `.html` 模板，渲染会直接报错
- `default_template` 和 `auto_render_template` 如果配置了不存在的模板，也会直接报错
- 仪表盘中的模板配置项会根据 `templates` 目录自动生成下拉选项，不需要手动输入模板名

## 安装

1. 安装插件到 `data/plugins/astrbot_plugin_html_render`

2. 安装依赖：

```bash
pip install playwright aiohttp mistune Pillow
playwright install chromium
```

3. 在插件目录准备至少一个模板文件：

```text
astrbot_plugin_html_render/
  templates/
    your_template.html
```

模板文件中需要包含 `{{content}}` 占位符，例如：

```html
<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <style>
    body {
      margin: 0;
      padding: 24px;
      background: #f6f2e9;
      font-family: "Microsoft YaHei", sans-serif;
    }
    .card {
      background: white;
      border-radius: 16px;
      padding: 24px;
      line-height: 1.8;
      box-shadow: 0 12px 32px rgba(0, 0, 0, 0.12);
    }
  </style>
</head>
<body>
  <div class="card">{{content}}</div>
</body>
</html>
```

## 配置示例

```yaml
inject_prompt: true

# 模板选择
default_template: ""               # 不填则自动使用第一个可用模板
auto_render_template: ""           # 不填则回落到当前默认模板
auto_dialogue_detection: true
dialogue_quote_threshold: 1

# 渲染参数
render_width: 600
render_scale: 2
enable_markdown: true
enable_math: true

# 自动渲染
auto_render_all: true
auto_render_min_length: 20         # 低于该长度不自动渲染
enable_auto_detect: true
preserve_text_for_context: true

# GIF
gif_duration: 3.0
gif_fps: 15
gif_scale: 2

# 背景图
background_image: ""
background_image_strategy: "fixed" # fixed=固定, round_robin=轮询, random=随机
background_render_mode: "ambient" # ambient=页面氛围背景, watermark=正文内容水印
background_opacity: 0.17          # 透明度，取值 0-1
```

## 使用方式

### 命令

- `/测试 <文本>`：测试当前模板渲染效果
- `/切换 <模板名或ID>`：切换自己的默认模板
- `/查看`：查看当前可用模板列表
- `/预览模板 <模板名或ID> [文本]`：临时预览指定模板

### 手动渲染

````html
<render template="your_template">
第一行
第二行

```python
print("Hello")
```

行内公式 $a^2+b^2=c^2$
</render>
````

### GIF 渲染

```html
<render gif>
<style>
.bar {
  width: 120px;
  height: 12px;
  border-radius: 999px;
  background: #ddd;
  animation: pulse 1.2s ease-in-out infinite;
}
@keyframes pulse {
  0%, 100% { transform: scaleX(0.6); opacity: 0.4; }
  50% { transform: scaleX(1); opacity: 1; }
}
</style>
<div class="bar"></div>
</render>
```

## Markdown 与公式说明

- Markdown 路径已经修复“代码块导致其它内容换行丢失”的问题
- 普通单换行会保留下来，不会再因为 fenced code block 挤成一段
- 数学公式通过前端 MathJax 渲染，需要运行环境能访问 `https://cdn.jsdelivr.net`

## 背景图说明

- `background_image` 通过配置页下拉框选择插件目录里的图片资源
- `background_image_strategy` 控制背景图切换策略：`fixed` 使用当前选中的图片，`round_robin` 和 `random` 会在全部可用背景图中切换
- `background_render_mode: ambient` 会把图片作为页面外层氛围背景，适合做边缘衬底
- `background_render_mode: watermark` 会把图片压到 `.content` 内容区内部作为正文水印，能绕开大多数模板自带的实底容器
- `background_opacity` 用来控制背景图透明度，`watermark` 模式通常 `0.10-0.25` 比较合适

## 注意事项

- 没有模板文件时，插件不会再偷偷回退到内置样式
- 模板文件请统一保存为 UTF-8 编码
- 如果配置了模板名，文件名必须与配置完全一致，不需要写 `.html`
- 自定义 HTML 模式下，检测到 `<style>` 后会跳过 Markdown 处理，直接嵌入模板渲染
