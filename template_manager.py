import colorsys
import os
import re
from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional

from astrbot.api import logger


class TemplateManager:
    """Manage external HTML templates stored on disk."""

    _BUILTIN_PROMPT_PATTERN = re.compile(
        r"<!--\s*BUILTIN_PROMPT\s*?\n(.*?)-->",
        re.DOTALL,
    )
    _STYLE_PATTERN = re.compile(r"<style\b[^>]*>(.*?)</style>", re.IGNORECASE | re.DOTALL)
    _CSS_COMMENT_PATTERN = re.compile(r"/\*.*?\*/", re.DOTALL)
    _CSS_COLOR_PATTERN = re.compile(
        r"#(?:[0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{4}|[0-9a-fA-F]{3})"
        r"(?![0-9a-fA-F])|rgba?\([^)]*\)",
        re.IGNORECASE,
    )
    _CSS_VARIABLE_PATTERN = re.compile(
        r"--(?P<name>[a-zA-Z0-9_-]+)\s*:\s*(?P<value>[^;{}]+)",
        re.IGNORECASE,
    )
    _CSS_VAR_REFERENCE_PATTERN = re.compile(
        r"var\(\s*--(?P<name>[a-zA-Z0-9_-]+)",
        re.IGNORECASE,
    )
    _PRIMARY_COLOR_TOKENS = {"primary", "theme", "brand", "main"}
    _ACCENT_COLOR_TOKENS = {"accent", "highlight", "active"}
    _SECONDARY_COLOR_TOKENS = {"secondary", "tertiary"}
    _NAMED_COLOR_TOKENS = {
        "amber", "aqua", "azure", "berry", "blue", "celadon", "coral",
        "cyan", "gold", "green", "indigo", "lavender", "lime", "magenta",
        "mint", "olive", "orange", "peach", "pink", "purple", "red",
        "rose", "sky", "teal", "terra", "turquoise", "violet", "wine",
        "yellow",
    }
    _NEUTRAL_COLOR_TOKENS = {
        "background", "bg", "border", "dark", "deep", "dim", "glass", "glow",
        "ink", "light", "line", "muted", "panel", "paper", "rule", "shadow",
        "soft", "surface", "text", "white",
    }

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

    def extract_color_palette(
        self,
        template_name: str,
        limit: int = 6,
    ) -> Optional[Dict[str, Any]]:
        """Extract the most useful accent palette from a template's inline CSS."""
        filepath = os.path.join(self.TEMPLATE_DIR, f"{template_name}.html")
        if not os.path.isfile(filepath):
            return None

        try:
            with open(filepath, "r", encoding="utf-8") as handle:
                raw_html = handle.read()
        except Exception as exc:
            logger.error(f"[HTML渲染] 读取模板 {template_name} 配色失败: {exc}")
            return None

        style_blocks = self._STYLE_PATTERN.findall(raw_html)
        if not style_blocks:
            return None
        css = self._CSS_COMMENT_PATTERN.sub("", "\n".join(style_blocks))

        color_counts: Counter[str] = Counter()
        first_seen: Dict[str, int] = {}
        for index, match in enumerate(self._CSS_COLOR_PATTERN.finditer(css)):
            color = self._normalize_css_color(match.group(0))
            if not color:
                continue
            color_counts[color] += 1
            first_seen.setdefault(color, index)

        if not color_counts:
            return None

        color_variable_names: Dict[str, list[str]] = defaultdict(list)
        for match in self._CSS_VARIABLE_PATTERN.finditer(css):
            color_match = self._CSS_COLOR_PATTERN.search(match.group("value"))
            if not color_match:
                continue
            color = self._normalize_css_color(color_match.group(0))
            if not color:
                continue
            variable_name = match.group("name").lower()
            color_variable_names[color].append(variable_name)

        variable_uses = Counter(
            match.group("name").lower()
            for match in self._CSS_VAR_REFERENCE_PATTERN.finditer(css)
        )

        ranked: list[tuple[float, int, str]] = []
        for color, count in color_counts.items():
            red, green, blue = self._hex_to_rgb(color)
            _, lightness, saturation = colorsys.rgb_to_hls(
                red / 255,
                green / 255,
                blue / 255,
            )
            if lightness <= 0.08 or lightness >= 0.96:
                continue

            score = min(count, 30) * 1.5 + saturation * 18
            score += max(0.0, 1.0 - abs(lightness - 0.55) * 2) * 6
            for variable_name in color_variable_names.get(color, []):
                tokens = set(filter(None, re.split(r"[-_]", variable_name)))
                neutral_tokens = tokens & self._NEUTRAL_COLOR_TOKENS
                score += min(variable_uses.get(variable_name, 0), 20) * 9
                if not neutral_tokens and tokens & self._PRIMARY_COLOR_TOKENS:
                    score += 90
                if not neutral_tokens and tokens & self._ACCENT_COLOR_TOKENS:
                    score += 65
                if not neutral_tokens and tokens & self._SECONDARY_COLOR_TOKENS:
                    score += 42
                if tokens & self._NAMED_COLOR_TOKENS:
                    score += 28
                score -= len(neutral_tokens) * 55

            if saturation < 0.08:
                score -= 45
            ranked.append((score, first_seen[color], color))

        if not ranked:
            return None

        ranked.sort(key=lambda item: (-item[0], item[1]))
        chromatic = [
            item
            for item in ranked
            if self._color_saturation(item[2]) >= 0.08
        ]
        selected = chromatic or ranked
        palette = [item[2] for item in selected[:max(1, min(limit, 12))]]
        primary = palette[0]
        return {
            "template": template_name,
            "primary": primary,
            "tone": self._describe_color_tone(primary),
            "colors": palette,
        }

    @classmethod
    def _normalize_css_color(cls, raw_color: str) -> Optional[str]:
        value = str(raw_color or "").strip().lower()
        if value.startswith("#"):
            digits = value[1:]
            if len(digits) in {3, 4}:
                digits = "".join(char * 2 for char in digits)
            if len(digits) not in {6, 8}:
                return None
            if len(digits) == 8 and int(digits[6:8], 16) == 0:
                return None
            return f"#{digits[:6]}"

        match = re.fullmatch(r"rgba?\((.*)\)", value, re.IGNORECASE)
        if not match:
            return None
        parts = [part.strip() for part in match.group(1).split(",")]
        if len(parts) not in {3, 4}:
            return None
        try:
            channels = []
            for part in parts[:3]:
                if part.endswith("%"):
                    channel = round(float(part[:-1]) * 2.55)
                else:
                    channel = round(float(part))
                channels.append(max(0, min(255, channel)))
            if len(parts) == 4:
                alpha = float(parts[3].rstrip("%"))
                if parts[3].endswith("%"):
                    alpha /= 100
                if alpha <= 0:
                    return None
        except (TypeError, ValueError):
            return None
        return "#{:02x}{:02x}{:02x}".format(*channels)

    @staticmethod
    def _hex_to_rgb(color: str) -> tuple[int, int, int]:
        return tuple(int(color[index:index + 2], 16) for index in (1, 3, 5))

    @classmethod
    def _color_saturation(cls, color: str) -> float:
        red, green, blue = cls._hex_to_rgb(color)
        return colorsys.rgb_to_hls(red / 255, green / 255, blue / 255)[2]

    @classmethod
    def _describe_color_tone(cls, color: str) -> str:
        red, green, blue = cls._hex_to_rgb(color)
        hue, lightness, saturation = colorsys.rgb_to_hls(
            red / 255,
            green / 255,
            blue / 255,
        )
        if saturation < 0.08:
            base = "中性"
        else:
            degrees = hue * 360
            tone_ranges = (
                (15, "红色"), (40, "橙色"), (65, "金黄色"),
                (90, "黄绿色"), (145, "绿色"), (175, "青绿色"),
                (200, "青色"), (250, "蓝色"), (285, "紫色"),
                (330, "洋红色"), (360, "玫红色"),
            )
            base = next(label for upper, label in tone_ranges if degrees < upper)
        if lightness < 0.28:
            return f"深{base}调"
        if lightness > 0.78:
            return f"浅{base}调"
        return f"{base}调"

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
