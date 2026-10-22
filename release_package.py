"""Build a public Windows archive, preserving local dist configuration verbatim."""
import argparse
import json
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from app_version import VERSION, RELEASE_NOTES
from pet_audio import SOUND_NAMES
from updater import digest_file
from pet_core import public_default_config


def build_package(root, version=VERSION):
    root = Path(root)
    dist = root/'dist'
    executable = dist/'Kurumi.exe'
    if not executable.is_file():
        raise ValueError('Build dist/Kurumi.exe first')
    tutorial = dist/'久留美桌宠-使用教程.docx'
    if not tutorial.is_file():
        raise ValueError('Latest tutorial is missing from dist')
    public_files = {'Kurumi.exe': executable, '久留美桌宠-使用教程.docx': tutorial}
    public_files.update({name: root/name for name in ('README.md', '使用说明.txt', 'LICENSES.txt')})
    # Public config is constructed from shipped defaults, never from dist or
    # okx.local.json. Only the explicitly public dialogue/image configuration is read.
    public_config = public_default_config(root/'kurumi.json')
    manifest = {'version': version, 'platform': 'windows-x64',
                'files': {name: digest_file(path) for name, path in public_files.items()}}
    archive = dist/f'Kurumi-v{version}-Windows-x64.zip'
    with ZipFile(archive, 'w', compression=ZIP_DEFLATED, compresslevel=6) as output:
        for name, path in public_files.items():
            output.write(path, 'Kurumi/'+name)
        for number in range(1, 15):
            name = f'assets/kurumi_{number:02d}.png'
            output.write(root/name, 'Kurumi/'+name)
        for sound in SOUND_NAMES:
            name = f'assets/sfx/{sound}.mp3'
            output.write(root/name, 'Kurumi/'+name)
        output.writestr('Kurumi/kurumi.json', json.dumps(public_config, ensure_ascii=False, indent=2)+'\n')
        output.writestr('Kurumi/update-manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2)+'\n')
    checksum = dist/'SHA256SUMS.txt'
    checksum.write_text(f'{digest_file(archive)}  {archive.name}\n', encoding='utf-8')
    (dist/f'RELEASE-NOTES-v{version}.md').write_text(RELEASE_NOTES+'\n', encoding='utf-8')
    return archive, checksum


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parent)
    arguments = parser.parse_args()
    for path in build_package(arguments.root):
        print(path)
