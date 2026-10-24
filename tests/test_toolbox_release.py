import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import zipfile

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "toolbox_release.py"


def integration():
    assert SCRIPT.is_file(), "the toolbox release adapter must exist"
    spec = importlib.util.spec_from_file_location("toolbox_release", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def source_archive(tmp_path, tool_id="dnf_skill_prism"):
    exe = {"dnf_skill_prism": "dnf_skill_prism.exe", "rep_player": "rep_player.exe",
           "neople_video_player": "NeopleFrame.exe"}[tool_id]
    archive = tmp_path / "original.zip"
    payload = {exe: b"original executable", "resources/required.dll": b"original dependency",
               "resources/licenses/notice.txt": b"original license"}
    with zipfile.ZipFile(archive, "w") as out:
        for name, data in payload.items():
            out.writestr("release/" + name, data)
    return archive, payload


@pytest.mark.parametrize("tool_id", ["dnf_skill_prism", "rep_player", "neople_video_player"])
def test_package_reuses_every_original_byte_and_has_toolbox_root(tmp_path, tool_id):
    module = integration()
    original, payload = source_archive(tmp_path, tool_id)
    output = tmp_path / "toolbox.zip"
    result = module.build_package(original, tool_id, "1.2.3", output, "2026-10-20", ["update"])
    with zipfile.ZipFile(output) as archive:
        manifest = json.loads(archive.read("tool.json"))
        assert manifest["id"] == tool_id
        assert manifest["entry"] == "bin/run.cmd"
        assert manifest["latest"]["stable"] == "1.2.3"
        assert "bin/run.ps1" in archive.namelist()
        for name, data in payload.items():
            assert archive.read("bin/app/" + name) == data
        assert not any(name.startswith(("config/", "data/")) for name in archive.namelist())
    assert len(result["versions"][0]["sha256"]) == 64
    assert result["versions"][0]["size"] == output.stat().st_size
    second = tmp_path / "second.zip"
    module.build_package(original, tool_id, "1.2.3", second, "2026-10-20", ["update"])
    assert second.read_bytes() == output.read_bytes(), "retries must reproduce the same attachment"


def test_neople_manifest_registers_as_video_player(tmp_path):
    module = integration()
    original, _ = source_archive(tmp_path, "neople_video_player")
    manifest = module.build_package(original, "neople_video_player", "1.0.0",
                                    tmp_path / "neople.zip", "2026-10-25", ["首次发布。"])
    assert manifest["projectUrl"] == "https://github.com/PhysicalWorldDo/neople-video-player"
    assert manifest["iconSource"] == "bin/app/NeopleFrame.exe"
    assert "BK2" in manifest["tags"] and "AVI" in manifest["tags"]
    module.write_json(tmp_path / "registry/index.json", {"schemaVersion": 1, "tools": []})
    assert module.register_manifest(tmp_path / "registry", manifest)
    index = json.loads((tmp_path / "registry/index.json").read_text(encoding="utf-8"))
    assert index["tools"][0]["manifestUrl"] == "tools/neople_video_player.json"


@pytest.mark.parametrize("name", ["release/../bad", "release/runtime/settings.json"])
def test_rejects_unsafe_or_user_data_in_official_package(tmp_path, name):
    module = integration()
    original, _ = source_archive(tmp_path)
    with zipfile.ZipFile(original, "a") as archive:
        archive.writestr(name, "must never ship")
    with pytest.raises(ValueError):
        module.build_package(original, "dnf_skill_prism", "1.2.3", tmp_path / "out.zip", "2026-10-20", [])


def test_retries_recognize_identical_payloads_with_different_zip_compression(tmp_path):
    module = integration()
    original, _ = source_archive(tmp_path)
    packed = tmp_path / "packed.zip"
    module.build_package(original, "dnf_skill_prism", "1.2.3", packed, "2026-10-20", [])
    plain = tmp_path / "plain.zip"
    with zipfile.ZipFile(packed) as source, zipfile.ZipFile(plain, "w") as target:
        for name in source.namelist():
            target.writestr(name, source.read(name))
    assert packed.read_bytes() != plain.read_bytes()
    assert module.same_package_contents(packed, plain)
    changed = tmp_path / "changed.zip"
    with zipfile.ZipFile(packed) as source, zipfile.ZipFile(changed, "w") as target:
        for name in source.namelist():
            target.writestr(name, b"different" if name == "bin/run.ps1" else source.read(name))
    assert not module.same_package_contents(packed, changed)


def test_registry_appends_history_and_changes_index_only_for_new_tool(tmp_path):
    module = integration()
    (tmp_path / "tools").mkdir()
    index = {"schemaVersion": 1, "toolbox": {"latestVersion": "0.2.0"}, "tools": [
        {"id": "rep_player", "name": "REP 播放器", "category": "资源工具", "manifestUrl": "tools/rep_player.json"}]}
    (tmp_path / "index.json").write_text(json.dumps(index), encoding="utf-8")
    old = module.tool_manifest("rep_player", "1.1.3", "2026-10-11", ["old"])
    old["entry"] = "bin/app/rep_player.exe"
    old["versions"][0].update(sha256="a" * 64, size=1)
    module.write_json(tmp_path / "tools/rep_player.json", old)
    current = module.tool_manifest("rep_player", "1.1.5", "2026-10-20", ["new"])
    current["versions"][0].update(sha256="b" * 64, size=2)
    assert module.register_manifest(tmp_path, current)
    assert json.loads((tmp_path / "index.json").read_text(encoding="utf-8")) == index
    saved = json.loads((tmp_path / "tools/rep_player.json").read_text(encoding="utf-8"))
    assert [v["version"] for v in saved["versions"]] == ["1.1.3", "1.1.5"]
    assert saved["latest"]["stable"] == "1.1.5"
    assert saved["blockedVersions"] == ["1.1.3"], "legacy packages without the shared launcher cannot be offered under the new global entry"
    assert not module.register_manifest(tmp_path, current)
    old["entry"] = "bin/run.cmd"
    assert not module.register_manifest(tmp_path, old), "an older event must not downgrade stable"
    prism = module.tool_manifest("dnf_skill_prism", "1.1.2", "2026-10-20", [])
    assert module.register_manifest(tmp_path, prism)
    new_index = json.loads((tmp_path / "index.json").read_text(encoding="utf-8"))
    assert new_index["toolbox"] == index["toolbox"]
    assert [item["id"] for item in new_index["tools"]] == ["rep_player", "dnf_skill_prism"]
    current["versions"][0]["sha256"] = "c" * 64
    with pytest.raises(ValueError, match="different"):
        module.register_manifest(tmp_path, current)


def test_registry_never_promotes_a_blocked_release(tmp_path):
    module = integration()
    module.write_json(tmp_path / "index.json", {"tools": []})
    current = module.tool_manifest("rep_player", "1.1.5", "2026-10-20", [])
    current["blockedVersions"] = ["1.1.6"]
    module.register_manifest(tmp_path, current)
    blocked = module.tool_manifest("rep_player", "1.1.6", "2026-10-21", [])
    assert not module.register_manifest(tmp_path, blocked)
    assert json.loads((tmp_path / "tools/rep_player.json").read_text(encoding="utf-8"))["latest"]["stable"] == "1.1.5"


def test_periodic_sync_does_not_redownload_unchanged_packages(tmp_path, monkeypatch):
    module = integration()
    metadata = {}
    for tool_id in module.TOOLS:
        saved = module.tool_manifest(tool_id, "1.2.3", "2026-10-20", [])
        saved["versions"][0].update(sha256="a" * 64, size=123)
        module.write_json(tmp_path / "tools" / (tool_id + ".json"), saved)
        metadata[tool_id] = {"tagName": "v1.2.3", "assets": [{"name": module.package_name(tool_id, "1.2.3"),
            "state": "uploaded", "digest": "sha256:" + "a" * 64, "size": 123}]}
    monkeypatch.setattr(module, "release_info", lambda tool_id: metadata[tool_id])
    def unexpected_download(*args):
        pytest.fail("unchanged releases must use metadata without downloading full binaries")
    monkeypatch.setattr(module, "download_asset", unexpected_download)
    module.sync_registry(tmp_path)


def test_cli_report_prints_chinese_metadata_on_cp1252_console(tmp_path, monkeypatch):
    module = integration()
    original, _ = source_archive(tmp_path)
    metadata = {"tagName": "v1.2.3", "publishedAt": "2026-10-20T00:00:00Z", "assets": []}
    monkeypatch.setattr(module, "release_info", lambda *args: metadata)
    monkeypatch.setattr(module, "download_asset", lambda *args: original)
    buffer = io.BytesIO()
    console = io.TextIOWrapper(buffer, encoding="cp1252")
    monkeypatch.setattr(sys, "stdout", console)
    module.package_release("dnf_skill_prism", "v1.2.3", tmp_path / "output")
    console.flush()
    emitted = json.loads(buffer.getvalue().decode("cp1252"))
    assert emitted["changelog"][0].startswith("复用官方成品")
    saved = json.loads((tmp_path / "output/dnf_skill_prism-manifest.json").read_text(encoding="utf-8"))
    assert saved["versions"][0]["changelog"] == emitted["changelog"]


@pytest.mark.skipif(os.name != "nt", reason="Windows launcher")
def test_launcher_restores_and_saves_runtime_exports_even_on_nonzero_exit(tmp_path):
    module = integration()
    root = tmp_path / "中文 tool"
    app = root / "bin/app"
    app.mkdir(parents=True)
    (root / "config/runtime").mkdir(parents=True)
    (root / "data/exports").mkdir(parents=True)
    (root / "config/runtime/user-presets.json").write_text("personal", encoding="utf-8")
    (root / "data/exports/old.txt").write_text("keep", encoding="utf-8")
    (app / "probe.cmd").write_text('@echo off\nif not exist runtime\\user-presets.json exit /b 31\n'
                                  'echo changed>runtime\\settings.json\n'
                                  'echo result>exports\\new.txt\nexit /b 7\n', encoding="ascii")
    (root / "bin/run.ps1").write_text(module.launcher_script("probe.cmd"), encoding="utf-8-sig")
    run = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                          str(root / "bin/run.ps1"), "-Hidden"], capture_output=True, timeout=30)
    assert run.returncode == 7, run.stderr.decode(errors="replace")
    assert (root / "config/runtime/user-presets.json").read_text(encoding="utf-8") == "personal"
    assert (root / "config/runtime/settings.json").read_text().strip() == "changed"
    assert (root / "data/exports/old.txt").read_text() == "keep"
    assert (root / "data/exports/new.txt").read_text().strip() == "result"


@pytest.mark.skipif(os.name != "nt", reason="Windows launcher")
def test_second_launch_cannot_replace_data_of_a_running_session(tmp_path):
    module = integration()
    root = tmp_path / "two launches"
    app = root / "bin/app"
    app.mkdir(parents=True)
    (root / "data/exports").mkdir(parents=True)
    (app / "probe.cmd").write_text('@echo off\necho launch>>starts.txt\necho personal>exports\\new.txt\n'
                                  'ping -n 6 127.0.0.1 >nul\nif not exist exports\\new.txt exit /b 45\nexit /b 0\n', encoding="ascii")
    script = root / "bin/run.ps1"
    script.write_text(module.launcher_script("probe.cmd"), encoding="utf-8-sig")
    command = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), "-Hidden"]
    first = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        for _ in range(100):
            if (app / "exports/new.txt").is_file():
                break
            time.sleep(.05)
        assert (app / "exports/new.txt").is_file()
        second = subprocess.run(command, capture_output=True, timeout=12, creationflags=subprocess.CREATE_NO_WINDOW)
        stdout, stderr = first.communicate(timeout=15)
        assert first.returncode == 0, stderr.decode(errors="replace")
        assert second.returncode == 0, second.stderr.decode(errors="replace")
        assert (app / "starts.txt").read_text().splitlines() == ["launch"], "a second session must not run or mirror data"
        assert (root / "data/exports/new.txt").read_text().strip() == "personal"
    finally:
        if first.poll() is None:
            subprocess.run(["taskkill", "/PID", str(first.pid), "/T", "/F"], capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)


def test_legacy_migration_then_real_package_manager_update_preserves_data(tmp_path):
    module = integration()
    sys.path.insert(0, os.environ.get("TOOLBOX_SOURCE", str(Path(__file__).resolve().parents[3] / "DNFtools")))
    from physical_toolbox.package_manager import PackageManager
    from physical_toolbox.manifest import ToolManifest
    from physical_toolbox.install_state import InstallStateStore
    workspace = tmp_path / "toolbox"
    target = workspace / "tools/rep_player"
    old_runtime = target / "bin/app/runtime"
    old_runtime.mkdir(parents=True)
    (old_runtime / "client.txt").write_bytes(b"private client setting")
    (target / "bin/app/exports").mkdir()
    (target / "bin/app/exports/personal.png").write_bytes(b"personal export")
    assert module.migrate_legacy_data(target) == 2
    original, _ = source_archive(tmp_path, "rep_player")
    output = tmp_path / "new.zip"
    manifest = module.build_package(original, "rep_player", "1.1.5", output, "2026-10-20", [])
    manager = PackageManager(workspace, InstallStateStore(workspace / "config/installed.json"))
    manager.install_from_archive(ToolManifest.from_dict(manifest), "1.1.5", output)
    assert (target / "config/runtime/client.txt").read_bytes() == b"private client setting"
    assert (target / "data/exports/personal.png").read_bytes() == b"personal export"
    assert not (target / "bin/app/runtime").exists()
    assert manager.installed_tools()["rep_player"].version == "1.1.5"
