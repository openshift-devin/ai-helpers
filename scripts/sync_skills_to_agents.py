#!/usr/bin/env python3
"""
Sync plugin skills to .agents/skills/ for Devin compatibility.

This script iterates over all plugins/*/skills/*/ directories and copies them
to .agents/skills/<skill-name>/, applying path transformations so that
references resolve correctly from the new location.

Transformations applied to .md files:
  - ${CLAUDE_PLUGIN_ROOT}/skills/<skill>/ -> ./
  - find ~/.claude/plugins fallback lines -> true  # comment
  - plugins/<plugin>/skills/<same-skill>/ -> ./ (self-references)
  - plugins/<plugin>/skills/<other-skill>/ -> ../<other-skill>/ (cross-skill refs)
  - /<plugin>:<command> -> /<command> (slash command prefix removal)
  - ../../reference/<file> -> ../../plugins/<plugin>/reference/<file>
    (non-skill plugin file references converted to repo-root-relative paths)
  - `<plugin>:<skill>` backtick references -> `<skill>` (remove plugin prefix)

Transformations applied to .py files:
  - Update parent.parent navigation to account for new depth
    (.agents/skills/<skill>/ instead of plugins/<plugin>/skills/<skill>/)
"""

import os
import re
import shutil
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGINS_DIR = REPO_ROOT / "plugins"
AGENTS_SKILLS_DIR = REPO_ROOT / ".agents" / "skills"

# Skill directory name overrides for generic names
SKILL_NAME_OVERRIDES = {
    # plugins/node-tuning/skills/scripts/ -> node-tuning-scripts
    ("node-tuning", "scripts"): "node-tuning-scripts",
}


def get_existing_agent_skills() -> set:
    """Return set of skill names already present in .agents/skills/."""
    if not AGENTS_SKILLS_DIR.exists():
        return set()
    return {d.name for d in AGENTS_SKILLS_DIR.iterdir() if d.is_dir()}


def discover_plugin_skills() -> list:
    """
    Discover all plugin skill directories.

    Returns list of tuples: (plugin_name, skill_name, source_path, target_skill_name)
    """
    skills = []
    for plugin_dir in sorted(PLUGINS_DIR.iterdir()):
        if not plugin_dir.is_dir():
            continue
        plugin_name = plugin_dir.name
        skills_dir = plugin_dir / "skills"
        if not skills_dir.exists():
            continue
        for skill_dir in sorted(skills_dir.iterdir()):
            if not skill_dir.is_dir():
                continue
            skill_name = skill_dir.name
            target_name = SKILL_NAME_OVERRIDES.get(
                (plugin_name, skill_name), skill_name
            )
            skills.append((plugin_name, skill_name, skill_dir, target_name))
    return skills


def transform_md_content(content: str, plugin_name: str, skill_name: str,
                         target_skill_name: str) -> str:
    """Apply all markdown transformations for Devin compatibility."""

    # 1. Replace ${CLAUDE_PLUGIN_ROOT}/skills/<skill-name>/ with ./
    content = content.replace(
        f"${{CLAUDE_PLUGIN_ROOT}}/skills/{skill_name}/", "./"
    )
    # Also handle without trailing slash
    content = re.sub(
        r'\$\{CLAUDE_PLUGIN_ROOT\}/skills/' + re.escape(skill_name) + r'(?=/|\s|"|\'|`|\))',
        ".",
        content,
    )

    # 2. Replace find ~/.claude/plugins fallback lines
    # Pattern: VAR=$(find ~/.claude/plugins -type f -path "*/<plugin>/skills/..." | sort | head -1)
    content = re.sub(
        r'^(\s*)\S+=\$\(find ~/\.claude/plugins .+\)$',
        r'\1true  # script is at ./<relative_path>',
        content,
        flags=re.MULTILINE,
    )
    # Also handle the if [ ! -f ] check that follows CLAUDE_PLUGIN_ROOT assignment
    # These are already handled by the find replacement above

    # 3. Self-references: plugins/<plugin>/skills/<same-skill>/ -> ./
    content = content.replace(
        f"plugins/{plugin_name}/skills/{skill_name}/", "./"
    )
    # Without trailing slash (for file references like plugins/teams/skills/list-teams/list_teams.py)
    content = re.sub(
        r'plugins/' + re.escape(plugin_name) + r'/skills/' + re.escape(skill_name) + r'(?=/)',
        ".",
        content,
    )

    # 4. Cross-skill references: plugins/<plugin>/skills/<other-skill>/ -> ../<other-skill>/
    # This handles references to skills from the same plugin
    content = re.sub(
        r'plugins/' + re.escape(plugin_name) + r'/skills/([a-zA-Z0-9_-]+)/',
        r'../\1/',
        content,
    )
    # Without trailing slash
    content = re.sub(
        r'plugins/' + re.escape(plugin_name) + r'/skills/([a-zA-Z0-9_-]+)(?=[/\s"`\')\]])',
        r'../\1',
        content,
    )

    # 5. Cross-plugin skill references: plugins/<other-plugin>/skills/<skill>/ -> ../<skill>/
    # All skills end up flat under .agents/skills/, so cross-plugin is also ../
    content = re.sub(
        r'plugins/[a-zA-Z0-9_-]+/skills/([a-zA-Z0-9_-]+)/',
        r'../\1/',
        content,
    )
    content = re.sub(
        r'plugins/[a-zA-Z0-9_-]+/skills/([a-zA-Z0-9_-]+)(?=[/\s"`\')\]])',
        r'../\1',
        content,
    )

    # 6. Remove plugin prefixes from slash commands: /plugin:command -> /command
    # Match /<plugin-name>:<command> at word boundary, but not inside URLs or file paths
    # Only match when preceded by whitespace, backtick, or start of line
    content = re.sub(
        r'(?<=[\s`])/([a-z][a-z0-9-]*):([a-z][a-z0-9-]*)',
        r'/\2',
        content,
    )
    # Also at start of line
    content = re.sub(
        r'^/([a-z][a-z0-9-]*):([a-z][a-z0-9-]*)',
        r'/\2',
        content,
        flags=re.MULTILINE,
    )

    # 7. Non-skill plugin file references (like ../../reference/wiki-markup.md)
    # These are relative to plugins/<plugin>/skills/<skill>/ and point to
    # plugins/<plugin>/reference/<file>
    # In .agents/skills/<skill>/, we need ../../plugins/<plugin>/reference/<file>
    content = re.sub(
        r'\.\./\.\./reference/',
        f'../../plugins/{plugin_name}/reference/',
        content,
    )

    # 8. References to other plugin-level files (like plugins/<plugin>/team_component_map.json)
    # These need to become ../../plugins/<plugin>/... from .agents/skills/<skill>/
    # Already handled by not matching skills pattern above

    # 9. Remove plugin prefix from backtick skill/command references
    # `plugin:skill-name` -> `skill-name`
    content = re.sub(
        r'`([a-z][a-z0-9-]*):((?:[a-z][a-z0-9-]*)(?:-[a-z][a-z0-9-]*)*)`',
        r'`\2`',
        content,
    )

    # 10. Handle repository-accessibility comment for scripts
    # "plugins/node-tuning/skills/scripts/" -> "./" when it's the target skill
    if skill_name == "scripts" and target_skill_name != skill_name:
        # Replace references to the old path with the new skill name context
        content = content.replace(
            f"plugins/{plugin_name}/skills/scripts/",
            "./"
        )

    return content


def transform_py_content(content: str, plugin_name: str, skill_name: str,
                         source_path: Path) -> str:
    """
    Apply Python file transformations for path resolution.

    The main pattern is scripts using script_dir.parent.parent to navigate
    from plugins/<plugin>/skills/<skill>/ up to plugins/<plugin>/.
    From .agents/skills/<skill>/, we need to go up 3 levels to repo root,
    then down into plugins/<plugin>/.
    """
    # Pattern: script_dir.parent.parent navigates from
    #   plugins/<plugin>/skills/<skill>/ -> plugins/<plugin>/
    # From .agents/skills/<skill>/, we need:
    #   script_dir.parent.parent.parent / "plugins" / "<plugin>"
    # to reach plugins/<plugin>/

    # Replace the comment that describes the old location
    content = re.sub(
        r"(#.*should be )plugins/" + re.escape(plugin_name) + r"/skills/" + re.escape(skill_name) + r"/",
        r"\1.agents/skills/" + skill_name + "/",
        content,
    )

    # Replace parent.parent navigation to plugin root
    # Old: plugin_dir = script_dir.parent.parent  # Go up two levels to plugins/<plugin>/
    # New: plugin_dir = script_dir.parent.parent.parent / "plugins" / "<plugin>"
    content = re.sub(
        r'(\w+)\s*=\s*script_dir\.parent\.parent\b(?!\.)',
        rf'\1 = script_dir.parent.parent.parent / "plugins" / "{plugin_name}"',
        content,
    )

    return content


def transform_py_in_scripts_subdir(content: str, plugin_name: str,
                                    skill_name: str) -> str:
    """
    Apply Python transformations for files in a scripts/ subdirectory.

    These files are at plugins/<plugin>/skills/<skill>/scripts/<file>.py
    and will be at .agents/skills/<skill>/scripts/<file>.py

    The parent navigation depth might differ. For scripts that use
    script_dir.parent.parent to reach plugin root from scripts/ subdir:
    Old: scripts/ -> skill/ -> skills/ -> plugin/  (3 parents up from scripts/)
    But they usually do script_dir.parent which is the skill dir, then .parent.parent
    to get plugin root.

    Actually, scripts in subdirs typically don't do path navigation themselves -
    they're invoked by SKILL.md. But if they do, handle it.
    """
    # Same pattern as above but accounting for the scripts/ subdirectory
    # script_dir = Path(__file__).parent  -> points to scripts/ subdir
    # If they do script_dir.parent.parent.parent to get to plugin root,
    # that would be: .agents/skills/<skill>/scripts/ -> .agents/skills/<skill>/ -> .agents/skills/ -> .agents/ -> repo root
    # We need: repo_root / "plugins" / "<plugin>"

    # This is rare - most scripts in subdirs don't do path nav. Handle if found.
    return content


def copy_and_transform_skill(plugin_name: str, skill_name: str,
                              source_path: Path, target_skill_name: str) -> None:
    """Copy a skill directory and apply all transformations."""
    target_path = AGENTS_SKILLS_DIR / target_skill_name

    # Copy entire directory
    if target_path.exists():
        shutil.rmtree(target_path)
    shutil.copytree(source_path, target_path)

    # Transform all files
    for root, dirs, files in os.walk(target_path):
        for filename in files:
            filepath = Path(root) / filename

            if filename.endswith(".md"):
                content = filepath.read_text(encoding="utf-8")
                new_content = transform_md_content(
                    content, plugin_name, skill_name, target_skill_name
                )
                if new_content != content:
                    filepath.write_text(new_content, encoding="utf-8")

            elif filename.endswith(".py"):
                content = filepath.read_text(encoding="utf-8")
                new_content = transform_py_content(
                    content, plugin_name, skill_name, source_path
                )
                if new_content != content:
                    filepath.write_text(new_content, encoding="utf-8")


def main():
    existing = get_existing_agent_skills()
    all_skills = discover_plugin_skills()

    # Filter to CI plugin skills (already mapped)
    ci_skill_names = {
        s[3] for s in all_skills if s[0] == "ci"
    }

    print(f"Found {len(all_skills)} total plugin skills")
    print(f"Found {len(existing)} existing .agents/skills/ entries")
    print(f"CI skills (already mapped): {len(ci_skill_names)}")
    print()

    copied = 0
    skipped = 0

    for plugin_name, skill_name, source_path, target_skill_name in all_skills:
        # Skip CI skills - they're already mapped
        if plugin_name == "ci":
            skipped += 1
            continue

        # Skip if already exists (shouldn't happen for non-CI, but safety check)
        if target_skill_name in existing:
            print(f"  SKIP (exists): {plugin_name}/{skill_name} -> {target_skill_name}")
            skipped += 1
            continue

        print(f"  COPY: plugins/{plugin_name}/skills/{skill_name}/ -> .agents/skills/{target_skill_name}/")
        copy_and_transform_skill(plugin_name, skill_name, source_path, target_skill_name)
        copied += 1

    print()
    print(f"Copied: {copied}")
    print(f"Skipped: {skipped} (CI skills already mapped)")
    print(f"Total .agents/skills/ entries: {len(existing) + copied}")


if __name__ == "__main__":
    main()
