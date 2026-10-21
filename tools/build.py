#!/usr/bin/env python3
"""Build the host-platform standalone bundle and a distributable archive."""

from importlib import metadata
from pathlib import Path
import hashlib
import json
import platform
import shutil
import subprocess
import sys
import sysconfig
import tarfile

ROOT = Path(__file__).resolve().parent.parent


def main():
    subprocess.run(
        [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", str(ROOT / "yamd.spec")],
        cwd=ROOT, check=True,
    )
    bundle = ROOT / "dist" / "yamd"
    shutil.copy2(ROOT / "README.md", bundle / "README.md")
    versions = {}
    for distribution in metadata.distributions():
        name = distribution.metadata["Name"]
        versions[name] = distribution.version
        for file in distribution.files or []:
            if file.name.upper().startswith(("LICENSE", "COPYING", "NOTICE")):
                source = Path(distribution.locate_file(file))
                if source.is_file():
                    target = bundle / "licenses" / name / str(file).replace("/", "_")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source, target)
    python_license = Path(sysconfig.get_path("stdlib")) / "LICENSE.txt"
    if python_license.is_file():
        target = bundle / "licenses" / "Python-LICENSE.txt"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(python_license, target)
    (bundle / "BUILD-INFO.json").write_text(json.dumps({
        "application": "Yet Another Music Downloader",
        "python": platform.python_version(),
        "platform": platform.system(),
        "architecture": platform.machine(),
        "packages": dict(sorted(versions.items())),
    }, indent=2) + "\n", encoding="utf-8")
    system = {"Darwin": "macos", "Windows": "windows"}.get(platform.system(), platform.system().lower())
    archive = ROOT / "dist" / f"yamd-{system}-{platform.machine().lower()}.tar.gz"
    with tarfile.open(archive, "w:gz", format=tarfile.PAX_FORMAT) as output:
        output.add(bundle, arcname="yamd", filter=anonymous_owner)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_suffix(archive.suffix + ".sha256").write_text(f"{digest}  {archive.name}\n", encoding="utf-8")
    print(f"Built: {archive}")
    print(f"SHA256: {digest}")


def anonymous_owner(info):
    # Do not publish the local account name through archive ownership metadata.
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    return info


if __name__ == "__main__":
    main()
