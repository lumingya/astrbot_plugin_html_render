from __future__ import annotations

import asyncio
import json
import sys
import types
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


class _Logger:
    def debug(self, *args, **kwargs):
        pass

    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass

    def error(self, *args, **kwargs):
        pass


class _FilterStub:
    def __getattr__(self, name):
        def decorator_factory(*args, **kwargs):
            def decorator(func):
                return func

            return decorator

        return decorator_factory


class _Component:
    def __init__(self, text: str = "", *args, **kwargs):
        self.text = text
        self.args = args
        self.kwargs = kwargs

    @classmethod
    def fromFileSystem(cls, path: str):
        return cls(path)


class _MessageChain:
    def __init__(self, chain=None):
        self.chain = list(chain or [])

    def message(self, text: str):
        self.chain.append(_Component(text))
        return self


def _install_astrbot_stubs() -> None:
    modules = {
        "astrbot": types.ModuleType("astrbot"),
        "astrbot.api": types.ModuleType("astrbot.api"),
        "astrbot.api.event": types.ModuleType("astrbot.api.event"),
        "astrbot.api.star": types.ModuleType("astrbot.api.star"),
        "astrbot.api.message_components": types.ModuleType("astrbot.api.message_components"),
        "astrbot.core": types.ModuleType("astrbot.core"),
        "astrbot.core.provider": types.ModuleType("astrbot.core.provider"),
        "astrbot.core.provider.entities": types.ModuleType("astrbot.core.provider.entities"),
        "astrbot.core.message": types.ModuleType("astrbot.core.message"),
        "astrbot.core.message.message_event_result": types.ModuleType(
            "astrbot.core.message.message_event_result"
        ),
        "astrbot.core.star": types.ModuleType("astrbot.core.star"),
        "astrbot.core.star.star_tools": types.ModuleType("astrbot.core.star.star_tools"),
    }
    sys.modules.update(modules)

    modules["astrbot.api"].logger = _Logger()
    modules["astrbot.api.event"].filter = _FilterStub()
    modules["astrbot.api.event"].AstrMessageEvent = type("AstrMessageEvent", (), {})
    modules["astrbot.api.star"].Context = type("Context", (), {})
    modules["astrbot.api.star"].Star = type("Star", (), {"__init__": lambda self, context: None})
    modules["astrbot.api.star"].register = lambda *args, **kwargs: (lambda cls: cls)
    modules["astrbot.api.message_components"].Image = type("Image", (_Component,), {})
    modules["astrbot.api.message_components"].Plain = type("Plain", (_Component,), {})
    modules["astrbot.core.message.message_event_result"].MessageChain = _MessageChain
    modules["astrbot.core.provider.entities"].ProviderRequest = type("ProviderRequest", (), {})
    modules["astrbot.core.star.star_tools"].StarTools = type(
        "StarTools",
        (),
        {"get_data_dir": staticmethod(lambda: str(REPO_ROOT / "data"))},
    )


class _Config(dict):
    def __init__(self, *args, schema=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.schema = schema


class _DummyContext:
    def __init__(self, provider=None):
        self.provider = provider

    def get_registered_star(self, name: str):
        return None

    def get_using_provider(self, *args, **kwargs):
        return self.provider


class _DummyEvent:
    def __init__(self):
        self._extra = {}

    def get_extra(self, key=None, default=None):
        if key is None:
            return self._extra
        return self._extra.get(key, default)

    def set_extra(self, key, value):
        self._extra[key] = value


class _SessionEvent(_DummyEvent):
    def __init__(self, platform_name="mock", group_id="", sender_id="", origin="mock:origin"):
        super().__init__()
        self._platform_name = platform_name
        self._group_id = group_id
        self._sender_id = sender_id
        self.unified_msg_origin = origin

    def get_platform_name(self):
        return self._platform_name

    def get_group_id(self):
        return self._group_id

    def get_sender_id(self):
        return self._sender_id

    def plain_result(self, text):
        return f"PLAIN:{text}"

    def chain_result(self, chain):
        return ("CHAIN", chain)


def _make_harness(config=None):
    from main import HtmlRenderPlugin
    from sub_html_panels import SubHtmlPanelManager
    from template_manager import TemplateManager

    harness = object.__new__(HtmlRenderPlugin)
    harness.config = config if config is not None else _Config(
        {
            "auto_render_all": True,
            "auto_render_min_length": 20,
            "default_template": "玻璃花房",
            "enable_auto_detect": True,
            "auto_merge_renders": True,
            "merged_template": "",
            "preserve_text_for_context": True,
        },
        schema={
            "default_template": {},
            "auto_render_template": {},
            "merged_template": {},
            "background_image": {},
        },
    )
    harness.context = _DummyContext()
    harness.DATA_DIR = str(REPO_ROOT / "data")
    harness.IMAGE_CACHE_DIR = str(REPO_ROOT / "data" / "html_render_cache")
    harness.template_mgr = TemplateManager(str(REPO_ROOT / "templates"))
    harness.sub_panel_mgr = SubHtmlPanelManager(str(REPO_ROOT))
    harness._panel_state_cache = {}
    harness._panel_state_signature = ""
    harness.user_default_template = {}
    harness._session_render_enabled = {}
    harness._kv = {}
    harness._bg_asset_cache = {}
    harness._bg_image_size = None
    harness._bg_round_robin_index = 0
    harness._tpl_round_robin_index = 0
    harness._pending_delete_tasks = set()
    harness.gif_duration = HtmlRenderPlugin._coerce_float(
        harness.config.get("gif_duration", 3.0),
        3.0,
        minimum=0.1,
    )
    harness.gif_fps = HtmlRenderPlugin._coerce_int(
        harness.config.get("gif_fps", 15),
        15,
        minimum=1,
    )

    async def _get_kv_data(self, key, default):
        return self._kv.get(key, default)

    async def _put_kv_data(self, key, value):
        self._kv[key] = value

    async def _delete_kv_data(self, key):
        self._kv.pop(key, None)

    harness.get_kv_data = types.MethodType(_get_kv_data, harness)
    harness.put_kv_data = types.MethodType(_put_kv_data, harness)
    harness.delete_kv_data = types.MethodType(_delete_kv_data, harness)
    return harness


async def _check_process_text_merge() -> None:
    from text_processing import detect_render_tag

    harness = _make_harness()
    calls = []

    async def fake_render(content, specified_template, user_id=None, is_gif=False, extra_vars=None):
        calls.append((content, specified_template, user_id, is_gif))
        return f"image:{specified_template}"

    harness._render_content = fake_render
    text = (
        "前言\n"
        "<render template=\"暖窗手札\">第一段</render>\n"
        "<render template=\"暖窗手札\">第二段</render>\n"
        "尾声"
    )
    comps = await harness._process_text(text, "user-a")

    assert comps == ["image:暖窗手札"]
    assert len(calls) == 1
    assert calls[0][1] == "暖窗手札"
    assert "第一段" in calls[0][0] and "第二段" in calls[0][0]
    assert not detect_render_tag(calls[0][0])


def main() -> int:
    _install_astrbot_stubs()

    from main import HtmlRenderPlugin
    from template_manager import TemplateManager
    from text_processing import detect_dialogue, detect_render_tag

    assert detect_render_tag("<render template=\"暖窗手札\" gif>hello</render>") == [
        ("<render template=\"暖窗手札\" gif>hello</render>", "暖窗手札", "hello", True)
    ]
    assert detect_render_tag("<render template=\\\"魔女特辑\\\">hello</render>")[0][1] == "魔女特辑"
    assert detect_dialogue("'hello'")
    assert detect_dialogue("hello", "[") is False
    assert detect_dialogue("'hello' and “world”", quote_threshold=1)
    assert detect_dialogue("'hello'", quote_threshold="1")
    assert detect_dialogue("'hello'", quote_threshold="bad")

    harness = _make_harness()
    same_tpl = detect_render_tag(
        "<render template=\"暖窗手札\">a</render><render template=\"暖窗手札\">b</render>"
    )
    mixed_tpl = detect_render_tag(
        "<render template=\"暖窗手札\">a</render><render template=\"玻璃花房\">b</render>"
    )
    assert harness._get_merged_template("user-a", same_tpl) == "暖窗手札"
    assert harness._get_merged_template("user-a", mixed_tpl) == "玻璃花房"
    forced_merge_harness = _make_harness(
        _Config(
            {
                "default_template": "玻璃花房",
                "merged_template": "魔女特辑",
            },
            schema={},
        )
    )
    explicit_same_tpl = detect_render_tag(
        "<render template=\"手账\">a</render><render template=\"手账\">b</render>"
    )
    assert forced_merge_harness._get_merged_template("user-a", explicit_same_tpl) == "手账"
    missing_tpl = detect_render_tag(
        "<render template=\"missing-template\">a</render><render template=\"missing-template\">b</render>"
    )
    assert harness._get_merged_template("user-a", missing_tpl) == "玻璃花房"
    assert harness.config.get("dialogue_quote_threshold", 1) == 1
    assert harness._is_auto_render_all_enabled() is True
    assert harness._get_background_opacity("ambient") == 0.17
    assert harness._get_background_opacity("watermark") == 0.17
    assert harness._get_gif_scale() == 2
    invalid_dialogue = _make_harness(
        _Config(
            {
                "default_template": "玻璃花房",
                "auto_render_all": True,
                "auto_dialogue_detection": True,
                "dialogue_quote_pattern": "[",
            },
            schema={},
        )
    )
    assert invalid_dialogue._select_template("'hello'", None, "user-a") == "玻璃花房"
    assert _make_harness(_Config({"gif_scale": "bad"}, schema={}))._get_gif_scale() == 2
    string_number_config = _Config(
        {
            "default_template": "玻璃花房",
            "auto_render_min_length": "7",
            "dialogue_quote_threshold": "2",
            "gif_scale": "3",
            "gif_duration": "2.5",
            "gif_fps": "12",
            "background_opacity": "0.22",
        },
        schema={},
    )
    string_number_harness = _make_harness(string_number_config)
    assert string_number_harness._get_auto_render_min_length() == 7
    assert string_number_harness._get_gif_scale() == 3
    assert string_number_harness.gif_duration == 2.5
    assert string_number_harness.gif_fps == 12
    assert string_number_harness._get_background_opacity("ambient") == 0.22
    assert string_number_harness._select_template("'a' and 'b'", None, "user-a") == "玻璃花房"

    missing_defaults = _make_harness(_Config({}, schema={}))
    assert missing_defaults._is_auto_render_all_enabled() is True
    assert missing_defaults._get_background_opacity("ambient") == 0.17
    assert missing_defaults._get_gif_scale() == 2

    disabled = _make_harness(_Config({"auto_render_all": False}, schema={}))
    assert disabled._is_auto_render_all_enabled() is False

    session_harness = _make_harness()
    group_event = _SessionEvent("qq", "12345", "10001", origin="qq:group:12345")
    private_event = _SessionEvent("qq", "", "10001", origin="qq:private:10001")
    fallback_event = _SessionEvent("", "", "", origin="fallback-origin")

    assert session_harness._get_session_scope(group_event) == (
        "group::qq::12345",
        "群聊 12345 (qq)",
    )
    assert session_harness._get_session_scope(private_event) == (
        "private::qq::10001",
        "私聊 10001 (qq)",
    )
    assert session_harness._get_session_scope(fallback_event) == (
        "session::fallback-origin",
        "fallback-origin",
    )

    asyncio.run(session_harness._set_session_render_enabled(group_event, True))
    assert asyncio.run(session_harness._get_session_render_override(group_event)) is True
    status = asyncio.run(session_harness._build_render_status_message(group_event))
    assert "当前状态: 开启" in status
    assert "会话覆盖: 开启" in status
    assert "命令: /html开" in status
    asyncio.run(session_harness._set_session_render_enabled(group_event, None))
    assert asyncio.run(session_harness._get_session_render_override(group_event)) is None

    async def _collect_text(gen):
        out = []
        async for item in gen:
            out.append(item)
        return out

    on_items = asyncio.run(_collect_text(session_harness.cmd_html_on(group_event)))
    assert any("已开启当前会话的 HTML 渲染" in item for item in on_items)
    off_items = asyncio.run(_collect_text(session_harness.cmd_html_off(group_event)))
    assert any("已关闭当前会话的 HTML 渲染" in item for item in off_items)
    status_items = asyncio.run(_collect_text(session_harness.cmd_html_status(group_event)))
    assert any("HTML 渲染状态" in item for item in status_items)
    reset_items = asyncio.run(_collect_text(session_harness.cmd_html_reset(group_event)))
    assert any("已恢复默认 HTML 渲染状态" in item for item in reset_items)
    status_after_reset = asyncio.run(_collect_text(session_harness.cmd_html_status(group_event)))
    assert any("会话覆盖: 无" in item for item in status_after_reset)

    switch_event = _SessionEvent("qq", "", "10001", origin="qq:private:10001")
    switch_event.message_str = "/切换 1"
    session_harness.template_mgr.template_id_map = {}
    switch_items = asyncio.run(_collect_text(session_harness.cmd_switch_template(switch_event)))
    assert any("已切换默认模板为" in item for item in switch_items)
    assert session_harness.user_default_template.get("10001") == session_harness.template_mgr.get_available_templates()[0]

    normalized = HtmlRenderPlugin._normalize_render_source_text(
        "<think>secret</think><pic prompt=\"draw\"></pic><ctx>keep</ctx>"
    )
    assert normalized == "keep"
    assert HtmlRenderPlugin._is_trivial_render_payload("。！？")
    assert not HtmlRenderPlugin._is_trivial_render_payload("正文")

    event = _DummyEvent()
    cleaned = harness._clean_assistant_history_text(
        event,
        "<render>hello</render><pic prompt=\"draw\"></pic><think>secret</think><ctx>ctx</ctx>",
    )
    assert "hello" in cleaned and "ctx" in cleaned
    assert "<render" not in cleaned and "<pic" not in cleaned and "<think" not in cleaned
    panel_cleaned = harness._clean_assistant_history_text(
        event,
        "正文\n<panel id=\"r18_status\" update>{\"affection\":80}</panel>",
    )
    assert panel_cleaned == "正文"

    prompt = HtmlRenderPlugin._prepend_prompt_before_chatroom_history(
        "alpha\nYou are now in a chatroom. The chat history is as follows:\nbeta",
        "inject",
    )
    assert "alpha\n\ninject\n\nYou are now in a chatroom" in prompt

    harness._refresh_template_schema_options()
    assert "暖窗手札" in harness.config.schema["merged_template"]["options"]

    templates = TemplateManager(str(REPO_ROOT / "templates")).get_available_templates()
    assert "暖窗手札" in templates
    glass_palette = TemplateManager(str(REPO_ROOT / "templates")).extract_color_palette(
        "玻璃花房"
    )
    assert glass_palette is not None
    assert glass_palette["primary"] == "#2e9e8f"
    assert glass_palette["tone"] == "青绿色调"
    assert "#7f74c9" in glass_palette["colors"]
    schema = json.loads((REPO_ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    assert schema["dialogue_quote_pattern"]["default"] == "[\"'“”‘’「」『』]"
    avatar_output_schema = schema["sub_html_panels"]["templates"]["avatar_dialogue"]["items"]["output_mode"]
    assert avatar_output_schema["options"] == ["when_relevant", "always"]

    # ==================== 子 HTML 面板 ====================
    from sub_html_panels import SubHtmlPanelManager

    panel_items = schema["sub_html_panels"]["default"]
    panel_mgr = SubHtmlPanelManager(str(REPO_ROOT))
    panel_mgr.reload(panel_items)
    enabled_panels = panel_mgr.get_enabled_panels()
    assert len(enabled_panels) == 2
    assert enabled_panels[0].panel_id == "r18_status"
    avatar_panel = next(panel for panel in enabled_panels if panel.panel_id == "avatar_dialogue")
    assert avatar_panel.panel_mode == "inline"
    panel_prompt = panel_mgr.build_prompt_block()
    assert "输出时机为“每次回复”的面板必须在每轮回复中输出" in panel_prompt
    assert "行内子模板（可重复、无状态、原地插入）" in panel_prompt
    assert "头像对话" in panel_prompt
    assert "punishment=惩罚模式" in panel_prompt
    assert "theme_color" in panel_prompt
    themed_panel_prompt = panel_mgr.build_prompt_block(theme_palette=glass_palette)
    assert "当前主模板：玻璃花房" in themed_panel_prompt
    assert "最匹配的色调：青绿色调" in themed_panel_prompt
    assert "优先使用推荐主题色" in themed_panel_prompt
    assert '"theme_color":"#2e9e8f"' in themed_panel_prompt
    always_items = json.loads(json.dumps(panel_items, ensure_ascii=False))
    always_items[0]["output_mode"] = "always"
    panel_mgr.reload(always_items)
    panel_prompt = panel_mgr.build_prompt_block()
    assert "每次回复都必须输出" in panel_prompt
    assert "update>{}</panel>" in panel_prompt
    assert [panel.panel_id for panel in panel_mgr.get_missing_always_panels("正文")] == [
        "r18_status"
    ]
    assert panel_mgr.get_missing_always_panels(
        '<panel id="r18_status" update>{}</panel>'
    ) == []
    assert panel_mgr.get_missing_always_panels(
        '<panel id="r18_status" update>{bad json}</panel>'
    )[0].panel_id == "r18_status"
    extracted_tags = panel_mgr.extract_valid_panel_tags(
        '解释文字<panel id="r18_status" update>{"affection":81}</panel>',
        ["r18_status"],
    )
    assert extracted_tags["r18_status"].endswith("</panel>")

    expanded = panel_mgr.expand_panels(
        '<panel id="r18_status">'
        '{"mode":"rescue","character":"<img src=x onerror=alert(1)>",'
        '"theme_color":"#39c7a4","affection":140,"arousal":"bad","current_goal":"保持清醒",'
        '"feet":"赤足"}'
        '</panel>'
    )
    assert 'data-mode="rescue"' in expanded
    assert "--r18-user-accent: #39c7a4" in expanded
    assert "救赎模式" in expanded
    assert "&lt;img src=x onerror=alert(1)&gt;" in expanded
    assert "<img src=x onerror=alert(1)>" not in expanded
    assert "--r18-value: 100%" in expanded
    assert "保持清醒" in expanded and "赤足" in expanded
    assert "{{value:" not in expanded and "{{percent:" not in expanded
    assert panel_mgr.expand_panels('<panel id="disabled">{"x":1}</panel>') == ""
    inline_state = {}
    inline_expanded = panel_mgr.expand_panels(
        '开场<panel id="avatar_dialogue">'
        '{"avatar":"女主.png","name":"苏雪","speech":"第一句","inner":"她有些紧张"}'
        '</panel>中段<panel id="avatar_dialogue">'
        '{"avatar":"男主.jpg","name":"林默","speech":"第二句","inner":""}'
        '</panel>结尾',
        state_store=inline_state,
    )
    assert inline_expanded.index("苏雪") < inline_expanded.index("中段")
    assert inline_expanded.index("中段") < inline_expanded.index("林默")
    assert "第一句" in inline_expanded and "第二句" in inline_expanded
    assert "avatar_dialogue" not in inline_state
    assert 'class="astr-avatar-dialogue__inner"></div>' in inline_expanded
    whitespace_inner = panel_mgr.expand_panels(
        '<panel id="avatar_dialogue">{"name":"林默","speech":"只有台词",'
        '"inner":"   "}</panel>'
    )
    assert 'class="astr-avatar-dialogue__inner"></div>' in whitespace_inner
    assert "../" not in panel_mgr._resolve_image_data_url("../images/not-allowed.png")

    inline_mgr = SubHtmlPanelManager(str(REPO_ROOT))
    avatar_item = next(item for item in panel_items if item.get("panel_id") == "avatar_dialogue")
    inline_mgr.reload(
        [
            avatar_item,
            {
                "enabled": True,
                "name": "行内提示",
                "panel_id": "inline_note",
                "panel_mode": "inline",
                "output_mode": "when_relevant",
                "fields": ["content|内容|text|提示内容"],
                "empty_value": "",
                "html_template": '<div class="inline-note">{{value:content}}</div>',
            },
        ]
    )
    mixed_inline = inline_mgr.expand_panels(
        'A<panel id="avatar_dialogue">{"name":"甲","speech":"一"}</panel>'
        'B<panel id="inline_note">{"content":"中间提示"}</panel>'
        'C<panel id="avatar_dialogue">{"name":"乙","speech":"二"}</panel>D'
    )
    assert mixed_inline.index("甲") < mixed_inline.index("中间提示") < mixed_inline.index("乙")
    assert mixed_inline.startswith("A") and mixed_inline.endswith("D")
    invalid_color = panel_mgr.expand_panels(
        '<panel id="r18_status">{"mode":"punishment","theme_color":"red"}</panel>'
    )
    assert "--r18-user-accent: #a92e3b" in invalid_color

    state = {}
    collapsed = panel_mgr.expand_panels(
        '<panel id="r18_status">{"mode":"punishment","theme_color":"#aa3344",'
        '"character":"旧状态","affection":20}</panel>\n'
        '<panel id="r18_status" update>{"character":"新状态","affection":80}</panel>',
        state_store=state,
    )
    assert "旧状态" not in collapsed
    assert "新状态" in collapsed
    assert state["r18_status"]["theme_color"] == "#aa3344"
    assert state["r18_status"]["affection"] == 80
    retained = panel_mgr.expand_panels(
        '<panel id="r18_status" update>{"feet":"赤足"}</panel>',
        state_store=state,
    )
    assert "新状态" in retained and "赤足" in retained
    assert state["r18_status"]["affection"] == 80
    assert panel_mgr.strip_panel_markup("正文\n<panel id=\"r18_status\" update>{\"feet\":\"赤足\"}</panel>") == "正文"

    original_signature = panel_mgr.configuration_signature
    original_state_signature = panel_mgr.state_signature
    presentation_changed_items = json.loads(json.dumps(panel_items, ensure_ascii=False))
    presentation_changed_items[0]["output_mode"] = "always"
    presentation_changed_items[0]["name"] = "仅修改展示名称"
    panel_mgr.reload(presentation_changed_items)
    assert panel_mgr.configuration_signature != original_signature
    assert panel_mgr.state_signature == original_state_signature

    changed_items = json.loads(json.dumps(panel_items, ensure_ascii=False))
    changed_items[0]["fields"].append("extra|额外|text|配置变更后的新字段")
    panel_mgr.reload(changed_items)
    assert panel_mgr.configuration_signature != original_signature
    assert panel_mgr.state_signature != original_state_signature

    panel_config = _Config(
        {
            "inject_prompt": False,
            "sub_html_panels": panel_items,
            "default_template": "玻璃花房",
        },
        schema={},
    )
    panel_harness = _make_harness(panel_config)
    panel_event = _SessionEvent("qq", "", "10001", origin="qq:private:10001")
    assert panel_harness._has_explicit_render_request(
        '<panel id="r18_status">{"mode":"punishment"}</panel>'
    )
    assert panel_harness._should_use_original_text_for_render(
        '<panel id="r18_status">{"mode":"rescue"}</panel>',
        ["普通文本"],
    )
    req = types.SimpleNamespace(system_prompt="原始系统提示词")
    asyncio.run(panel_harness.on_llm_req(panel_event, req))
    assert "子 HTML 面板数据协议" in req.system_prompt
    assert "## HTML 渲染功能" not in req.system_prompt
    assert "当前主模板：玻璃花房" in req.system_prompt
    assert "推荐面板主题色：#2e9e8f" in req.system_prompt

    context_req = types.SimpleNamespace(
        system_prompt="原始系统提示词",
        contexts=[
            {
                "role": "assistant",
                "content": "正文\n<panel id=\"r18_status\" update>{\"affection\":80}</panel>",
            }
        ],
    )
    asyncio.run(panel_harness.on_llm_req(panel_event, context_req))
    assert "panel" not in context_req.contexts[0]["content"]
    assert context_req.contexts[0]["content"] == "正文"

    inserted_panel = panel_harness._insert_panel_tags(
        "<render>正文</render>",
        ['<panel id="r18_status" update>{}</panel>'],
    )
    assert inserted_panel.index("<panel") < inserted_panel.index("</render>")

    class _PanelProvider:
        def __init__(self, completion_text):
            self.completion_text = completion_text
            self.calls = []

        async def text_chat(self, **kwargs):
            self.calls.append(kwargs)
            return types.SimpleNamespace(completion_text=self.completion_text)

    always_config = _Config(
        {
            "inject_prompt": False,
            "sub_html_panels": always_items,
            "default_template": "玻璃花房",
        },
        schema={},
    )
    supplement_harness = _make_harness(always_config)
    supplement_provider = _PanelProvider(
        '<panel id="r18_status" update>{"affection":88}</panel>'
    )
    supplement_harness.context = _DummyContext(supplement_provider)
    supplement_event = _SessionEvent(
        "qq",
        "",
        "10001",
        origin="qq:private:panel-supplement",
    )
    supplement_harness._panel_state_cache[supplement_event.unified_msg_origin] = {
        "r18_status": {"character": "旧状态", "affection": 80}
    }
    supplemented_text, supplemented_tags = asyncio.run(
        supplement_harness._supplement_missing_always_panels(
            supplement_event,
            "<render>正文</render>",
        )
    )
    assert len(supplement_provider.calls) == 1
    assert '"affection":88' in supplemented_text
    assert "推荐面板主题色：#2e9e8f" in supplement_provider.calls[0]["system_prompt"]
    assert supplemented_text.index("<panel") < supplemented_text.index("</render>")
    assert supplemented_tags == [
        '<panel id="r18_status" update>{"affection":88}</panel>'
    ]
    unchanged_text, unchanged_tags = asyncio.run(
        supplement_harness._supplement_missing_always_panels(
            supplement_event,
            supplemented_text,
        )
    )
    assert unchanged_text == supplemented_text
    assert unchanged_tags == []
    assert len(supplement_provider.calls) == 1

    invalid_provider = _PanelProvider("没有按协议输出")
    supplement_harness.context = _DummyContext(invalid_provider)
    fallback_text, fallback_tags = asyncio.run(
        supplement_harness._supplement_missing_always_panels(
            supplement_event,
            "普通正文",
        )
    )
    assert len(invalid_provider.calls) == 1
    assert fallback_tags == ['<panel id="r18_status" update>{}</panel>']
    assert fallback_text.endswith('<panel id="r18_status" update>{}</panel>')

    empty_response = types.SimpleNamespace(completion_text="", result_chain=None)
    empty_event = _SessionEvent(
        "qq",
        "",
        "10001",
        origin="qq:private:panel-empty-response",
    )
    asyncio.run(supplement_harness.on_llm_response(empty_event, empty_response))
    assert '<panel id="r18_status" update>{}</panel>' in empty_response.completion_text
    assert empty_response.result_chain.chain

    legacy_scope = "legacy-scope"
    legacy_key = panel_harness._legacy_panel_state_kv_key(legacy_scope)
    stable_key = panel_harness._panel_state_kv_key(legacy_scope)
    assert legacy_key != stable_key
    panel_harness._kv[legacy_key] = {
        "panels": {"r18_status": {"character": "旧版状态", "affection": 70}}
    }
    migrated_state = asyncio.run(panel_harness._get_panel_state(legacy_scope))
    assert migrated_state["r18_status"]["character"] == "旧版状态"
    assert panel_harness._kv[stable_key] == {"panels": migrated_state}

    panel_harness._panel_state_cache["scope-a"] = {
        "r18_status": {"character": "旧状态", "affection": 80}
    }
    state_signature = panel_harness._panel_state_signature
    panel_harness.config["sub_html_panels"] = presentation_changed_items
    panel_harness._refresh_sub_html_panels()
    assert panel_harness._panel_state_signature == state_signature
    retained_state = panel_harness._panel_state_cache["scope-a"]
    retained = panel_harness.sub_panel_mgr.expand_panels(
        '<panel id="r18_status" update>{"affection":90}</panel>',
        state_store=retained_state,
    )
    assert "旧状态" in retained
    assert retained_state["r18_status"]["affection"] == 90

    panel_harness.config["sub_html_panels"] = changed_items
    panel_harness._refresh_sub_html_panels()
    assert panel_harness._panel_state_cache == {}

    # 禁用单个有状态面板时，其他面板状态应保持；重新启用后旧状态不得复活。
    second_state_item = {
        "enabled": True,
        "name": "第二状态面板",
        "panel_id": "second_status",
        "panel_mode": "stateful",
        "output_mode": "when_relevant",
        "fields": ["value|值|text|状态值"],
        "empty_value": "",
        "html_template": '<div>{{value:value}}</div>',
    }
    state_items = json.loads(json.dumps(panel_items, ensure_ascii=False))
    state_items.append(second_state_item)
    state_config = _Config({"sub_html_panels": state_items}, schema={})
    state_harness = _make_harness(state_config)
    state_harness._refresh_sub_html_panels()
    state_scope = "state-isolation"
    asyncio.run(
        state_harness._save_panel_state(
            state_scope,
            {
                "r18_status": {"character": "旧状态"},
                "second_status": {"value": "仍保留"},
            },
        )
    )
    disabled_items = json.loads(json.dumps(state_items, ensure_ascii=False))
    disabled_items[0]["enabled"] = False
    state_harness.config["sub_html_panels"] = disabled_items
    state_harness._refresh_sub_html_panels()
    assert state_harness._panel_state_cache[state_scope] == {
        "second_status": {"value": "仍保留"}
    }
    state_harness.config["sub_html_panels"] = state_items
    state_harness._refresh_sub_html_panels()
    state_harness._panel_state_cache.clear()
    reenabled_state = asyncio.run(state_harness._get_panel_state(state_scope))
    assert "r18_status" not in reenabled_state
    assert reenabled_state["second_status"]["value"] == "仍保留"

    # ==================== 模板文件完整性 ====================
    for tpl_name in templates:
        raw = (REPO_ROOT / "templates" / f"{tpl_name}.html").read_text(encoding="utf-8")
        stripped = TemplateManager.strip_builtin_prompt(raw)
        assert "{{content}}" in stripped, f"模板缺少 {{{{content}}}} 占位符: {tpl_name}"

    cat_panel_html = (REPO_ROOT / "sub_html_panels" / "甜心猫咪状态面板.html").read_text(
        encoding="utf-8"
    )
    assert cat_panel_html.count('class="kitty-reminder__text"') == 4
    assert ".kitty-status-panel .kitty-reminders li::before" in cat_panel_html
    assert "position: static;" in cat_panel_html
    assert "{{value:avatar}}" not in cat_panel_html
    assert "{{value:age}}" not in cat_panel_html
    removed_cat_fields = {
        "name",
        "role",
        "dependence",
        "trust",
        "possessiveness",
        "curiosity",
        "relationship",
        "topic",
        "time",
        "interaction",
        "location",
        "environment",
    }
    assert all("{{value:" + key + "}}" not in cat_panel_html for key in removed_cat_fields)
    assert cat_panel_html.count('class="kitty-meter"') == 2

    intimacy_panel_html = (REPO_ROOT / "sub_html_panels" / "私密经验记录.html").read_text(
        encoding="utf-8"
    )
    intimacy_template_fields = {
        "character",
        "theme_color",
        "corruption_level",
        "corruption_exp",
        "virginity",
        "partner_count",
        "masturbation_count",
        "service_count",
        "anal_count",
        "prostitution_count",
        "shame_count",
        "intercourse_count",
        "creampie_count",
        "group_count",
        "mouth_state",
        "chest_state",
        "intimate_state",
        "hips_state",
    }
    assert all(
        "{{value:" + key + "}}" in intimacy_panel_html for key in intimacy_template_fields - {"theme_color"}
    )
    assert "{{color:theme_color}}" in intimacy_panel_html
    assert all(
        forbidden not in intimacy_panel_html
        for forbidden in ("圣性", "妊娠", "露出抗性", "孩子数量")
    )
    intimacy_config = next(
        item for item in schema["sub_html_panels"]["default"] if item.get("panel_id") == "intimacy_record"
    )
    assert intimacy_config["enabled"] is False
    assert len(intimacy_config["fields"]) == len(intimacy_template_fields)
    intimacy_mgr = SubHtmlPanelManager(str(REPO_ROOT))
    intimacy_runtime_config = json.loads(json.dumps(intimacy_config, ensure_ascii=False))
    intimacy_runtime_config["enabled"] = True
    intimacy_mgr.reload([intimacy_runtime_config])
    expanded_intimacy = intimacy_mgr.expand_panels(
        '<panel id="intimacy_record">'
        '{"theme_color":"#a84e9f","character":"测试角色","corruption_level":3,'
        '"corruption_exp":24,"virginity":"未记录","partner_count":2,'
        '"masturbation_count":1,"service_count":2,"anal_count":0,'
        '"prostitution_count":0,"shame_count":4,"intercourse_count":3,'
        '"creampie_count":1,"group_count":0,"mouth_state":"简短状态",'
        '"chest_state":"胸部状态","intimate_state":"私处状态","hips_state":"臀部状态"}'
        '</panel>'
    )
    assert "测试角色" in expanded_intimacy
    assert "--ir-user-accent: #a84e9f" in expanded_intimacy
    assert "简短状态" in expanded_intimacy
    assert "{{value:" not in expanded_intimacy

    # ==================== 模板选择策略 ====================
    assert schema["template_strategy"]["default"] == "fixed"

    strategy_harness = _make_harness(_Config({"template_strategy": "round_robin"}, schema={}))
    available = strategy_harness._get_available_templates()
    assert len(available) >= 2
    first = strategy_harness._get_default_template("u", for_render=True)
    second = strategy_harness._get_default_template("u", for_render=True)
    assert first == available[0]
    assert second == available[1 % len(available)]
    # 非渲染路径（提示词注入等）不受策略影响，保持稳定
    assert strategy_harness._get_default_template("u") == available[0]
    # 用户个人模板优先于策略
    strategy_harness.user_default_template["u"] = available[-1]
    assert strategy_harness._get_default_template("u", for_render=True) == available[-1]

    random_harness = _make_harness(_Config({"template_strategy": "random"}, schema={}))
    assert random_harness._get_default_template("u", for_render=True) in available
    assert _make_harness(_Config({"template_strategy": "bogus"}, schema={}))._get_template_strategy() == "fixed"
    assert _make_harness(_Config({}, schema={}))._get_template_strategy() == "fixed"

    # ==================== 模板变量 ====================
    vars_harness = _make_harness()
    tvars = vars_harness._build_template_vars(None)
    assert {"date", "time", "weekday", "sender_name", "sender_id", "platform"} <= set(tvars)
    assert tvars["sender_name"] == "朋友"
    filled = vars_harness._fill_template_vars(
        "A{{sender_name}}B{{date}}C{{unknown}}",
        {"sender_name": "测试用户"},
    )
    assert "A测试用户B" in filled
    assert "{{date}}" not in filled
    assert "{{unknown}}" in filled  # 未知占位符保持原样

    event_vars = vars_harness._build_template_vars(
        _SessionEvent("qq", "", "10001", origin="qq:private:10001")
    )
    assert event_vars["platform"] == "qq"
    assert event_vars["sender_id"] == "10001"

    asyncio.run(_check_process_text_merge())
    print("html_render self_check ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
