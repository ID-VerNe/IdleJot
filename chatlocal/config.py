import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# 本地数据根与 water-group/daemon/wg_paths.py 同一约定（那边是单一事实来源；
# 这边不能 flat import 到 daemon 目录，按同规则复述一份，改规则时两边一起改）。
WG_HOME = Path(os.environ.get('WATER_GROUP_HOME') or (Path.home() / '.water-group'))
DATA = WG_HOME / 'data'
DB = DATA / 'chats.sqlite3'
TMP_DIR = WG_HOME / 'tmp'
# 私有化人格卡与规则住在数据根，仓库里只留公开模板（同 wg_paths.personas_dir/rules_dir）。
PRIVATE_PERSONAS = WG_HOME / 'personas'
PRIVATE_RULES = WG_HOME / 'rules'
for folder in (DATA, TMP_DIR, PRIVATE_PERSONAS, PRIVATE_RULES, ROOT / '.cache', ROOT / 'imports', ROOT / '.tmp'):
    # parents=True：全新机器上 ~/.water-group 本身不存在，缺了它首启即
    # FileNotFoundError（CI 全新 runner 上 check_mcp.py 就是这么挂的）。
    folder.mkdir(parents=True, exist_ok=True)
os.environ['GRADIO_ANALYTICS_ENABLED'] = 'False'
os.environ['GRADIO_TEMP_DIR'] = str(TMP_DIR / 'gradio')
os.environ['HF_HOME'] = str(WG_HOME / 'cache' / 'huggingface')
os.environ['TEMP'] = str(TMP_DIR)
os.environ['TMP'] = str(TMP_DIR)


# Bounds catch typos without silently changing the requested budget.
AGENT_LIMITS = {
    'AGENT_MAX_REQUESTS': (12, 2, 40),
    'AGENT_MAX_TOOL_CALLS': (30, 1, 100),
    'AGENT_MAX_MESSAGES': (300, 1, 1000),
    'AGENT_MAX_EVIDENCE_CHARS': (72000, 1000, 240000),
    'AGENT_MAX_REQUEST_CHARS': (200000, 16000, 600000),
    'AGENT_MAX_SECONDS': (600, 30, 1800),
    'AGENT_MAX_OUTPUT_TOKENS': (16384, 1024, 65536),
    'AGENT_STREAM_IDLE_SECONDS': (90, 20, 300),
}


def settings():
    from dotenv import dotenv_values
    values = dotenv_values(ROOT / '.env')
    return {key: os.environ.get(key, values.get(key) or default).strip()
            for key, default in [('API_BASE', 'https://api.deepseek.com'),
                                 ('API_KEY', ''), ('MODEL', 'deepseek-flash'),
                                 ('AGENT_REASONING_EFFORT', 'high'),
                                 *((k,str(v[0])) for k,v in AGENT_LIMITS.items())]}


def agent_options(config):
    result={}
    for key,(default,low,high) in AGENT_LIMITS.items():
        value=config.get(key,default)
        try:
            if isinstance(value,bool): raise ValueError()
            parsed=int(str(value))
            if not low<=parsed<=high: raise ValueError()
        except (ValueError,TypeError):
            raise ValueError(f'{key} 必须是 {low}–{high} 之间的整数。') from None
        result[key.removeprefix('AGENT_').lower()]=parsed
    effort=str(config.get('AGENT_REASONING_EFFORT','high')).strip().lower()
    if effort not in ('none','low','high','max'):
        raise ValueError('AGENT_REASONING_EFFORT 必须为 none、low、high 或 max。')
    result['reasoning_effort']=effort
    if result['max_request_chars']<result['max_evidence_chars']+16000:
        raise ValueError('AGENT_MAX_REQUEST_CHARS 至少比 AGENT_MAX_EVIDENCE_CHARS 多 16000，给工具说明及历史留出空间。')
    return result


def resolve_path(value):
    """Expand and absolutise a path against the program directory."""
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = ROOT / path
    return path.resolve()


def local_path(value):
    """Resolve an import file, refusing anything outside the program directory."""
    path = resolve_path(value)
    if not path.is_relative_to(ROOT):
        raise ValueError('请先把导出文件放入程序目录下的 imports 文件夹；仅访问当前工作目录内的导入文件。')
    return path


def wg_config(root=None):
    """Read-only cached water-group config.json. Single source for onebot/ws/sender
    endpoints and self_account. Falls back to {} so callers fall through to .env.

    ``root`` defaults to ROOT but callers pass a (possibly patched) root so test
    isolation holds: a test that patches chatlocal.onebot.ROOT to a temp dir must
    not pick up the real repo's water-group/config.json.
    """
    path = (root or ROOT) / 'water-group' / 'config.json'
    try:
        return json.loads(path.read_text('utf-8'))
    except (OSError, ValueError):
        return {}
