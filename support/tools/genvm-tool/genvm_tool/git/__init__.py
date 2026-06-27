"""`git` command group: git helpers spanning the manager + executor submodules."""

from . import list_repo, ls

NAME = 'git'
HELP = 'git helpers across the manager and executor submodules'
COMMANDS = [ls, list_repo]
