# Git third party

Tool for patching third-party libraries without need to fork them

This is a small zero-dependency utility that is an alternative to [git-submodules](https://git-scm.com/book/en/v2/Git-Tools-Submodules) and [git-subtree](https://manpages.debian.org/testing/git-man/git-subtree.1.en.html). It stores only delta (changes) inside your tree

This tool is not oriented for highly concurrent modification of third-party tools by multiple users

## Usage
```bash
# adding third-party repository
git-third-party add third-party/RustPython https://github.com/RustPython/RustPython.git a13b99642b0bc13ca89d01768a7ddbec18fe8219
# ^ git-third-party add <relative path> <repository url> <commit hash>

# now you can modify it as a regular repository, commit your changes

# save patches to push to your origin
git-third-party save third-party/RustPython
# ^ git-third-party save <relative path>

# update
git-third-party update third-party/RustPython
# ^ git-third-party update <relative path>
```

## How it works?

It stores (at `/.git-third-party/`) a configuration that describes all third party repositories and patches. `save` command updates the patches, `update` command reapplies them

Directory `.git-third-party` must be controlled by git, while third-party repos should not

Best effort is done to keep patches deterministic and strip all metadata from them, which includes:
- git version
- commit hash (which depends on committer)
- ...
