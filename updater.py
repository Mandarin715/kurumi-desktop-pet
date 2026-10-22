"""Public GitHub updates and a local transactional installer. No pet config access."""
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zipfile import ZipFile

from app_version import VERSION

REPOSITORY = 'Mandarin715/kurumi-desktop-pet'
LATEST_URL = f'https://api.github.com/repos/{REPOSITORY}/releases/latest'
BAIDU_URL = 'https://pan.baidu.com/s/1Fuasa1-ZIbr1AAknZswXqA?pwd=g64g'
QUARK_URL = 'https://pan.quark.cn/s/3d36119ca037'
UPDATE_FILES = frozenset(('Kurumi.exe', 'README.md', '使用说明.txt', 'LICENSES.txt',
                          '久留美桌宠-使用教程.docx'))
MAX_DOWNLOAD = 512 * 1024 * 1024
REDIRECT_HOSTS = frozenset(('github.com', 'api.github.com', 'release-assets.githubusercontent.com',
                             'objects.githubusercontent.com', 'github-releases.githubusercontent.com'))


class UpdateError(Exception):
    """Only fixed, user-facing text crosses worker boundaries."""


class Cancelled(UpdateError):
    pass


def version_tuple(value):
    if not isinstance(value, str) or not re.fullmatch(r'v?\d{1,6}\.\d{1,6}\.\d{1,6}', value):
        raise ValueError('Invalid stable version')
    return tuple(int(part) for part in value.removeprefix('v').split('.'))


def digest_file(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def safe_download_url(url, tag, filename):
    return url == f'https://github.com/{REPOSITORY}/releases/download/{tag}/{filename}'


class GitHubRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urlsplit(newurl)
        if (parsed.scheme != 'https' or parsed.hostname not in REDIRECT_HOSTS
                or parsed.username or parsed.password or parsed.port not in (None, 443)):
            raise UpdateError('下载地址无效，请使用网盘下载。')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def open_public(url, timeout=6):
    # Fresh unauthenticated opener: never inherit the OKX client or its headers.
    return build_opener(GitHubRedirects()).open(Request(url, headers={
        'User-Agent': f'KurumiPet/{VERSION}', 'Accept': 'application/vnd.github+json'
        if url == LATEST_URL else 'application/octet-stream'}), timeout=timeout)


def read_public(url, limit, opener=open_public):
    with opener(url) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise UpdateError('更新信息过大，请使用网盘下载。')
    return data


@dataclass(frozen=True)
class Release:
    version: str
    tag: str
    notes: str
    filename: str
    url: str
    size: int
    sha256: str


def parse_release(data, current=VERSION, skipped='', checksum_loader=None):
    if not isinstance(data, dict) or data.get('draft') or data.get('prerelease'):
        return None
    tag = data.get('tag_name')
    try:
        newer = version_tuple(tag) > version_tuple(current)
    except ValueError:
        return None
    version = tag.removeprefix('v')
    if not newer or version == str(skipped).removeprefix('v'):
        return None
    filename = f'Kurumi-v{version}-Windows-x64.zip'
    assets = data.get('assets', [])
    if not isinstance(assets, list):
        return None
    matches = [asset for asset in assets if isinstance(asset, dict) and asset.get('name') == filename]
    if len(matches) != 1:
        return None
    asset = matches[0]
    url, size = asset.get('browser_download_url'), asset.get('size')
    if not safe_download_url(url, tag, filename) or type(size) is not int or not 0 < size <= MAX_DOWNLOAD:
        return None
    digest = asset.get('digest') or ''
    sha256 = digest[7:].lower() if isinstance(digest, str) and digest.startswith('sha256:') else ''
    if not re.fullmatch(r'[0-9a-f]{64}', sha256) and checksum_loader:
        checksums = [item for item in assets if isinstance(item, dict) and item.get('name') == 'SHA256SUMS.txt']
        if len(checksums) == 1 and safe_download_url(checksums[0].get('browser_download_url'), tag, 'SHA256SUMS.txt'):
            raw = checksum_loader(checksums[0]['browser_download_url'])
            for line in raw.decode('utf-8-sig').splitlines():
                match = re.fullmatch(r'([0-9a-fA-F]{64})\s+\*?(.+)', line)
                if match and match[2] == filename:
                    sha256 = match[1].lower()
                    break
    if not re.fullmatch(r'[0-9a-f]{64}', sha256):
        return None  # Never offer an automatic install without an expected checksum.
    notes = data.get('body')
    notes = notes[:32000] if isinstance(notes, str) and notes.strip() else '此版本暂无更新说明。'
    return Release(version, tag, notes, filename, url, size, sha256)


def check_latest(current=VERSION, skipped='', opener=open_public):
    try:
        data = json.loads(read_public(LATEST_URL, 1024*1024, opener))
        return parse_release(data, current, skipped,
                             lambda url: read_public(url, 16384, opener))
    except Exception:
        return None  # Startup checks are deliberately silent, including malformed releases.


def cleanup_finished_sessions(base=None):
    """Reclaim helpers left locked during install, only in completed owned sessions."""
    base = Path(base or tempfile.gettempdir()).resolve()
    for folder in base.glob('KurumiUpdate-*'):
        try:
            if (not folder.is_dir() or folder.is_symlink() or folder.is_junction()
                    or folder.resolve().parent != base or time.time()-folder.stat().st_mtime < 300):
                continue
            result = json.loads((folder/'result.json').read_text(encoding='utf-8'))
            plan = json.loads((folder/'install.json').read_text(encoding='utf-8'))
            if (type(result.get('ok')) is bool and Path(plan['target']).name == 'Kurumi.exe'
                    and 'Kurumi.exe' in plan['files'] and set(plan['files']) <= UPDATE_FILES):
                shutil.rmtree(folder, ignore_errors=True)
        except (OSError, ValueError, KeyError, TypeError):
            pass


def slow_download(elapsed, idle, received):
    return idle >= 20 or (elapsed >= 30 and received / max(elapsed, 1) < 100*1024)


def download_release(release, destination, cancel, progress=lambda *_: None, opener=open_public):
    destination = Path(destination)
    received, checksum = 0, hashlib.sha256()
    try:
        if cancel.is_set():
            raise Cancelled()
        with opener(release.url, timeout=25) as response, destination.open('wb') as stream:
            # read1 yields available bytes rather than waiting for a full 64 KiB,
            # keeping low-bandwidth progress and the idle timer accurate.
            read = getattr(response, 'read1', response.read)
            while True:
                if cancel.is_set():
                    raise Cancelled()
                chunk = read(64*1024)
                if not chunk:
                    break
                received += len(chunk)
                if received > release.size or received > MAX_DOWNLOAD:
                    raise UpdateError('下载文件大小不符，请使用网盘下载。')
                stream.write(chunk)
                checksum.update(chunk)
                progress(received, release.size)
        if cancel.is_set():
            raise Cancelled()
        if received != release.size or checksum.hexdigest() != release.sha256:
            raise UpdateError('下载校验未通过，请重试或使用网盘下载。')
        return destination
    except Cancelled:
        destination.unlink(missing_ok=True)
        raise
    except Exception as error:
        destination.unlink(missing_ok=True)
        if isinstance(error, UpdateError):
            raise
        raise UpdateError('GitHub 下载失败，可使用百度网盘或夸克网盘下载。') from None


def manifest_files(folder):
    folder = Path(folder)
    return {name: digest_file(folder/name) for name in sorted(UPDATE_FILES) if (folder/name).is_file()}


def stage_archive(archive, destination, version):
    """Read the archive without extractall; write only exact allowlisted filenames."""
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False)
    try:
        with ZipFile(archive) as source:
            entries = source.infolist()
            if len(entries) > 256 or sum(entry.file_size for entry in entries) > MAX_DOWNLOAD:
                raise ValueError()
            mapping = {}
            for entry in entries:
                name = entry.filename
                parts = PurePosixPath(name).parts
                if ('\\' in name or ':' in name or not parts or parts[0] != 'Kurumi'
                        or any(part in ('.', '..') for part in parts) or name.startswith('/')
                        or stat.S_ISLNK(entry.external_attr >> 16) or entry.flag_bits & 1):
                    raise ValueError()
                if name in mapping:
                    raise ValueError()
                mapping[name] = entry
            manifest_info = mapping['Kurumi/update-manifest.json']
            if manifest_info.file_size > 16384:
                raise ValueError()
            manifest = json.loads(source.read(manifest_info))
            files = manifest['files']
            if (manifest['version'] != version or manifest.get('platform') != 'windows-x64'
                    or not isinstance(files, dict) or 'Kurumi.exe' not in files
                    or not set(files) <= UPDATE_FILES):
                raise ValueError()
            for name, expected in files.items():
                if not isinstance(expected, str) or not re.fullmatch(r'[0-9a-f]{64}', expected):
                    raise ValueError()
                entry = mapping['Kurumi/'+name]
                with source.open(entry) as stream, (destination/name).open('wb') as target:
                    shutil.copyfileobj(stream, target, 64*1024)
                if digest_file(destination/name) != expected:
                    raise ValueError()
            with (destination/'Kurumi.exe').open('rb') as executable:
                if executable.read(2) != b'MZ':
                    raise ValueError()
        return files
    except Exception:
        shutil.rmtree(destination)
        raise UpdateError('更新包结构或文件校验失败，请使用网盘下载。') from None


def clean_environment():
    environment = os.environ.copy()
    environment['PYINSTALLER_RESET_ENVIRONMENT'] = '1'
    return environment


def launch_program(executable, arguments=()):
    return subprocess.Popen([str(executable), *map(str, arguments)], cwd=str(Path(executable).parent),
                            env=clean_environment(), close_fds=True,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))


def prepare_install(work, target, files, parent_pid=None, executable=None):
    """Copy our own frozen executable as the helper, never a script from the network."""
    work, target = Path(work).resolve(), Path(target).resolve()
    executable = Path(executable or sys.executable).resolve()
    if target.name != 'Kurumi.exe' or not target.is_file() or not files or not set(files) <= UPDATE_FILES:
        raise UpdateError('无法准备自动更新，请使用网盘下载。')
    # Prove directory write access before closing the current pet.
    with tempfile.TemporaryFile(dir=target.parent):
        pass
    helper = work/'KurumiUpdater.exe'
    shutil.copy2(executable, helper)
    plan = {'target': str(target), 'files': files,
            'parent_pid': parent_pid or os.getpid()}
    plan_path = work/'install.json'
    plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding='utf-8')
    return plan_path, helper


def wait_for_process(pid, timeout=45):
    if sys.platform != 'win32':
        return True
    import ctypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [ctypes.c_uint, ctypes.c_bool, ctypes.c_uint]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel.OpenProcess(0x00100000, False, pid)
    if not handle:
        return ctypes.get_last_error() == 87  # Already exited, not access-denied.
    try:
        return kernel.WaitForSingleObject(handle, round(timeout*1000)) == 0
    finally:
        kernel.CloseHandle(handle)


def replace_retry(source, target, seconds=15):
    deadline = time.monotonic()+seconds
    while True:
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(.2)


def install_transaction(work, target, files, launch=launch_program, health_timeout=40):
    """Back up only owned program files; restore every replaced file on failure."""
    work, target = Path(work).resolve(), Path(target).resolve()
    stage, backup = work/'stage', work/'backup'
    if target.name != 'Kurumi.exe' or not target.is_file() or 'Kurumi.exe' not in files or not set(files) <= UPDATE_FILES:
        raise UpdateError('Invalid update plan')
    for name, digest in files.items():
        if not isinstance(digest, str) or digest_file(stage/name) != digest:
            raise UpdateError('Staged file changed')
        if (target.parent/name).is_symlink() or (target.parent/name).is_junction():
            raise UpdateError('Invalid update target')
    backup.mkdir(exist_ok=False)
    existed, attempted, process = set(), [], None
    receipt = work/'started.json'
    receipt.unlink(missing_ok=True)
    try:
        for name in files:
            old = target.parent/name
            if old.exists():
                shutil.copy2(old, backup/name)
                existed.add(name)
        # Replace exe last; all incoming files first get a sibling temporary
        # file so os.replace remains atomic even across different disk volumes.
        for name in sorted(files, key=lambda n: n == 'Kurumi.exe'):
            temporary = target.parent/(name+'.kurumi-update-tmp')
            try:
                shutil.copy2(stage/name, temporary)
                attempted.append(name)
                replace_retry(temporary, target.parent/name)
            finally:
                temporary.unlink(missing_ok=True)
        process = launch(target, ['--update-receipt', receipt])
        deadline = time.monotonic()+health_timeout
        while time.monotonic() < deadline:
            if receipt.is_file():
                # The real app writes this only after creating and showing pet.
                result = json.loads(receipt.read_text(encoding='utf-8'))
                if result.get('executable') == str(target) and process.poll() is None:
                    return True
            if process.poll() is not None:
                break
            time.sleep(.1)
        raise UpdateError('New version did not start')
    except Exception:
        if process is not None and process.poll() is None:
            # Terminate bootloader + child before restoring its locked binary.
            if sys.platform == 'win32':
                subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                               creationflags=subprocess.CREATE_NO_WINDOW, capture_output=True, timeout=10)
            else:
                process.terminate()
            process.wait(timeout=10)
        for name in reversed(attempted):
            if name in existed:
                temporary = target.parent/(name+'.kurumi-update-tmp')
                shutil.copy2(backup/name, temporary)
                replace_retry(temporary, target.parent/name)
            else:
                (target.parent/name).unlink(missing_ok=True)
        launch(target)
        return False


def run_installer(plan_path):
    """Special frozen entry point, before the normal single-instance mutex."""
    parent_exited = False
    target = None
    try:
        plan_path = Path(plan_path).resolve()
        if plan_path.parent != Path(sys.executable).resolve().parent or plan_path.name != 'install.json':
            return 1
        plan = json.loads(plan_path.read_text(encoding='utf-8'))
        if type(plan['parent_pid']) is not int or plan['parent_pid'] <= 0:
            return 1
        target = Path(plan['target']).resolve()
        if target.name != 'Kurumi.exe' or not target.is_file() or 'Kurumi.exe' not in plan['files']:
            return 1
        if not set(plan['files']) <= UPDATE_FILES:
            return 1
        for name, digest in plan['files'].items():
            if digest_file(plan_path.parent/'stage'/name) != digest:
                return 1
        (plan_path.parent/'helper-ready').touch()
        if not wait_for_process(plan['parent_pid']):
            return 1
        parent_exited = True
        ok = install_transaction(plan_path.parent, plan['target'], plan['files'])
        (plan_path.parent/'result.json').write_text(json.dumps({'ok': ok}), encoding='utf-8')
        # The running helper itself is locked on Windows; remove large payloads
        # and backups only after a successful install or completed rollback.
        for name in ('stage', 'backup'):
            shutil.rmtree(plan_path.parent/name, ignore_errors=True)
        (plan_path.parent/'download.zip').unlink(missing_ok=True)
        return 0 if ok else 1
    except Exception:
        if parent_exited and target is not None and target.is_file():
            try:
                launch_program(target)
            except Exception:
                pass
        return 1  # No logs containing paths or user data; original binary remains.


def write_start_receipt(path):
    """Only acknowledge the helper session targeting this exact executable."""
    path = Path(path).resolve()
    if path.name != 'started.json' or not path.parent.name.startswith('KurumiUpdate-'):
        return
    try:
        plan = json.loads((path.parent/'install.json').read_text(encoding='utf-8'))
        target = Path(sys.executable).resolve()
        if Path(plan['target']).resolve() != target:
            return
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps({'executable': str(target), 'version': VERSION}), encoding='utf-8')
        os.replace(temporary, path)
    except (OSError, ValueError, KeyError, TypeError):
        pass
