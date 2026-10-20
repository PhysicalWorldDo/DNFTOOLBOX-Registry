"""Adapt published binaries to toolbox packages; register verified attachments."""
import argparse
import copy
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tempfile
import zipfile

TOOLS = {
    "dnf_skill_prism": {"repo": "PhysicalWorldDo/DNF-Skill-Prism", "name": "技能染色工坊",
        "exe": "dnf_skill_prism.exe", "asset": "DNFSkillPrism-v{version}-win-x64.zip",
        "description": "REP 技能同步预览、配色预设、批量染色和补丁导出。"},
    "rep_player": {"repo": "PhysicalWorldDo/rep-player", "name": "REP 播放器",
        "exe": "rep_player.exe", "asset": "rep-player-v{version}-windows-x64.zip",
        "description": "离线播放 REP 录像，支持逐帧、倍速和素材导出。"},
}


def stable_version(tag):
    if not re.fullmatch(r"v?\d+\.\d+\.\d+", tag):
        raise ValueError(f"Not a stable version: {tag}")
    return tag.removeprefix("v")


def version_key(version):
    return tuple(map(int, stable_version(version).split(".")))


def package_name(tool_id, version):
    return f"{tool_id}-{version}-win-x64-toolbox.zip"


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def tool_manifest(tool_id, version, release_date, changelog):
    tool = TOOLS[tool_id]
    version = stable_version(version)
    return {"schemaVersion": 1, "id": tool_id, "name": tool["name"], "category": "资源工具",
        "description": tool["description"], "icon": "", "entry": "bin/run.cmd", "needAdmin": False,
        "projectUrl": "https://github.com/" + tool["repo"], "latest": {"stable": version},
        "versions": [{"version": version, "channel": "stable", "releaseDate": release_date,
            "packageUrl": f'https://github.com/{tool["repo"]}/releases/download/v{version}/{package_name(tool_id, version)}',
            "sha256": "", "size": 0, "changelog": changelog, "minToolboxVersion": "0.2.0"}],
        "permissions": [], "tags": ["DNF", "REP"], "status": "active", "blockedVersions": []}


def launcher_script(executable):
    return r'''param([switch]$Hidden)
$ErrorActionPreference = 'Stop'
$toolRoot = Split-Path $PSScriptRoot -Parent
$appRoot = Join-Path $PSScriptRoot 'app'
$savedRuntime = Join-Path $toolRoot 'config\runtime'
$savedExports = Join-Path $toolRoot 'data\exports'
$runtime = Join-Path $appRoot 'runtime'
$exports = Join-Path $appRoot 'exports'
$rootBytes = [Text.Encoding]::UTF8.GetBytes($toolRoot.ToUpperInvariant())
$rootHash = [BitConverter]::ToString([Security.Cryptography.SHA256]::Create().ComputeHash($rootBytes)).Replace('-', '')
$lockCreated = $false
$sessionLock = [Threading.Mutex]::new($true, ('Local\DNFToolbox-' + $rootHash), [ref]$lockCreated)
if (!$lockCreated) { $sessionLock.Dispose(); exit 0 }
function Mirror-Directory([string]$Source, [string]$Destination) {
    if (!(Test-Path -LiteralPath $Source)) { return }
    $resolvedDestination = [IO.Path]::GetFullPath($Destination)
    if (!$resolvedDestination.StartsWith([IO.Path]::GetFullPath($toolRoot) + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Data destination must stay inside this tool directory'
    }
    New-Item -ItemType Directory -Path $Destination -Force | Out-Null
    & robocopy.exe $Source $Destination /MIR /R:1 /W:1 /XJ /XD webview /NFL /NDL /NJH /NJS /NP | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "Data sync failed: $Source -> $Destination ($LASTEXITCODE)" }
}
$exitCode = 0
try {
    Mirror-Directory $savedRuntime $runtime
    Mirror-Directory $savedExports $exports
    New-Item -ItemType Directory -Path $runtime,$exports -Force | Out-Null
    try {
        $style = if ($Hidden) { 'Hidden' } else { 'Normal' }
        $application = Start-Process -FilePath (Join-Path $appRoot '__EXECUTABLE__') -WorkingDirectory $appRoot -WindowStyle $style -Wait -PassThru
        $exitCode = $application.ExitCode
    } finally {
        Mirror-Directory $runtime $savedRuntime
        Mirror-Directory $exports $savedExports
    }
} finally {
    $sessionLock.ReleaseMutex()
    $sessionLock.Dispose()
}
exit $exitCode
'''.replace("__EXECUTABLE__", executable)


def build_package(source, tool_id, version, output, release_date, changelog):
    version = stable_version(version)
    tool = TOOLS[tool_id]
    manifest = tool_manifest(tool_id, version, release_date, changelog)
    files = {}
    with zipfile.ZipFile(source) as archive:
        members = [item for item in archive.infolist() if not item.is_dir()]
        for item in members:
            parts = PurePosixPath(item.filename).parts
            if "\\" in item.filename or item.filename.startswith("/") or any(p in {"..", "."} or ":" in p for p in parts):
                raise ValueError(f"Unsafe ZIP path: {item.filename}")
            if (item.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError("Package must not contain symlinks")
        executables = [item for item in members if PurePosixPath(item.filename).name == tool["exe"]]
        if len(executables) != 1:
            raise ValueError("Official package must contain exactly one application")
        parent = PurePosixPath(executables[0].filename).parent
        for item in members:
            try:
                relative = PurePosixPath(item.filename).relative_to(parent)
            except ValueError as error:
                raise ValueError("Unexpected files outside application root") from error
            if relative.parts[0].casefold() in {"runtime", "exports", "config", "data", ".git"}:
                raise ValueError("Official package contains user data or development history")
            if str(relative).casefold() in {name.casefold() for name in files}:
                raise ValueError("Duplicate application file")
            files[str(relative)] = archive.read(item)
    output = Path(output)
    if output.exists():
        raise FileExistsError(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        def put(name, data):
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, data, compresslevel=9)
        put("tool.json", json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"))
        put("bin/run.cmd", b'@echo off\r\npowershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0run.ps1"\r\nexit /b %ERRORLEVEL%\r\n')
        put("bin/run.ps1", launcher_script(tool["exe"]).encode("utf-8-sig"))
        put("首次升级说明.txt", ("首次从旧版工具箱包升级前，请先备份 bin/app/runtime 与 bin/app/exports。\n"
            "将其分别复制到工具根目录 config/runtime 与 data/exports 后再安装新版。\n"
            "本适配包退出时自动保存上述数据，之后更新会保留 config 和 data。\n"
            "请通过工具箱入口或 bin/run.cmd 启动，升级前关闭工具。WebView 缓存不迁移。\n").encode("utf-8"))
        for name, data in sorted(files.items()):
            put("bin/app/" + name, data)
    manifest["versions"][0].update(sha256=hashlib.sha256(output.read_bytes()).hexdigest(), size=output.stat().st_size)
    return manifest


def register_manifest(root, incoming):
    root = Path(root)
    tool_id = incoming["id"]
    if tool_id not in TOOLS or incoming["entry"] != "bin/run.cmd":
        raise ValueError("Unexpected toolbox manifest")
    path = root / "tools" / (tool_id + ".json")
    version = incoming["latest"]["stable"]
    entry = next(item for item in incoming["versions"] if item["version"] == version)
    if path.exists():
        saved = json.loads(path.read_text(encoding="utf-8"))
        latest = saved["latest"]["stable"]
        if version in saved.get("blockedVersions", []) or version_key(version) < version_key(latest):
            return False
        previous = next((item for item in saved["versions"] if item["version"] == version), None)
        if previous:
            if previous["sha256"] != entry["sha256"] or previous["size"] != entry["size"]:
                raise ValueError("Existing version has different package bytes")
            return False
        if saved["entry"] != incoming["entry"]:
            saved["blockedVersions"] = list(dict.fromkeys(saved.get("blockedVersions", []) +
                [item["version"] for item in saved["versions"]]))
        saved["versions"].append(copy.deepcopy(entry))
        saved["latest"]["stable"] = version
        saved["entry"] = incoming["entry"]
    else:
        saved = copy.deepcopy(incoming)
    index_path = root / "index.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    if not any(item["id"] == tool_id for item in index["tools"]):
        index["tools"].append({"id": tool_id, "name": saved["name"], "category": saved["category"],
                               "manifestUrl": f"tools/{tool_id}.json"})
        write_json(index_path, index)
    write_json(path, saved)
    return True


def migrate_legacy_data(tool_root):
    tool_root = Path(tool_root)
    count = 0
    for source, target in ((tool_root / "bin/app/runtime", tool_root / "config/runtime"),
                           (tool_root / "bin/app/exports", tool_root / "data/exports")):
        if not source.is_dir():
            continue
        for file in source.rglob("*"):
            relative = file.relative_to(source)
            if not file.is_file() or relative.parts[0].casefold() == "webview":
                continue
            destination = target / relative
            if destination.exists():
                if destination.read_bytes() != file.read_bytes():
                    raise ValueError(f"Migration conflict; preserve both files for review: {relative}")
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(file, destination)
            count += 1
    return count


def gh_json(*arguments):
    return json.loads(subprocess.check_output(["gh", *arguments], encoding="utf-8"))


def same_package_contents(first, second):
    with zipfile.ZipFile(first) as left, zipfile.ZipFile(second) as right:
        if sorted(left.namelist()) != sorted(right.namelist()):
            return False
        for name in left.namelist():
            with left.open(name) as a, right.open(name) as b:
                while True:
                    chunk = a.read(1024 * 1024)
                    if chunk != b.read(1024 * 1024):
                        return False
                    if not chunk:
                        break
    return True


def release_info(tool_id, tag=None):
    args = ["release", "view"] + ([tag] if tag else [])
    info = gh_json(*args, "--repo", TOOLS[tool_id]["repo"], "--json",
                   "tagName,isDraft,isPrerelease,publishedAt,body,assets")
    if info["isDraft"] or info["isPrerelease"]:
        raise ValueError("Only published stable releases are supported")
    stable_version(info["tagName"])
    return info


def download_asset(tool_id, info, name, folder):
    asset = next((item for item in info["assets"] if item["name"] == name and item["state"] == "uploaded"), None)
    if not asset:
        raise ValueError(f"Release attachment is not ready: {name}")
    subprocess.run(["gh", "release", "download", info["tagName"], "--repo", TOOLS[tool_id]["repo"],
                    "--pattern", name, "--dir", str(folder)], check=True)
    path = Path(folder) / name
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if path.stat().st_size != asset["size"] or asset.get("digest") not in (None, "", "sha256:" + digest):
        raise ValueError("Release attachment digest or size mismatch")
    return path


def package_release(tool_id, tag, output_dir, upload=False):
    info = release_info(tool_id, tag)
    version = stable_version(info["tagName"])
    name = package_name(tool_id, version)
    changelog = ["复用官方成品；新增工具箱启动与用户配置、导出数据保存适配。", "原版更新说明：https://github.com/" + TOOLS[tool_id]["repo"] + "/releases/tag/" + info["tagName"]]
    with tempfile.TemporaryDirectory() as temporary:
        source = download_asset(tool_id, info, TOOLS[tool_id]["asset"].format(version=version), temporary)
        output = Path(output_dir) / name
        manifest = build_package(source, tool_id, version, output, info["publishedAt"][:10], changelog)
    if upload:
        existing = next((item for item in info["assets"] if item["name"] == name), None)
        if existing:
            if existing.get("digest") != "sha256:" + manifest["versions"][0]["sha256"]:
                with tempfile.TemporaryDirectory() as temporary:
                    previous = download_asset(tool_id, info, name, temporary)
                    if not same_package_contents(output, previous):
                        raise ValueError("Existing toolbox attachment has different contents; refusing to overwrite")
                    shutil.copyfile(previous, output)
                manifest["versions"][0].update(sha256=hashlib.sha256(output.read_bytes()).hexdigest(), size=output.stat().st_size)
        else:
            subprocess.run(["gh", "release", "upload", info["tagName"], str(output), "--repo", TOOLS[tool_id]["repo"]], check=True)
    write_json(Path(output_dir) / (tool_id + "-manifest.json"), manifest)
    print(json.dumps(manifest["versions"][0], ensure_ascii=True))


def sync_registry(root):
    for tool_id in TOOLS:
        info = release_info(tool_id)
        version = stable_version(info["tagName"])
        name = package_name(tool_id, version)
        asset = next((item for item in info["assets"] if item["name"] == name and item["state"] == "uploaded"), None)
        if not asset:
            print(f"{tool_id}: waiting for {name}")
            continue
        current_path = Path(root) / "tools" / (tool_id + ".json")
        if current_path.exists():
            current = json.loads(current_path.read_text(encoding="utf-8"))
            if version in current.get("blockedVersions", []) or version_key(version) < version_key(current["latest"]["stable"]):
                print(f"{tool_id}: blocked or older release, skipped")
                continue
            registered = next((item for item in current["versions"] if item["version"] == version), None)
            if registered and asset.get("digest"):
                if asset["digest"] != "sha256:" + registered["sha256"] or asset["size"] != registered["size"]:
                    raise ValueError("Registered attachment changed; refusing same-version replacement")
                print(f"{tool_id}: {version} already registered")
                continue
        with tempfile.TemporaryDirectory() as temporary:
            archive_path = download_asset(tool_id, info, name, temporary)
            with zipfile.ZipFile(archive_path) as archive:
                manifest = json.loads(archive.read("tool.json"))
                if manifest["id"] != tool_id or manifest["latest"]["stable"] != version:
                    raise ValueError("Attachment tool/version mismatch")
                if manifest["entry"] not in archive.namelist() or f'bin/app/{TOOLS[tool_id]["exe"]}' not in archive.namelist():
                    raise ValueError("Incomplete toolbox attachment")
            manifest["versions"][0].update(sha256=hashlib.sha256(archive_path.read_bytes()).hexdigest(), size=archive_path.stat().st_size)
            changed = register_manifest(root, manifest)
            print(f"{tool_id}: {version}, changed={changed}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    package = commands.add_parser("package")
    package.add_argument("--tool-id", choices=TOOLS, required=True)
    package.add_argument("--tag", required=True)
    package.add_argument("--output-dir", type=Path, required=True)
    package.add_argument("--upload", action="store_true")
    sync = commands.add_parser("sync")
    sync.add_argument("--root", type=Path, default=Path.cwd())
    migrate = commands.add_parser("migrate")
    migrate.add_argument("--tool-directory", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "package":
        package_release(args.tool_id, args.tag, args.output_dir, args.upload)
    elif args.command == "sync":
        sync_registry(args.root)
    else:
        print(f"Migrated {migrate_legacy_data(args.tool_directory)} files without deleting the originals")


if __name__ == "__main__":
    main()
