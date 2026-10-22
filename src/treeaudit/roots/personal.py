"""The user's personal folder as an audit root (docs/DECISIONS.md D-037).

It is offered whole, read-only, so that hidden caches (often the biggest things
to clean) are visible too. Folders and files holding secrets are never even
examined: not listed, not read, not archived. They are the usual places of
keys, password stores, browser profiles (cookies, saved passwords) and cloud
credentials. This list only reduces what is exposed: the folder is still
mounted read-only, in a container without network (rule 11 of CLAUDE.md).
"""

from __future__ import annotations

from treeaudit.core.config import RootConfig

HOME_ROOT_ID = "home"
HOME_LABEL = "Mon dossier personnel"
CONTAINER_HOME_DIR = "/audit/home"
HOME_PATH_ENV_VAR = "TREEAUDIT_HOME_PATH"
"""Host path of the personal folder, set by ./treeaudit when it mounts it."""

PRIVATE_PATHS: frozenset[bytes] = frozenset(
    {
        # keys and password stores
        b".ssh",
        b".gnupg",
        b".password-store",
        b".pki",
        b".local/share/keyrings",
        b".local/share/kwalletd",
        b".config/Bitwarden",
        b".config/keepassxc",
        # browser and mail profiles: cookies, saved passwords, sessions
        b".mozilla",
        b".thunderbird",
        b".librewolf",
        b".config/google-chrome",
        b".config/chromium",
        b".config/BraveSoftware",
        b".config/vivaldi",
        b".config/opera",
        b".config/microsoft-edge",
        b".var/app/org.mozilla.firefox",
        b".var/app/org.mozilla.Thunderbird",
        b".var/app/com.google.Chrome",
        b".var/app/org.chromium.Chromium",
        b".var/app/com.brave.Browser",
        b".var/app/com.github.Eloston.UngoogledChromium",
        # cloud, container and forge credentials
        b".aws",
        b".azure",
        b".kube",
        b".docker",
        b".config/gcloud",
        b".config/gh",
        b".netrc",
        b".git-credentials",
        b".pgpass",
    }
)


def personal_root(path: str, display_path: str | None = None) -> RootConfig:
    return RootConfig(
        id=HOME_ROOT_ID,
        path=path,
        label=HOME_LABEL,
        # The personal folder is on the system volume by nature: offering it is
        # the explicit choice D-009 asks for, made once in D-037.
        allow_system_volume=True,
        display_path=display_path,
        personal=True,
        private_paths=PRIVATE_PATHS,
    )
