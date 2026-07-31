"""`git` command group: git helpers spanning the manager + executor submodules."""

from . import (
	check_for_push,
	create_branches,
	list_repo,
	ls,
	verify_pushed_executors,
)

NAME = 'git'
HELP = 'git helpers across the manager and executor submodules'
COMMANDS = [
	ls,
	list_repo,
	create_branches,
	check_for_push,
	verify_pushed_executors,
]
