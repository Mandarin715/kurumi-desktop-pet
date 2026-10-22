"""Configuration and file resolution, independent of the GUI."""
import json
import logging
import math
from pathlib import Path
from dataclasses import dataclass, field
from fx_logic import validated_policy
from okx_api import validate_options


@dataclass
class PetConfig:
    default: int
    duration: int
    lines: dict[int, list[str]]
    fx: dict
    volume: float = .6
    data_source: str = 'ocr'
    okx: dict = field(default_factory=validate_options)
    config_path: Path | None = field(default=None, repr=False)


def load_config(external: Path, bundled: Path) -> PetConfig:
    """Try external config first; a malformed override must not prevent startup."""
    for path in dict.fromkeys((external / 'kurumi.json', external / 'assets' / 'kurumi.json',
                               bundled / 'kurumi.json')):
        try:
            data = json.loads(path.read_text(encoding='utf-8-sig'))
            if not isinstance(data, dict):
                raise ValueError('配置最外层必须是 JSON 对象')
            default, duration = data.get('default', 10), data.get('duration', 4000)
            if type(default) is not int or not 1 <= default <= 14:
                raise ValueError('default 必须是 1～14 的整数')
            if type(duration) is not int or not 100 <= duration <= 600000:
                raise ValueError('duration 必须是 100～600000 毫秒的整数')
            lines = {}
            for number in range(1, 15):
                choices = data['lines'][str(number)]
                if not isinstance(choices, list) or not choices or not all(
                        isinstance(line, str) and line.strip() for line in choices):
                    raise ValueError(f'第 {number} 张图的台词必须是非空字符串数组')
                lines[number] = choices
            try:
                policy = validated_policy(data.get('fx'))
            except ValueError as error:
                logging.warning('FX 配置无效，使用默认规则：%s', error)
                policy = validated_policy()
            sound = data.get('sound', {})
            volume = sound.get('volume', .6) if isinstance(sound, dict) else None
            if type(volume) not in (int, float) or not math.isfinite(volume) or not 0 <= volume <= 1:
                logging.warning('音量配置无效，使用默认值 0.6（有效范围为 0～1）')
                volume = .6
            source = data.get('data_source', 'ocr')
            if source not in ('ocr', 'okx'):
                source = 'ocr'
            try:
                okx = validate_options(data.get('okx'))
            except ValueError:
                logging.warning('OKX 公共设置无效，使用默认配置')
                okx = validate_options()
            return PetConfig(default, duration, lines, policy, float(volume), source, okx, path)
        except FileNotFoundError:
            continue
        except (OSError, ValueError, KeyError, TypeError) as error:
            logging.warning('配置 %s: %s', path, error)
    raise ValueError('没有可用的 kurumi.json，请恢复程序附带的配置文件。')


def image_candidates(number: int, external: Path, bundled: Path):
    filename = f'kurumi_{number:02d}.png'
    return dict.fromkeys((external / 'assets' / filename, bundled / 'assets' / filename))


def atomic_json(path, data):
    """No backup copies containing credentials; temporary file is replaced atomically."""
    import os
    temporary = path.with_name(path.name + '.tmp')
    try:
        with temporary.open('w', encoding='utf-8') as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write('\n')
        os.replace(temporary, path)
    finally:
        if temporary.is_file():
            temporary.unlink()


def save_options(config, external, bundled, source, okx, fx=None):
    path = config.config_path
    # Bundled one-file resources are temporary/read-only; create an external copy.
    if path not in (external / 'kurumi.json', external / 'assets' / 'kurumi.json'):
        path = external / 'kurumi.json'
    template = path if path.is_file() else bundled / 'kurumi.json'
    try:
        data = json.loads(template.read_text(encoding='utf-8-sig'))
        if not isinstance(data, dict) or source not in ('ocr', 'okx'):
            raise ValueError()
        options = validate_options(okx)
        # Merge only known edited fields, retaining future/user extension keys.
        raw_okx = data.get('okx', {})
        data.update({'data_source': source,
                     'okx': {**(raw_okx if isinstance(raw_okx, dict) else {}), **options}})
        if fx is not None:
            policy = validated_policy(fx)
            raw_fx = data.get('fx', {})
            data['fx'] = {**(raw_fx if isinstance(raw_fx, dict) else {}), **policy}
        atomic_json(path, data)
    except (ValueError, OSError, TypeError):
        raise ValueError('无法保存 kurumi.json，请检查文件格式或目录写入权限') from None
    config.data_source, config.okx, config.config_path = source, options, path
    if fx is not None:
        config.fx = policy
