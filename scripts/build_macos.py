"""Build a self-contained arm64 macOS App using an explicit source allowlist."""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata as metadata
import json
import os
import platform
import plistlib
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION = "0.4.1"
RELEASE = ROOT / "release"
BUILD = ROOT / "build"


def run(*args, **kwargs):
    print(" ".join(map(str, args)), flush=True)
    subprocess.run(list(map(str, args)), cwd=ROOT, check=True, **kwargs)


def source_files():
    names = ["README.md", "OPTIMIZATION_PLAN.md", "启动译制台.command", "组内简明使用说明.txt", "drizzle.config.ts", "package.json", "package-lock.json", "tsconfig.json", "postcss.config.mjs",
             "eslint.config.mjs", "vite.config.ts", "next.config.ts", "next-env.d.ts", ".gitignore"]
    for folder in ("app", "desktop", "scripts", "tests", "public", "worker", "db", "drizzle"):
        for path in (ROOT / folder).rglob("*"):
            if path.is_file() and not any(part.startswith(".") or part == "__pycache__" for part in path.relative_to(ROOT).parts):
                names.append(str(path.relative_to(ROOT)))
    names += ["backend/app.py", "backend/desktop_support.py", "backend/library.py", "backend/engine_bootstrap.py", "backend/request_runtime.py", "backend/translation_runtime.py", "backend/requirements.txt"]
    return sorted(set(names))


def licenses(destination):
    destination.mkdir(parents=True, exist_ok=True)
    notice = ["纸间第三方组件授权与源码", "", "主要 PDF 引擎：",
        "PDFMathTranslate-next 2.9.0 (AGPL-3.0): https://github.com/PDFMathTranslate-next/PDFMathTranslate-next/tree/v2.9.0",
        "BabelDOC 0.6.2 (AGPL-3.0): https://github.com/funstory-ai/BabelDOC/tree/v0.6.2",
        "PyMuPDF 1.25.2 (AGPL-3.0/commercial): https://github.com/pymupdf/PyMuPDF/tree/1.25.2",
        "", "以下为构建环境中分发的上游许可证副本（部分组件仅用于构建）：", ""]
    for dist in sorted(metadata.distributions(), key=lambda d: d.metadata.get("Name", "")):
        name = dist.metadata.get("Name", "unknown")
        notice.append(f"{name} {dist.version}")
        for file in dist.files or []:
            if not any(part.lower().startswith(("license", "copying", "notice", "authors")) for part in file.parts):
                continue
            path = Path(dist.locate_file(file))
            if path.is_file() and path.stat().st_size < 512 * 1024:
                target = destination / name / Path(*file.parts[1:])
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)
    react_license = ROOT / "node_modules/react/LICENSE"
    if react_license.exists():
        shutil.copy2(react_license, destination / "React-LICENSE.txt")
    (destination / "NOTICES.txt").write_text("\n".join(notice), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-engine", action="store_true")
    parser.add_argument("--skip-checks", action="store_true")
    args = parser.parse_args()
    if sys.platform != "darwin" or platform.machine() != "arm64":
        raise SystemExit("Build this edition on an Apple Silicon Mac.")
    RELEASE.mkdir(exist_ok=True)
    BUILD.mkdir(exist_ok=True)
    if not args.skip_checks:
        run(sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py")
        run("npx", "tsc", "--noEmit", "-p", "desktop/tsconfig.json")
        run("npm", "run", "lint")
    run("npx", "vite", "build", "--config", "desktop/vite.config.ts")
    env = dict(os.environ, PYINSTALLER_CONFIG_DIR=str(ROOT / ".runtime/pyinstaller"))
    if not args.skip_engine:
        import cv2
        if re.search(r"FFMPEG:\s+YES", cv2.getBuildInformation()):
            raise SystemExit("Slim 发布需先构建并安装精简 OpenCV：参见 README 中的 build_slim_opencv.py 步骤。")
        run(sys.executable, "-m", "PyInstaller", "--noconfirm", "--distpath", "build/engine-dist",
            "--workpath", "build/engine-work", "desktop/engine.spec", env=env)
    native = BUILD / "PaperStudio"
    run("xcrun", "swiftc", "-O", "-target", "arm64-apple-macosx14.0", "-module-cache-path", 
        ROOT / ".runtime/swift-cache", "-framework", "AppKit", "-framework", "WebKit", "-framework", "Security",
        "-framework", "UniformTypeIdentifiers", "desktop/Studio.swift", "desktop/ProviderStore.swift", "desktop/ValidationDriver.swift", "-o", native)
    app = BUILD / f"package-{VERSION}-macos-arm64/纸间.app"
    if app.exists():
        shutil.rmtree(app)
    contents = app / "Contents"
    resources = contents / "Resources"
    (contents / "MacOS").mkdir(parents=True)
    (contents / "Helpers").mkdir()
    resources.mkdir()
    shutil.copy2(native, contents / "MacOS/PaperStudio")
    shutil.copytree(BUILD / "engine-dist/StudioEngine.app", contents / "Helpers/StudioEngine.app", symlinks=True)
    shutil.copytree(BUILD / "desktop-web", resources / "web")
    shutil.copy2(ROOT / "desktop/使用说明.txt", resources / "使用说明.txt")
    info = {"CFBundleExecutable": "PaperStudio", "CFBundleIdentifier": "org.paperstudio.app",
            "CFBundleName": "纸间", "CFBundleDisplayName": "纸间 · PDF 译制台", "CFBundlePackageType": "APPL",
            "CFBundleShortVersionString": VERSION, "CFBundleVersion": "5", "LSMinimumSystemVersion": "14.0",
            "NSHighResolutionCapable": True, "NSPrincipalClass": "NSApplication", "CFBundleIconFile": "Studio.icns",
            "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True},
            "NSHumanReadableCopyright": "纸间 · PDF 译制台；第三方授权见随附说明。"}
    (contents / "Info.plist").write_bytes(plistlib.dumps(info))
    run("xcrun", "swiftc", "-O", "-module-cache-path", ROOT / ".runtime/swift-cache", "desktop/icon.swift", "-o", BUILD / "make-icon")
    run(BUILD / "make-icon", BUILD / "icon.png")
    iconset = BUILD / "Studio.iconset"
    iconset.mkdir(exist_ok=True)
    for size in (16, 32, 128, 256, 512):
        for scale in (1, 2):
            suffix = "@2x" if scale == 2 else ""
            run("sips", "-z", size * scale, size * scale, BUILD / "icon.png", "--out", iconset / f"icon_{size}x{size}{suffix}.png", stdout=subprocess.DEVNULL)
    run("iconutil", "-c", "icns", iconset, "-o", resources / "Studio.icns")
    source = RELEASE / f"PaperStudio-{VERSION}-source.zip"
    with zipfile.ZipFile(source, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in source_files():
            archive.write(ROOT / name, f"paperstudio/{name}")
    shutil.copy2(source, resources / "Source.zip")
    licenses(resources / "ThirdPartyLicenses")
    run("xattr", "-cr", app)
    run("codesign", "--force", "--deep", "--sign", "-", "--timestamp=none", app)
    run("codesign", "--verify", "--deep", "--strict", "--verbose=2", app)
    distribution = RELEASE / f"PaperStudio-{VERSION}-macOS-arm64.zip"
    run("ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", app, distribution)
    shutil.copy2(ROOT / "desktop/使用说明.txt", RELEASE / "使用说明.txt")
    hashes = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in (distribution, source)}
    (RELEASE / "SHA256SUMS.txt").write_text("".join(f"{value}  {name}\n" for name, value in hashes.items()), encoding="utf-8")
    report = {"version": VERSION, "architecture": "arm64", "minimum_macos": "14.0", "python": platform.python_version(),
              "app_bytes": sum(path.stat().st_size for path in app.rglob("*") if path.is_file() and not path.is_symlink()),
              "zip_bytes": distribution.stat().st_size, "sha256": hashes, "signature": "ad-hoc", "notarized": False}
    (RELEASE / "build-info.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
