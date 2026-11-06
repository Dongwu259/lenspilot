"""
skill_loader.py — Skills system for the AI Agent.

Skills are reusable workflow templates stored as SKILL.md files under
<user_data_dir>/skills/<skill-name>/SKILL.md.  Each file has YAML frontmatter
(name, description, trigger regex, optional model/auto_approve overrides)
followed by a markdown body that is injected into the system prompt when
the skill is activated (either manually via the activate_skill tool or
automatically when a user message matches the trigger regex).

No external dependencies beyond stdlib — the YAML frontmatter parser is
a lightweight regex-based implementation that handles the subset we need.
"""

import os
import re

from service import send_log


# ── Skill dataclass ──

class Skill:
    __slots__ = ("name", "description", "trigger", "model", "auto_approve",
                 "work_folder", "body", "imports", "allow_tools", "_trigger_re")

    def __init__(self, name="", description="", trigger="", model=None,
                 auto_approve=None, work_folder=None, body="",
                 imports=None, allow_tools=None):
        self.name = name
        self.description = description
        self.trigger = trigger
        self.model = model
        self.auto_approve = auto_approve   # None = use session default
        self.work_folder = work_folder     # None = use session default
        self.body = body
        self.imports = imports or []       # list of skill names to import
        self.allow_tools = allow_tools     # list of tool prefixes, None = all allowed
        self._trigger_re = re.compile(trigger, re.IGNORECASE) if trigger else None

    def matches(self, text):
        """Return True if `text` matches this skill's trigger regex."""
        if not self._trigger_re:
            return False
        return bool(self._trigger_re.search(text))

    def system_message(self, loader=None):
        """Return a system-role message dict to inject into the conversation.
        If loader is provided, imported skills are resolved and included."""
        content = f"[已激活技能: {self.name}]\n\n{self.body}"
        if self.imports and loader:
            for imp_name in self.imports:
                imp = loader.skills.get(imp_name)
                if imp:
                    content += f"\n\n---\n[导入技能: {imp.name}]\n{imp.body}"
        return {"role": "system", "content": content}

    def to_dict(self):
        return {
            "name": self.name,
            "description": self.description,
            "trigger": self.trigger,
            "model": self.model,
            "auto_approve": self.auto_approve,
            "work_folder": self.work_folder,
            "body": self.body,
        }


# ── YAML frontmatter parser (stdlib only) ──

_FRONTMATTER_RE = re.compile(r'^---\s*\n(.*?)\n---\s*\n', re.DOTALL)


def _parse_skill(path):
    """Parse a SKILL.md file.  Returns a Skill or None on failure."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read()
    except Exception as e:
        send_log(f"[skill_loader] read error {path}: {e}", "warn")
        return None

    m = _FRONTMATTER_RE.match(raw)
    if not m:
        # No frontmatter — treat the entire file as the body, use dirname as name
        name = os.path.basename(os.path.dirname(path))
        return Skill(name=name, description="", body=raw.strip())

    fm_text = m.group(1)
    body = raw[m.end():].strip()

    # Parse minimal YAML (key: value pairs, no nesting needed for our subset)
    meta = {}
    for line in fm_text.split("\n"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" in line:
            key, _, val = line.partition(":")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            meta[key] = val

    imports = []
    if meta.get("imports"):
        imports = [s.strip() for s in meta["imports"].split(",") if s.strip()]
    allow_tools = None
    if meta.get("allow_tools"):
        allow_tools = [s.strip() for s in meta["allow_tools"].split(",") if s.strip()]

    return Skill(
        name=meta.get("name", os.path.basename(os.path.dirname(path))),
        description=meta.get("description", ""),
        trigger=meta.get("trigger", ""),
        model=meta.get("model") or None,
        auto_approve=True if meta.get("auto_approve", "").lower() == "true" else None,
        work_folder=meta.get("work_folder") or None,
        body=body,
        imports=imports,
        allow_tools=allow_tools,
    )


def _format_skill(path, skill):
    """Write a Skill back to a SKILL.md file."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    lines = ["---"]
    lines.append(f"name: {skill.name}")
    if skill.description:
        lines.append(f"description: {skill.description}")
    if skill.trigger:
        lines.append(f"trigger: {skill.trigger}")
    if skill.model:
        lines.append(f"model: {skill.model}")
    if skill.auto_approve is not None:
        lines.append(f"auto_approve: {'true' if skill.auto_approve else 'false'}")
    if skill.work_folder:
        lines.append(f"work_folder: {skill.work_folder}")
    if skill.imports:
        lines.append(f"imports: {', '.join(skill.imports)}")
    if skill.allow_tools:
        lines.append(f"allow_tools: {', '.join(skill.allow_tools)}")
    lines.append("---")
    lines.append("")
    lines.append(skill.body)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


# ── SkillLoader ──

class SkillLoader:
    def __init__(self, skills_dir=None):
        if skills_dir is None:
            from service import _get_config_dir
            skills_dir = os.path.join(_get_config_dir(), "skills")
        self.skills_dir = skills_dir
        self.skills = {}  # name -> Skill

    # ── Scan ──

    def scan(self):
        """(Re)scan the skills directory.  Safe to call multiple times."""
        self.skills.clear()
        try:
            os.makedirs(self.skills_dir, exist_ok=True)
        except Exception:
            return
        try:
            for entry in sorted(os.listdir(self.skills_dir)):
                dir_path = os.path.join(self.skills_dir, entry)
                if not os.path.isdir(dir_path):
                    continue
                md_path = os.path.join(dir_path, "SKILL.md")
                if os.path.isfile(md_path):
                    skill = _parse_skill(md_path)
                    if skill:
                        self.skills[skill.name] = skill
        except Exception as e:
            send_log(f"[skill_loader] scan error: {e}", "warn")

    # ── Trigger matching ──

    def match(self, text):
        """Return the first skill whose trigger regex matches `text`, or None."""
        for skill in self.skills.values():
            if skill.matches(text):
                return skill
        return None

    # ── Prompt list for system message ──

    def get_prompt_list(self):
        """Return a compact string for injection into the system prompt."""
        if not self.skills:
            return ""
        lines = ["", "## 可用技能 (Skills)", ""]
        for name, skill in sorted(self.skills.items()):
            lines.append(f"- **{name}**: {skill.description or '(无描述)'}")
        return "\n".join(lines)

    # ── CRUD (for IPC handlers) ──

    def list_all(self):
        return [s.to_dict() for s in sorted(self.skills.values(), key=lambda s: s.name)]

    def get(self, name):
        return self.skills.get(name)

    def save(self, name, description="", trigger="", model=None,
             auto_approve=None, work_folder=None, body="",
             imports=None, allow_tools=None):
        dir_path = os.path.join(self.skills_dir, name)
        md_path = os.path.join(dir_path, "SKILL.md")
        skill = Skill(name=name, description=description, trigger=trigger,
                      model=model, auto_approve=auto_approve,
                      work_folder=work_folder, body=body,
                      imports=imports, allow_tools=allow_tools)
        _format_skill(md_path, skill)
        self.skills[name] = skill
        return skill

    def delete(self, name):
        import shutil
        dir_path = os.path.join(self.skills_dir, name)
        if os.path.isdir(dir_path):
            shutil.rmtree(dir_path)
        self.skills.pop(name, None)
        return True

    # ── 内置技能种子（首次/缺失时写入，幂等，不覆盖用户已有技能）──

    _BUILTIN_SKILLS = {
        "delivery-workflow": {
            "description": "一键处理整场拍摄：粗筛→LR 修图→终选→风格化→多尺寸→压缩→上传→客户预览→交付报告。对『处理这场婚礼/选片交付/出片』等指令自动触发。",
            "trigger": r"(交付|选片|出片|修图|处理.*(婚礼|拍摄|活动|外景|写真)|成片|最终交付)",
            "auto_approve": False,
            "body": (
                "用户要交付整场拍摄。按专业交付工作流执行：\n"
                "1. 先确保工作文件夹已设置（set_work_folder），若用户给了拍摄路径先设置。\n"
                "2. 用 pipeline_list_templates 查看模板；内置模板 lr_two_round 就是粗筛→LR→终选→交付。"
                "  pipeline_start_run 启动（folder 传工作文件夹），模板里的破坏性步骤会自动弹门等用户批准，不需要逐步确认。\n"
                "3. 若要给交付步骤配参（如交付统一色调、多尺寸、客户预览），用 pipeline_generate_template 生成带 step.params 的草稿，"
                "  给用户看过后 pipeline_save_template 保存，再 pipeline_start_run 启动该模板。常见步骤参数：\n"
                "   - auto_retouch_keepers.params.strength = 0-1 调色强度(默认1)；engine=auto 走 AI 模型(权重未下载自动降级规则层)，engine=suggest 强制规则层\n"
                "   - grade_keepers.params.lut_file = 交付统一色调的 .cube 路径（用户给或选一个）\n"
                "   - multi_size_keepers.params.sizes = 例如 thumb:480,screen:1920\n"
                "   - build_customer_preview.params.mode = gallery 或 contact_sheet\n"
                "   - compress_keepers.params.target_size = 上传体积上限 MB\n"
                "4. 跑完后用 pipeline_get_status 查进度，pipeline_cancel_run 可取消。\n"
                "5. 如需额外批量处理（压缩/格式转换/降噪等），用内置 photo_s 工具（mcp__photo_s__process 等），只传工作文件夹内路径。\n"
                "6. 向用户汇报产物位置：_风格化交付/、_多尺寸交付/、客户预览/、交付报告.csv、云端相册链接。"
            ),
        },
        "photo-archive": {
            "description": "批量打标归档，建立可搜索照片库：给照片批量写评分/关键词/标题（EXIF），可配合移动/整理。对『归档/打标/建库/整理照片/加关键词』等指令自动触发。",
            "trigger": r"(归档|打标|建库|整理照片|加关键词|批量加星|加标题)",
            "auto_approve": False,
            "body": (
                "用户要批量打标/归档照片，建立可搜索的照片库。执行：\n"
                "1. 确保工作文件夹已设置。用 list_photos 了解照片清单，用 get_scores 看评分。\n"
                "2. 打标首选 photo_s 的 exif 工具（mcp__photo_s__exif）：paths 传工作文件夹内的照片列表，"
                "   tags 传 rating(0-5)/keywords(逗号分隔)/title，一次批量写入 EXIF（就地修改原文件）。\n"
                "3. 若在数字员工管线里做，可用 tag_keepers 步骤（keywords/rating/title 参数）在交付后统一打标。\n"
                "4. 可按评分/关键词筛选后移动到归档子文件夹（用现有 move/整理工具，破坏性操作会弹门）。\n"
                "5. 完成后汇报：打了多少张、写了什么标签、归档位置。"
            ),
        },
    }

    def seed_builtin_skills(self):
        """幂等写入内置技能（缺失才写，不覆盖用户同名自定义技能）。"""
        seeded = []
        for name, meta in self._BUILTIN_SKILLS.items():
            if name in self.skills:
                continue
            self.save(
                name=name,
                description=meta["description"],
                trigger=meta["trigger"],
                auto_approve=meta.get("auto_approve"),
                body=meta["body"],
            )
            seeded.append(name)
        if seeded:
            send_log(f"[skill_loader] 已写入内置技能: {', '.join(seeded)}", "info")
        return seeded


# ── Module-level singleton ──

_skill_loader = None


def get_skill_loader():
    global _skill_loader
    if _skill_loader is None:
        _skill_loader = SkillLoader()
    return _skill_loader
