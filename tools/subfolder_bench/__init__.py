"""Development bench for the sub-folders proposed by the rules (D-050).

Not shipped: the application never imports it (tests/unit/test_architecture.py).
It scores ``treeaudit.organize.subfolders.propose`` on two sets of made-up
cases. From the repository, in the development container:

    TREEAUDIT_UID=$(id -u) TREEAUDIT_GID=$(id -g) \
        docker compose -f compose.yaml run --rm dev python -m tools.subfolder_bench

``cases.py`` holds the held-out cases, committed before the algorithm and never
changed since; ``cases_earlier.py`` the cases first written for the local AI;
``scoring.py`` the score; ``__main__.py`` the report (made-up names only).
"""
