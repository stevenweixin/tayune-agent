"""Build and package only the public WordPress entry and production assets."""
import argparse
import hashlib
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "deploy/wordpress"
FILES = ("tayune_assistant.php", "assets/tea_companion.js", "assets/tea_companion.js.LEGAL.txt")


def create_package(plugin: Path, output: Path) -> dict:
    version = json.loads((plugin / "package.json").read_text(encoding="utf-8"))["version"]
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError("Invalid plugin version")
    php = (plugin / "tayune_assistant.php").read_text(encoding="utf-8")
    # PHP头部是WordPress识别插件版本的入口，必须与构建清单一致。
    match = re.search(r"^\s*\* Version:\s*(\S+)\s*$", php, re.MULTILINE)
    if match is None or match.group(1) != version:
        raise ValueError("PHP and package.json versions differ")
    if output.exists():
        raise FileExistsError("Preserve the existing archive; choose a new output filename")
    for name in FILES:
        if not (plugin / name).is_file() or (plugin / name).stat().st_size == 0:
            raise ValueError("Missing or empty release file: " + name)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output.parent) as directory:
        staged = Path(directory) / "plugin.zip"
        with ZipFile(staged, "w", compression=ZIP_DEFLATED) as archive:
            # 显式白名单；不递归打包，避免带入.env、node_modules或本机资料。
            for name in FILES:
                archive.write(plugin / name, "tayune-tea-companion/" + name)
        with ZipFile(staged) as archive:
            if archive.testzip() is not None:
                raise ValueError("Archive integrity check failed")
        staged.replace(output)
    return {"version": version, "archive": str(output), "files": list(FILES),
            "sha256": hashlib.sha256(output.read_bytes()).hexdigest(), "bytes": output.stat().st_size}


def main():
    parser = argparse.ArgumentParser(description="Build, lint and package the WordPress widget; never upload")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    version = json.loads((PLUGIN / "package.json").read_text(encoding="utf-8"))["version"]
    output = args.output or ROOT / "reports/releases" / ("tayune-tea-companion-" + version + ".zip")
    if output.exists():
        raise FileExistsError("Archive already exists; use --output with a new filename")
    environment = dict(os.environ)
    environment["npm_config_cache"] = str(ROOT / "reports/npm-cache")
    subprocess.run(["npm", "run", "build"], cwd=PLUGIN, env=environment, check=True)
    subprocess.run(["php", "-l", "tayune_assistant.php"], cwd=PLUGIN, check=True)
    print(json.dumps(create_package(PLUGIN, output), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
