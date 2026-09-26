#!/usr/bin/env python3
"""Build a privacy-clean macOS ZIP from the explicit distribution allowlist.

The archive uses the fixed ``codex-lead-deepseek/`` prefix, preserves file
modes, orders every entry and stamps every timestamp deterministically, so two
builds from the same tree are byte-identical.  No ``git`` is required.

``--with-runtimes`` packs the four official archives named by
``install/runtime-manifest.tsv`` (strict 6-column TSV:
``component,arch,version,basename,url,sha256``).  Every archive is checked
against the manifest SHA256 before it is added, so a wrong or corrupted
download is refused.  The running Mac's own environment is never copied.  The
adjacent ``.zip.sha256`` file is the hash of the ZIP itself.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import stat
import sys
import zipfile
from pathlib import Path, PurePosixPath

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
from leadseek import distribution  # noqa: E402

ZIP_TIME = (1980, 1, 1, 0, 0, 0)
ARCHIVE_SUFFIXES = (".tar.gz", ".tgz", ".tar.xz", ".zip")
MANIFEST_COLUMNS = ("component", "arch", "version", "basename", "url", "sha256")
REQUIRED_RUNTIMES = (
    ("node", "arm64"),
    ("node", "x86_64"),
    ("python", "arm64"),
    ("python", "x86_64"),
)


def read_version(root):
    text = (Path(root) / "src" / "leadseek" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'__version__\s*=\s*"([^"]+)"', text)
    if not match:
        raise SystemExit("无法从 src/leadseek/__init__.py 读取版本号")
    return match.group(1)


def decode_basename(basename):
    """Turn a manifest basename into the real on-disk file name.

    Python's build system URL-encodes the ``+`` in ``3.12.14+20260924`` as
    ``%2B``; the archive itself keeps the literal ``+``.
    """
    name = basename.replace("%2B", "+").replace("%2b", "+")
    if not name or name != PurePosixPath(name).name or name in (".", ".."):
        raise SystemExit("运行时归档名不合法: " + repr(basename))
    if "\\" in name or "/" in name:
        raise SystemExit("运行时归档名不能包含路径分隔符: " + repr(basename))
    if not name.endswith(ARCHIVE_SUFFIXES):
        raise SystemExit("运行时归档名后缀不受支持: " + repr(basename))
    return name


def parse_runtime_manifest(path):
    """Strictly parse the 6-column TSV and require the four official rows."""
    rows = []
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    for number, raw in enumerate(lines, 1):
        text = raw.strip()
        if not text or text.startswith("#"):
            continue
        fields = raw.split("\t")
        if len(fields) != len(MANIFEST_COLUMNS):
            raise SystemExit("运行时清单第 %d 行应为 %d 列（%s），实际 %d 列"
                             % (number, len(MANIFEST_COLUMNS),
                                ",".join(MANIFEST_COLUMNS), len(fields)))
        values = [field.strip() for field in fields]
        if not all(values):
            raise SystemExit("运行时清单第 %d 行存在空字段" % number)
        rows.append(dict(zip(MANIFEST_COLUMNS, values)))
    got = [(row["component"], row["arch"]) for row in rows]
    if len(rows) != len(REQUIRED_RUNTIMES) or set(got) != set(REQUIRED_RUNTIMES):
        raise SystemExit("运行时清单必须恰好包含 4 行：" + "、".join(
            "%s/%s" % pair for pair in REQUIRED_RUNTIMES))
    return rows


def runtime_archive_paths(root, manifest_path=None, overrides=None):
    """Resolve the four official archives and verify each manifest SHA256."""
    root = Path(root)
    manifest = Path(manifest_path) if manifest_path else root / distribution.RUNTIME_MANIFEST_REL
    if not manifest.is_file():
        raise SystemExit("找不到运行时清单: " + str(manifest))
    overrides = overrides or {}
    resolved = {}
    for row in parse_runtime_manifest(manifest):
        key = (row["component"], row["arch"])
        disk_name = decode_basename(row["basename"])
        override = overrides.get(row["basename"]) or overrides.get(disk_name)
        if override is not None:
            candidate = Path(override)
            if not candidate.is_absolute():
                candidate = root / candidate
            if not candidate.is_file():
                raise SystemExit("缺少运行时归档: " + str(candidate))
        else:
            # Accept the URL-encoded ``%2B`` form as well as the literal ``+``
            # form on disk; the archive entry always uses the literal ``+``.
            candidates = [root / "bundled" / disk_name]
            if row["basename"] != disk_name:
                candidates.append(root / "bundled" / row["basename"])
            candidate = next((item for item in candidates if item.is_file()), None)
            if candidate is None:
                raise SystemExit("缺少运行时归档: " + " 或 ".join(str(item) for item in candidates))
        actual = distribution.sha256_file(candidate)
        if actual.lower() != row["sha256"].lower():
            raise SystemExit("运行时归档 SHA256 与清单不符: " + disk_name)
        resolved[key] = (disk_name, candidate)
    return [resolved[key] for key in REQUIRED_RUNTIMES]


def build(root=REPO, output_dir=None, with_runtimes=False, version=None,
          runtime_manifest=None, runtime_overrides=None):
    root = Path(root).resolve()
    version = version or read_version(root)
    output_dir = Path(output_dir) if output_dir else root / "dist"
    output_dir.mkdir(parents=True, exist_ok=True)
    zip_path = output_dir / ("codex-lead-deepseek-%s-macos.zip" % version)

    payloads = []
    manifest_lines = []
    for rel in distribution.source_files(root):
        source = root / rel
        data = source.read_bytes()
        arcname = distribution.PACKAGE_DIR + "/" + rel
        payloads.append((arcname, data, stat.S_IMODE(source.stat().st_mode)))
        manifest_lines.append("%s  %s" % (hashlib.sha256(data).hexdigest(), arcname))

    if with_runtimes:
        for name, path in runtime_archive_paths(root, runtime_manifest, runtime_overrides):
            data = Path(path).read_bytes()
            arcname = distribution.PACKAGE_DIR + "/bundled/" + name
            payloads.append((arcname, data, stat.S_IMODE(Path(path).stat().st_mode)))
            manifest_lines.append("%s  %s" % (hashlib.sha256(data).hexdigest(), arcname))

    manifest_lines.sort()
    manifest_text = "\n".join(manifest_lines) + "\n"
    payloads.append((distribution.PACKAGE_DIR + "/" + distribution.MANIFEST_NAME,
                     manifest_text.encode("utf-8"), 0o644))

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for arcname, data, mode in sorted(payloads, key=lambda item: item[0]):
            info = zipfile.ZipInfo(arcname, date_time=ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (stat.S_IFREG | (mode & 0o777)) << 16
            archive.writestr(info, data)

    digest = distribution.sha256_file(zip_path)
    Path(str(zip_path) + ".sha256").write_text(
        "%s  %s\n" % (digest, zip_path.name), encoding="utf-8")
    return zip_path


def main(argv=None):
    parser = argparse.ArgumentParser(description="打包不含隐私的 macOS 安装 ZIP。")
    parser.add_argument("--root", default=str(REPO), help="项目根目录")
    parser.add_argument("--output-dir", help="输出目录，缺省 <root>/dist")
    parser.add_argument("--with-runtimes", action="store_true", help="附带四个官方运行时归档")
    parser.add_argument("--version", help="覆盖包名中的版本号")
    parser.add_argument("--runtime-manifest", help="install/runtime-manifest.tsv 路径")
    parser.add_argument("--runtime-archive", action="append", default=[], metavar="NAME=PATH",
                        help="显式指定某个运行时归档，可重复")
    args = parser.parse_args(argv)
    overrides = {}
    for item in args.runtime_archive:
        if "=" not in item:
            parser.error("--runtime-archive 需要 NAME=PATH 形式")
        name, path = item.split("=", 1)
        if (not name or "/" in name or "\\" in name
                or ".." in PurePosixPath(name).parts or name in (".", "..")):
            parser.error("--runtime-archive 的名称不能包含路径分隔符或 ..")
        overrides[name] = path
    path = build(args.root, args.output_dir, args.with_runtimes, args.version,
                 args.runtime_manifest, overrides)
    print("已生成 " + str(path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
