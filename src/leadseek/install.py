"""Install small discovery links; keep code, backups and receipts in this project."""

import argparse
from datetime import datetime
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
BEGIN = "<!-- codex-lead-deepseek:begin -->"
END = "<!-- codex-lead-deepseek:end -->"


def rule_block():
    return f"""{BEGIN}
## Codex 统筹与 DeepSeek 默认执行

- 默认先使用 `deepseek-delegate` Skill 做简短任务路由：Codex 负责理解需求、关键判断、方案、拆解、验收和下一步指令；凡标准明确、结果可核验且现有工具能完成的执行工作，默认交给 DeepSeek，不限于代码和检索，也包括常规内容加工、提取清洗、分类去重、摘要翻译、格式转换、测试、日志分析和批量处理。禁止因为方便而由 GPT-6 Astra 包办全部工作。
- Skill：`{ROOT / 'skill/deepseek-delegate/SKILL.md'}`。CLI：`{ROOT / 'bin/leadseek'}`（全局入口 `~/.local/bin/leadseek`，不硬编码本项目所在路径）。允许自动调用 Harness 及其受限 subagent。
- 每个 Codex 任务首次启用本 Skill、开始任何执行前，先运行 `~/.local/bin/leadseek activate`（PATH 中有 `leadseek` 时也可直接调用）：首次会打开带认证的本机监控并自动关联当前 `CODEX_THREAD_ID`；同一任务后续 run/revise 不重复弹页，服务中断后可重试。失败时命令返回非零并只给不含 token 的简短提示，按提示处理，不要读取或转述原始捕获输出。Codex 自主审核、发返工指令、验证、应用、继续下一项，直到原目标完成；普通可逆选择自行判断，不逐轮向用户确认。
- 首次实质执行前用 `leadseek route` 记录分工；委派和返工自动留痕。Codex 直接实现仅限关键复杂部分、很少量集成胶水或委派开销明显大于工作量的微小改动，并记录具体原因。DeepSeek 失败先诊断和有限返工，不得静默转为 GPT 全包。最终简述实际分工，不把日志压缩率当账单节省率。
- 完整日志只留本地，默认只读精简结果，验收时按需读差异和必要证据。涉密、高度隐私、用户核心安全利益相关的重要或不可逆操作，以及更高优先级权限要求，才向用户确认；不擅自扩大原任务授权。公文仍遵守原有不生成交付文件的要求，敏感材料不外发。用户明确不用 DeepSeek 时遵从。
{END}"""


def remove_block(text):
    if text.count(BEGIN) != text.count(END) or text.count(BEGIN) > 1:
        raise ValueError("全局规则标记异常，未修改文件")
    if BEGIN not in text:
        return text
    start = text.index(BEGIN)
    finish = text.index(END, start) + len(END)
    return text[:start].rstrip() + text[finish:]


def integrate(home, uninstall=False):
    home = Path(home)
    links = {home / ".codex/skills/deepseek-delegate": ROOT / "skill/deepseek-delegate",
             home / ".local/bin/leadseek": ROOT / "bin/leadseek"}
    agents = home / ".codex/AGENTS.md"
    if agents.is_symlink():
        raise ValueError("全局 AGENTS.md 是符号链接，请先确认其实际位置")
    old = agents.read_text(encoding="utf-8") if agents.exists() else ""
    cleaned = remove_block(old)
    for link, target in links.items():
        if link.exists() or link.is_symlink():
            if not link.is_symlink() or link.resolve() != target.resolve():
                raise ValueError("入口已被其他工具占用，未覆盖: " + str(link))
    if uninstall:
        for link in links:
            if link.is_symlink():
                link.unlink()
        if cleaned != old:
            agents.write_text(cleaned.rstrip() + "\n", encoding="utf-8")
        return {"installed": False, "files_preserved": str(ROOT)}
    updated = cleaned.rstrip() + "\n\n" + rule_block() + "\n"
    backups = ROOT / ".state/install"
    backups.mkdir(parents=True, exist_ok=True)
    backup = None
    if updated != old:
        backup = backups / ("AGENTS-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f") + ".md")
        backup.write_text(old, encoding="utf-8")
        agents.parent.mkdir(parents=True, exist_ok=True)
        agents.write_text(updated, encoding="utf-8")
    for link, target in links.items():
        link.parent.mkdir(parents=True, exist_ok=True)
        if not link.is_symlink():
            link.symlink_to(target, target_is_directory=target.is_dir())
    result = {"installed": True, "global_rules": str(agents), "links": {str(k): str(v) for k, v in links.items()},
              "backup": str(backup) if backup else None}
    (backups / "receipt.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def main():
    parser = argparse.ArgumentParser(description="接入或解除 Codex 全局分工；主体文件保留在交付目录")
    parser.add_argument("--uninstall", action="store_true")
    args = parser.parse_args()
    try:
        print(json.dumps(integrate(Path.home(), args.uninstall), ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1
