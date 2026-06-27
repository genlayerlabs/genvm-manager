"""`hook` command group: run / install the per-repo lint hooks."""

from . import install, run

NAME = 'hook'
HELP = 'run and install the per-repo lint hooks'
COMMANDS = [run, install]
