"""ISO9660/Joliet writer for the offline bundle and NoCloud seed; stdlib only.

No boot code, symlinks, Rock Ridge, host mounts or arbitrary file discovery.
Linux mounts the upper-case ISO identifiers as lower case with map=normal.
"""
import math
from pathlib import Path
import re
import shutil

BLOCK = 2048
DATE = bytes((126, 1, 1, 0, 0, 0, 0))


def both(value, size=4):
    return value.to_bytes(size, 'little') + value.to_bytes(size, 'big')


def record(name, extent, length, directory=False):
    result = bytearray(33 + len(name) + (len(name) % 2 == 0))
    result[0] = len(result)
    result[2:10] = both(extent)
    result[10:18] = both(length)
    result[18:25] = DATE
    result[25] = 2 if directory else 0
    result[28:32] = both(1, 2)
    result[32] = len(name)
    result[33:33 + len(name)] = name
    return bytes(result)


def create(bundle, destination, names, volume='TDB_UPDATE'):
    names = sorted(names)
    if (volume not in ('TDB_UPDATE', 'CIDATA') or any(not re.fullmatch(
            r'(?:tools/|src/)?[a-z_]+\.(?:py|json)|user-data|meta-data', name) for name in names)):
        raise ValueError('ISO only supports the fixed bundle names')
    directories = ['', 'src', 'tools']
    trees = [{name: 23 + i + tree*3 for i, name in enumerate(directories)} for tree in range(2)]
    offset = 29
    files = {}
    for name in names:
        size = (bundle / name).stat().st_size
        files[name] = (offset, size)
        offset += max(1, math.ceil(size / BLOCK))
    descriptors, paths, contents = [], [], []
    for tree, extents in enumerate(trees):
        def encode(name):
            return name.encode('utf-16-be') if tree else name.upper().encode()
        pvd = bytearray(BLOCK)
        pvd[:7] = bytes((2 if tree else 1,)) + b'CD001\x01'
        pvd[8:40] = b'TDB_HARNESS'.ljust(32, b' ')
        pvd[40:72] = encode(volume).ljust(32, b'\0' if tree else b' ')
        if tree:
            pvd[88:91] = b'%/E'
        pvd[80:88] = both(offset)
        pvd[120:124], pvd[124:128], pvd[128:132] = both(1, 2), both(1, 2), both(BLOCK, 2)
        for endian in ('little', 'big'):
            table = bytearray()
            for name in directories:
                ident = encode(name) if name else b'\0'
                table += bytes((len(ident), 0)) + extents[name].to_bytes(4, endian) + (1).to_bytes(2, endian)
                table += ident + (b'\0' if len(ident) % 2 else b'')
            paths.append(table)
        pvd[132:140] = both(len(paths[-1]))
        pvd[140:144] = (19+tree*2).to_bytes(4, 'little')
        pvd[148:152] = (20+tree*2).to_bytes(4, 'big')
        pvd[156:190] = record(b'\0', extents[''], BLOCK, True)
        pvd[881] = 1
        descriptors.append(pvd)
        for directory in directories:
            entries = record(b'\0', extents[directory], BLOCK, True) + record(b'\x01', extents[''], BLOCK, True)
            if not directory:
                for sub in directories[1:]:
                    entries += record(encode(sub), extents[sub], BLOCK, True)
            for name, (extent, size) in files.items():
                if str(Path(name).parent).replace('\\', '/') == (directory or '.'):
                    entries += record(encode(Path(name).name + ';1'), extent, size)
            if len(entries) > BLOCK:
                raise ValueError('bundle directory exceeds ISO layout')
            contents.append(entries)
    with destination.open('xb') as output:
        output.write(bytes(BLOCK * 16))
        for descriptor in descriptors:
            output.write(descriptor)
        output.write((b'\xffCD001\x01').ljust(BLOCK, b'\0'))
        for table in paths:
            output.write(table.ljust(BLOCK, b'\0'))
        for entries in contents:
            output.write(entries.ljust(BLOCK, b'\0'))
        for name, (_, size) in files.items():
            with (bundle / name).open('rb') as source:
                shutil.copyfileobj(source, output, 512 * 1024)
            output.write(bytes(max(1, math.ceil(size / BLOCK)) * BLOCK - size))
