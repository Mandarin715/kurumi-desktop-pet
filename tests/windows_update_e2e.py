"""Explicit Windows smoke test for the FROZEN helper, using harmless fixture exes.

Run after packaging: python tests/windows_update_e2e.py dist/Kurumi.exe report.json
It never launches a real pet, reads a personal config or checks GitHub.
"""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from updater import digest_file, launch_program, prepare_install


CS = r'''
using System;
using System.Diagnostics;
using System.IO;
using System.Threading;
class Fixture {
    const string Mode = "MODE_VALUE";
    static int Main(string[] args) {
        string exe = Process.GetCurrentProcess().MainModule.FileName;
        if (args.Length == 1 && args[0] == "--hold") { Thread.Sleep(60000); return 0; }
        if (Mode == "bad") return 7;
        if (args.Length == 2 && args[0] == "--update-receipt") {
            string escaped = exe.Replace("\\", "\\\\").Replace("\"", "\\\"");
            File.WriteAllText(args[1], "{\"executable\":\""+escaped+"\",\"pid\":"+Process.GetCurrentProcess().Id+"}");
            Thread.Sleep(60000);
            return 0;
        }
        File.WriteAllText(Path.Combine(Path.GetDirectoryName(exe), "old-started"), Mode);
        return 0;
    }
}
'''


def wait_file(path, process, seconds=20):
    deadline = time.monotonic()+seconds
    while time.monotonic() < deadline:
        if path.is_file():
            return
        if process.poll() is not None:
            raise RuntimeError(f'Helper exited early: {process.returncode}')
        time.sleep(.1)
    raise TimeoutError(path.name)


def run(frozen, report):
    compiler = Path('C:/Windows/Microsoft.NET/Framework64/v4.0.30319/csc.exe')
    results = {}
    with tempfile.TemporaryDirectory(prefix='update-e2e-', dir=ROOT/'build') as temporary:
        folder = Path(temporary)
        for mode in ('old', 'good', 'bad'):
            source = folder/(mode+'.cs')
            source.write_text(CS.replace('MODE_VALUE', mode), encoding='utf-8')
            subprocess.run([str(compiler), '/nologo', '/target:winexe', '/out:'+str(folder/(mode+'.exe')),
                            str(source)], check=True, capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
        for success in (True, False):
            case = folder/('success' if success else 'rollback')
            install, work = case/'install', case/'KurumiUpdate-test'
            install.mkdir(parents=True)
            (work/'stage').mkdir(parents=True)
            target = install/'Kurumi.exe'
            shutil.copy2(folder/'old.exe', target)
            old_hash = digest_file(target)
            shutil.copy2(folder/('good.exe' if success else 'bad.exe'), work/'stage'/'Kurumi.exe')
            (install/'使用说明.txt').write_bytes(b'old docs')
            (work/'stage'/'使用说明.txt').write_bytes(b'new docs')
            personal = {'kurumi.json': b'PERSONAL CONFIG', 'okx.local.json': b'PERSONAL KEYS',
                        'custom.png': b'CUSTOM IMAGE'}
            for name, value in personal.items():
                (install/name).write_bytes(value)
            files = {name: digest_file(work/'stage'/name) for name in ('Kurumi.exe', '使用说明.txt')}
            parent = launch_program(target, ['--hold'])
            helper = None
            child_pid = None
            try:
                plan, helper_path = prepare_install(work, target, files, parent_pid=parent.pid, executable=frozen)
                helper = launch_program(helper_path, ['--apply-update', plan])
                wait_file(work/'helper-ready', helper)
                # The helper must not overwrite the still-running old executable.
                assert digest_file(target) == old_hash
                parent.terminate()
                parent.wait(timeout=5)
                helper.wait(timeout=45)
                result = json.loads((work/'result.json').read_text())
                assert result['ok'] is success
                assert helper.returncode == (0 if success else 1)
                if success:
                    receipt = json.loads((work/'started.json').read_text())
                    child_pid = receipt['pid']
                    assert digest_file(target) == files['Kurumi.exe']
                    assert (install/'使用说明.txt').read_bytes() == b'new docs'
                else:
                    assert digest_file(target) == old_hash
                    assert (install/'使用说明.txt').read_bytes() == b'old docs'
                    deadline = time.monotonic()+5
                    while not (install/'old-started').exists() and time.monotonic() < deadline:
                        time.sleep(.1)
                    assert (install/'old-started').read_text() == 'old'
                assert all((install/name).read_bytes() == value for name, value in personal.items())
                results['install_restart' if success else 'failed_start_rollback'] = True
            finally:
                if parent.poll() is None:
                    parent.terminate()
                    parent.wait(timeout=5)
                if child_pid is None and (work/'started.json').is_file():
                    child_pid = json.loads((work/'started.json').read_text()).get('pid')
                if child_pid:
                    subprocess.run(['taskkill', '/PID', str(child_pid), '/T', '/F'],
                                   capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW, timeout=10)
                if helper and helper.poll() is None:
                    subprocess.run(['taskkill', '/PID', str(helper.pid), '/T', '/F'],
                                   capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW, timeout=10)
                    helper.wait(timeout=10)
                time.sleep(.3)
    report = Path(report)
    report.write_text(json.dumps({'ok': True, **results, 'personal_files_preserved': True}, indent=2))
    print(report.read_text())


if __name__ == '__main__':
    run(Path(sys.argv[1]).resolve(), sys.argv[2])
