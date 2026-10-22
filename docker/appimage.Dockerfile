# syntax=docker/dockerfile:1
#
# TreeAudit AppImage (docs/DECISIONS.md D-038), built entirely in Docker:
#   ./treeaudit appimage   ->   dist/TreeAudit-x86_64.AppImage
#
# Inside: a standalone Python (python-build-standalone, fetched and checked by
# uv), the locked runtime dependencies (uv.lock, with hashes) and TreeAudit.
# The file is the AppImage type 2 runtime followed by a squashfs image.
#
# Everything is pinned: base images by digest, the runtime by checksum. The
# runtime comes from the "continuous" release: when it changes, the checksum
# no longer matches and the build stops, until the new one is reviewed.
ARG PYTHON_IMAGE=python:3.14-slim@sha256:fbecd461fc72d21b2143c6eaba25143738079154a6d0ea3da3aec724f50eed4a
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.13.1@sha256:4a1f3666816dafadd20ceb71eac9a7d56d7ef30279f7d64048519ab901ada336

FROM ${UV_IMAGE} AS uv

# ---- the AppDir: Python, dependencies, TreeAudit
FROM ${PYTHON_IMAGE} AS appdir
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_NO_CACHE=1 \
    UV_PYTHON_INSTALL_DIR=/opt/python \
    UV_PYTHON_PREFERENCE=only-managed \
    UV_LINK_MODE=copy
# uv names the interpreter through a symbolic link (cpython-3.14-...): copy
# the real folder, never the link.
RUN uv python install 3.14 \
 && mkdir -p /AppDir/usr \
 && cp -a "$(realpath "$(dirname "$(dirname "$(uv python find 3.14)")")")" /AppDir/usr/python
WORKDIR /src
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN uv export --frozen --no-dev --no-emit-project --format requirements-txt -o /tmp/requirements.txt \
 && uv pip install --python /AppDir/usr/python/bin/python3 --break-system-packages \
        --require-hashes --compile-bytecode -r /tmp/requirements.txt \
 && uv pip install --python /AppDir/usr/python/bin/python3 --break-system-packages \
        --no-deps --compile-bytecode .
# Not needed at run time: tests, IDLE, Tk, pip bootstrap, headers.
RUN cd /AppDir/usr/python \
 && rm -rf include share lib/tcl* lib/tk* lib/itcl* lib/thread* lib/libtcl* lib/libtk* \
 && cd lib/python3.14 \
 && rm -rf test idlelib tkinter turtledemo turtle.py ensurepip lib-dynload/_tkinter* \
 && /AppDir/usr/python/bin/python3 -I -c "import sys, sqlite3, lzma, ctypes, treeaudit.entry; \
assert sys.prefix == '/AppDir/usr/python', sys.prefix; \
assert treeaudit.entry.__file__.startswith('/AppDir/'), treeaudit.entry.__file__" \
 && test ! -L /AppDir/usr/python

# ---- the AppImage file
FROM ${PYTHON_IMAGE} AS pack
RUN apt-get update \
 && apt-get install -y --no-install-recommends squashfs-tools \
 && rm -rf /var/lib/apt/lists/*
ADD --checksum=sha256:617aa77314d57c8b8e4fae9c3f0b35af7b30d5529cb69e98f64f92510ed98a46 \
    https://github.com/AppImage/type2-runtime/releases/download/continuous/runtime-x86_64 /runtime
COPY --from=appdir /AppDir /AppDir
COPY packaging/appimage/AppRun packaging/appimage/treeaudit.desktop packaging/appimage/treeaudit.svg /AppDir/
RUN chmod 755 /AppDir/AppRun \
 && ln -s treeaudit.svg /AppDir/.DirIcon \
 && mksquashfs /AppDir /app.squashfs -root-owned -noappend -comp zstd -quiet \
 && mkdir /out \
 && cat /runtime /app.squashfs > /out/TreeAudit-x86_64.AppImage \
 && chmod 755 /out/TreeAudit-x86_64.AppImage

FROM scratch AS out
COPY --from=pack /out/ /
