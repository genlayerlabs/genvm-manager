# Adding an Agent Skill

Skills are procedures loaded on demand. `.agents/` is canonical; Claude Code
sees each through a symlink

1. Write `.agents/skills/<name>/SKILL.md`, starting with the frontmatter:

   ```markdown
   ---
   name: <name>
   description: What it does. Use when <the moment it applies>.
   ---
   ```

   `name` matches the directory. `description` drives triggering, so name when
   the skill applies and the words someone would use then

2. Symlink it where Claude Code discovers skills, relative so the link survives
   a clone or a worktree:

   ```bash
   ln -s ../../.agents/skills/<name> .claude/skills/<name>
   ```

   Link each skill, never the whole directory, which would expose future skills
   without review. Link subagents individually too:
   `ln -s ../../.agents/agents/<name>.md .claude/agents/<name>.md`

3. Keep the body short and imperative. Supporting files sit beside `SKILL.md` in
   the same directory and are referenced by relative path

## Skill or How-To

They serve different roles:

| It is | Where it goes |
|---|---|
| A rule about the repository, true whoever reads it | a how-to in `docs/contributing/`, indexed and reaching humans too |
| When to act, what to report, what an agent may decide alone | the skill |

Restating a how-to in a skill creates a copy that will drift. Link to the page;
keep only AI-specific instructions in the skill. [review-ready](../review-ready.md)
and its skill are the worked example
