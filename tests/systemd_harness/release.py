"""Fixed migration: c77b5f7 harness / 0a9a453 controller -> 75d1654 controller."""
BASE = '75d16545c5ccbdb965b7dfd2cd73dd605f6f2e0d'
OLD_BASE = '0a9a453aa975615df1c0c0b6ca835f277d7dfe1c'
OLD_HARNESS = 'c77b5f77063bcce279d9645a16a5d60c5b243bbe'
SOURCE_HASHES = {
    'tools/daily_backup.py': 'c26582430fed01ce8388c765f50151d414acc9f4925c0665b4ad91c6514f3ff9',
    'tools/backup_restore.py': '15693effe54eeff44cef4eda5b43d9c1a258df2836d55999077a75832a9359e7',
    'tools/offsite_backup.py': 'ec3e71814e7739e1a8720904ad4f0240149a0821ac8f925f2ad4acbd5eab4641',
    'src/maintenance.py': '990c10b548d62fe77c96e26f4682de15439e3418d20473397a33aee3914d877a',
}
SOURCE = tuple(SOURCE_HASHES)
HARNESS = ('guard.py', 'guest.py', 'synthetic.py', 'scenarios.py', 'release.py', 'update.py', 'auto_guest.py', 'wire.py')
# Old Windows bundles read the four harness modules from the working checkout.
# Permit exactly the reviewed LF or CRLF bytes, never arbitrary normalized code.
OLD_HASHES = {
    'tools/daily_backup.py': ['074b3971adc5d0add9bc9974f0862e7c8402c0430aab6cf22aa3481a591938a1'],
    **{p: [h] for p, h in SOURCE_HASHES.items() if p != 'tools/daily_backup.py'},
    'guard.py': ['8f756db4a7b47ed2374a1e4b26e7add02de4fcba4ee3a31613e7af8baf0815d9', '5bacfb2033a5660484564821a5dc01e7d5d8fd2b7e9239d26c8ae9f18d2d3d4c'],
    'guest.py': ['def6f03c2dd283dbf273319427da9c89b7cd1d07121078f9998555e62edb6746', 'a990be8fdfceceb05baf79085beda9659a44e4f0a5c322aaa49d53c03784ab8a'],
    'synthetic.py': ['3bcfd0db8e671195966ac18a18a9d3c4b5d6d46015011bc2d1b05fa42163c022', '31807237c0d399aa8c62ceef281846867c5949b3c27b8b855b70d18810688b1a'],
    'scenarios.py': ['17bdcbbdede4382be43ecdf2f247a29d43c4b7c2979940e1762153eb7894ad2b', '0b393c950910732b59cdd950922d25bd97d119d1b0f9ccf9ffadd1d7f0c41826'],
}


def wrapper(name, code='/var/lib/tdb-harness/code'):
    return f'''import sys
sys.path.insert(0, {code!r})
import synthetic
if __name__ == '__main__':
    synthetic.tool({name!r}, sys.argv[1:])
else:
    _real = synthetic.load({(code + '/tools/' + name)!r}, 'real_{name[:-3]}')
    globals().update({{k: v for k, v in vars(_real).items() if not k.startswith('__')}})
'''.encode()
