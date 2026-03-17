import os
import re
from typing import Dict, List, Optional

from astrbot.api import logger


class TemplateManager:
    """Manage external HTML templates stored on disk."""

    _BUILTIN_PROMPT_PATTERN = re.compile(
        r"<!--\s*BUILTIN_PROMPT\s*?\n(.*?)-->",
        re.DOTALL,
    )

    def __init__(self, template_dir: str):
        self.TEMPLATE_DIR = template_dir
        self.templates: Dict[str, str] = {}
        self.template_id_map: Dict[int, str] = {}

    async def load_templates(self):
        """Preload templates from disk for startup diagnostics."""
        self.templates = {}
        os.makedirs(self.TEMPLATE_DIR, exist_ok=True)

        for template_name in self.get_available_templates():
            filepath = os.path.join(self.TEMPLATE_DIR, f"{template_name}.html")
            try:
                with open(filepath, "r", encoding="utf-8") as handle:
                    self.templates[template_name] = handle.read()
                logger.info(f"[HTML渲染] 已加载模板: {template_name}")
            except Exception as exc:
                logger.error(f"[HTML渲染] 加载模板 {filepath} 失败: {exc}")

        if not self.templates:
            logger.warning(
                f"[HTML渲染] 未找到任何模板文件，请先在 {self.TEMPLATE_DIR} 中放入至少一个 .html 模板"
            )

    def get_available_templates(self) -> List[str]:
        """Return all template file names without extension."""
        if not os.path.isdir(self.TEMPLATE_DIR):
            return []

        templates = set()
        for filename in os.listdir(self.TEMPLATE_DIR):
            if filename.endswith(".html"):
                templates.add(filename[:-5])
        return sorted(templates)

    def require_available_templates(self) -> List[str]:
        templates = self.get_available_templates()
        if templates:
            return templates

        raise FileNotFoundError(
            f"未找到任何模板文件，请先在 {self.TEMPLATE_DIR} 中放入至少一个 .html 模板"
        )

    def has_template(self, template_name: Optional[str]) -> bool:
        if not template_name:
            return False
        return template_name in self.get_available_templates()

    def load_template(self, template_name: str) -> str:
        """Load one template from disk on demand."""
        if not template_name:
            raise ValueError("模板名不能为空")

        filepath = os.path.join(self.TEMPLATE_DIR, f"{template_name}.html")
        if not os.path.isfile(filepath):
            raise FileNotFoundError(f"模板不存在: {template_name} ({filepath})")

        try:
            with open(filepath, "r", encoding="utf-8") as handle:
                content = handle.read()
        except Exception as exc:
            raise RuntimeError(f"读取模板失败: {template_name}: {exc}") from exc

        return self.strip_builtin_prompt(content)

    @classmethod
    def strip_builtin_prompt(cls, html: str) -> str:
        """Remove BUILTIN_PROMPT comment blocks before rendering."""
        return cls._BUILTIN_PROMPT_PATTERN.sub("", html)

    def extract_builtin_prompt(self, template_name: str) -> Optional[str]:
        filepath = os.path.join(self.TEMPLATE_DIR, f"{template_name}.html")
        if not os.path.isfile(filepath):
            return None

        try:
            with open(filepath, "r", encoding="utf-8") as handle:
                raw_html = handle.read()
        except Exception as exc:
            logger.error(f"[HTML渲染] 读取模板 {template_name} 失败: {exc}")
            return None

        match = self._BUILTIN_PROMPT_PATTERN.search(raw_html)
        if not match:
            return None

        prompt = match.group(1).strip()
        return prompt or None

    def extract_all_builtin_prompts(self) -> Dict[str, str]:
        result: Dict[str, str] = {}
        for template_name in self.get_available_templates():
            prompt = self.extract_builtin_prompt(template_name)
            if prompt:
                result[template_name] = prompt
        return result

    def update_template_id_map(self):
        available = self.get_available_templates()
        self.template_id_map = {
            idx: name for idx, name in enumerate(available, start=1)
        }
        logger.debug(f"[HTML渲染] 模板 ID 映射已更新: {self.template_id_map}")

    @staticmethod
    def get_default_test_content(template_name: Optional[str] = None) -> str:
        _ = template_name
        return """# HTML Render Preview

这是一段模板预览文本。
这里会测试普通段落、列表、代码块和数学公式。

- 项目一
- 项目二

```python
print("Hello from AstrBot")
```

行内公式 $a^2 + b^2 = c^2$

$$
\\int_0^1 x^2 dx = \\frac{1}{3}
$$
"""

    @staticmethod
    def get_gif_test_content() -> str:
        return """<render gif>
<style>
body {
    margin: 0;
    padding: 24px;
    background: linear-gradient(135deg, #0f172a, #1e293b);
    font-family: "Microsoft YaHei", sans-serif;
}
.stage {
    width: 520px;
    padding: 28px;
    border-radius: 20px;
    background: rgba(255, 255, 255, 0.12);
    color: #f8fafc;
    overflow: hidden;
    box-shadow: 0 16px 48px rgba(15, 23, 42, 0.28);
}
.track {
    display: inline-block;
    white-space: nowrap;
    font-size: 32px;
    font-weight: 700;
    letter-spacing: 2px;
    animation: slide 4s linear infinite;
}
@keyframes slide {
    0% { transform: translateX(100%); }
    100% { transform: translateX(-120%); }
}
</style>
<div class="stage">
    <div class="track">AstrBot HTML Render GIF Preview</div>
</div>
</render>"""
