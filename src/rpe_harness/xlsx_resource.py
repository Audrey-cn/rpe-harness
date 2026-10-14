"""Streaming XLSX resource admission before any object-expanding workbook parser.

This module does not calculate business results. Both producer and oracle invoke
it independently on the original ZIP bytes, before calling normal openpyxl.
"""
from __future__ import annotations

import hashlib
import io
from pathlib import Path, PurePosixPath
import posixpath
import re
from urllib.parse import unquote, urlsplit
from xml.parsers import expat
from zipfile import BadZipFile, ZipFile

RESOURCE_REVISION = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
MAX_INPUT_BYTES = 16 * 1024 * 1024
MAX_UNPACKED_BYTES = 64 * 1024 * 1024
MAX_XML_BYTES = 16 * 1024 * 1024
MAX_MEMBERS = 2048
MAX_XML_NODES = 1000000
MAX_XML_DEPTH = 64
MAX_SHEET_GRID = 100000
MAX_WORKBOOK_GRID = 200000
MAX_RANGE_TEXT = 4096
_COORDINATE = re.compile(r'\$?([A-Za-z]{1,3})\$?([1-9][0-9]{0,6})\Z')


def _local(name):
    return name.rsplit('}', 1)[-1]


def _coordinate(value):
    if not isinstance(value, str) or len(value) > 16:
        raise ValueError('worksheet cell coordinate is invalid')
    match = _COORDINATE.fullmatch(value)
    if match is None:
        raise ValueError('worksheet cell coordinate is invalid')
    column = 0
    for char in match[1].upper():
        column = column * 26 + ord(char) - ord('A') + 1
    row = int(match[2])
    if column > 16384 or row > 1048576:
        raise ValueError('worksheet cell coordinate exceeds Excel bounds')
    return row, column


def _range(value):
    if not isinstance(value, str) or len(value) > MAX_RANGE_TEXT:
        raise ValueError('worksheet range text exceeds its budget')
    parts = value.split(':')
    if not 1 <= len(parts) <= 2:
        raise ValueError('worksheet range is invalid')
    start = _coordinate(parts[0])
    end = _coordinate(parts[-1])
    if end[0] < start[0] or end[1] < start[1]:
        raise ValueError('worksheet range is reversed')
    return end[0], end[1], (end[0] - start[0] + 1) * (end[1] - start[1] + 1)


def _looks_xml(prefix):
    # Detect XML even under a non-.xml ZIP member name. UTF-16/32 declarations
    # and BOMs are passed to Expat, whose declaration/entity gates still apply.
    stripped = prefix.lstrip(b' \t\r\n')
    return (stripped.startswith((b'<', b'\xef\xbb\xbf', b'\xff\xfe', b'\xfe\xff',
                                 b'\x00<', b'\x00\x00\xfe\xff', b'\x00\x00\x00<')))


def _relationship_source(name):
    parts = PurePosixPath(name).parts
    if len(parts) < 2 or parts[-2] != '_rels' or not parts[-1].endswith('.rels'):
        raise ValueError('workbook relationship part path is invalid')
    return '/'.join((*parts[:-2], parts[-1][:-5])).rstrip('/')


def _target(source_part, target):
    url = urlsplit(target)
    if url.scheme or url.netloc or url.query or url.fragment:
        raise ValueError('workbook external or non-package relationship target is forbidden')
    value = unquote(url.path)
    if '\\' in value or '\x00' in value:
        raise ValueError('workbook relationship target is invalid')
    result = posixpath.normpath(value.lstrip('/') if value.startswith('/') else
                               posixpath.join(posixpath.dirname(source_part), value))
    if result in {'.', '..'} or result.startswith('../'):
        raise ValueError('workbook relationship target escapes the package')
    return result


def preflight_xlsx(raw):
    """Bound decompression, XML nodes/depth and every range that can allocate cells."""
    if type(raw) is not bytes or len(raw) > MAX_INPUT_BYTES:
        raise ValueError('XLSX bytes exceed their 16 MiB resource budget')
    total_nodes = 0
    total_grid = 0
    total_expansion = 0
    worksheet_count = 0
    try:
        with ZipFile(io.BytesIO(raw)) as archive:
            members = archive.infolist()
            if len(members) > MAX_MEMBERS or sum(m.file_size for m in members) > MAX_UNPACKED_BYTES:
                raise ValueError('workbook ZIP exceeds member or expanded-byte budget')
            names = [member.filename for member in members]
            if len(names) != len(set(names)):
                raise ValueError('workbook ZIP contains duplicate members')
            worksheet_targets, workbook_targets = set(), set()
            workbook_sheet_targets = set()
            relationships = {}
            processed, checked_worksheets, queued = set(), set(), set()
            # Package declarations and relationship maps come first. A worksheet
            # cannot evade bounds by changing its filename, root name or rel.Type.
            scan_members = sorted(members, key=lambda member: (
                0 if member.filename == '[Content_Types].xml' else
                1 if member.filename.lower().endswith('.rels') else 2))
            for member in scan_members:
                name = member.filename
                if (member.flag_bits & 1 or '\\' in name or name.startswith('/')
                        or '..' in PurePosixPath(name).parts):
                    raise ValueError('workbook ZIP contains encrypted or unsafe members')
                if member.is_dir():
                    continue
                processed.add(name)
                with archive.open(member) as stream:
                    prefix = stream.read(65536)
                    # Unknown extensions cannot hide XML behind arbitrarily long whitespace.
                    while prefix and not prefix.strip(b' \t\r\n') and len(prefix) <= MAX_XML_BYTES:
                        extra = stream.read(65536)
                        if not extra:
                            break
                        prefix += extra
                    is_xml = (name.lower().endswith(('.xml', '.rels')) or _looks_xml(prefix)
                              or name in worksheet_targets or name in workbook_targets)
                    if not is_xml:
                        continue
                    if member.file_size > MAX_XML_BYTES or len(prefix) > MAX_XML_BYTES:
                        raise ValueError('workbook XML member exceeds 16 MiB byte budget')
                    parser = expat.ParserCreate(namespace_separator='}')
                    expected_worksheet = (name in worksheet_targets or
                        name.startswith('xl/worksheets/') and '/_rels/' not in name)
                    state = {'depth': 0, 'worksheet': expected_worksheet,
                             'workbook': name in workbook_targets, 'max_row': 1, 'max_col': 1,
                             'cells': set(), 'merge_area': 0, 'expansion': 0}

                    def forbidden(*args):
                        raise ValueError('workbook XML DTD/entities/external references are forbidden')

                    def bounds(row, column):
                        state['max_row'] = max(state['max_row'], row)
                        state['max_col'] = max(state['max_col'], column)
                        if state['max_row'] * state['max_col'] > MAX_SHEET_GRID:
                            raise ValueError('worksheet grid exceeds 100000-cell resource budget')

                    def start(element, attributes):
                        nonlocal total_nodes
                        state['depth'] += 1
                        total_nodes += 1
                        if total_nodes > MAX_XML_NODES or state['depth'] > MAX_XML_DEPTH:
                            raise ValueError('workbook XML node/depth resource budget exceeded')
                        tag = _local(element)
                        attrs = {_local(key): value for key, value in attributes.items()}
                        if len(attrs) != len(attributes):
                            raise ValueError('worksheet/package XML has ambiguous attribute namespaces')
                        if state['depth'] == 1:
                            if expected_worksheet and tag != 'worksheet':
                                raise ValueError('declared worksheet XML has an invalid root')
                            if state['workbook'] and tag != 'workbook':
                                raise ValueError('declared workbook XML has an invalid root')
                            state['worksheet'] = state['worksheet'] or tag == 'worksheet'
                            state['workbook'] = state['workbook'] or tag == 'workbook'
                        if tag == 'Relationship' and attrs.get('TargetMode', '').lower() == 'external':
                            raise ValueError('workbook external relationships are not supported')
                        if tag == 'Relationship':
                            source_part = _relationship_source(name)
                            if not {'Id', 'Type', 'Target'} <= attrs.keys():
                                raise ValueError('workbook relationship is incomplete')
                            target = _target(source_part, attrs['Target'])
                            mapping = relationships.setdefault(source_part, {})
                            if attrs['Id'] in mapping:
                                raise ValueError('workbook relationship id is duplicated')
                            mapping[attrs['Id']] = (target, attrs['Type'])
                            if attrs['Type'].endswith('/officeDocument'):
                                workbook_targets.add(target)
                            elif attrs['Type'].endswith('/worksheet'):
                                worksheet_targets.add(target)
                        if tag == 'Override' and {'PartName', 'ContentType'} <= attrs.keys():
                            target = _target('', attrs['PartName'])
                            content_type = attrs['ContentType'].lower()
                            if content_type.endswith('.worksheet+xml'):
                                worksheet_targets.add(target)
                            elif any(value in content_type for value in
                                     ('.sheet.main+xml', '.template.main+xml',
                                      '.sheet.macroenabled.main+xml', '.template.macroenabled.main+xml')):
                                workbook_targets.add(target)
                        if state['workbook'] and tag == 'sheet':
                            relationship = relationships.get(name, {}).get(attrs.get('id'))
                            if relationship is None:
                                raise ValueError('workbook sheet relationship is unavailable')
                            target, relation_type = relationship
                            if 'chartsheet' not in relation_type:
                                if target in workbook_sheet_targets:
                                    raise ValueError('workbook worksheet target is duplicated/shared')
                                workbook_sheet_targets.add(target)
                                worksheet_targets.add(target)
                                if target not in names:
                                    raise ValueError('workbook worksheet part is unavailable')
                                if target in processed and target not in checked_worksheets and target not in queued:
                                    scan_members.append(archive.getinfo(target))
                                    queued.add(target)
                        if not state['worksheet']:
                            return
                        if tag == 'c':
                            coordinate = attrs.get('r')
                            row, column = _coordinate(coordinate)
                            if coordinate in state['cells'] or len(state['cells']) >= MAX_SHEET_GRID:
                                raise ValueError('worksheet cell count/duplicate resource budget exceeded')
                            state['cells'].add(coordinate)
                            bounds(row, column)
                        elif tag == 'row' and 'r' in attrs:
                            value = attrs['r']
                            if not re.fullmatch(r'[1-9][0-9]{0,6}', value) or int(value) > 1048576:
                                raise ValueError('worksheet row coordinate is invalid')
                            bounds(int(value), 1)
                        # dimension, mergeCell, hyperlink, formula, filter and selection
                        # refs all undergo coordinate validation before openpyxl sees them.
                        for key in ('ref', 'sqref'):
                            if key not in attrs:
                                continue
                            value = attrs[key]
                            if len(value) > MAX_RANGE_TEXT:
                                raise ValueError('worksheet range text exceeds its resource budget')
                            refs = value.split() if key == 'sqref' else [value]
                            if not refs:
                                raise ValueError('worksheet range is empty')
                            for ref in refs:
                                row, column, area = _range(ref)
                                bounds(row, column)
                                if tag in {'mergeCell', 'hyperlink'}:
                                    state['expansion'] += area
                                    if state['expansion'] > MAX_SHEET_GRID:
                                        raise ValueError('worksheet merged/hyperlink grid expansion budget exceeded')
                                    if tag == 'mergeCell':
                                        state['merge_area'] += area
                        if tag in {'dimension', 'mergeCell', 'hyperlink'} and 'ref' not in attrs:
                            raise ValueError('worksheet dimension/merge/hyperlink lacks an explicit range')

                    def end(element):
                        state['depth'] -= 1

                    parser.StartElementHandler = start
                    parser.EndElementHandler = end
                    parser.StartDoctypeDeclHandler = forbidden
                    parser.EntityDeclHandler = forbidden
                    parser.ExternalEntityRefHandler = forbidden
                    parser.SkippedEntityHandler = forbidden
                    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
                    count = len(prefix)
                    parser.Parse(prefix, False)
                    while True:
                        chunk = stream.read(65536)
                        if not chunk:
                            break
                        count += len(chunk)
                        if count > MAX_XML_BYTES:
                            raise ValueError('workbook XML member exceeds its streamed byte budget')
                        parser.Parse(chunk, False)
                    parser.Parse(b'', True)
                    if state['worksheet']:
                        checked_worksheets.add(name)
                        worksheet_count += 1
                        total_grid += state['max_row'] * state['max_col']
                        total_expansion += state['expansion']
                        if total_grid > MAX_WORKBOOK_GRID or total_expansion > MAX_WORKBOOK_GRID:
                            raise ValueError('workbook cumulative grid exceeds 200000-cell resource budget')
            if worksheet_count == 0:
                raise ValueError('workbook contains no resource-checked worksheet XML')
            if worksheet_targets - checked_worksheets:
                raise ValueError('workbook has a worksheet part without resource admission')
            return {'members': len(members), 'xml_nodes': total_nodes, 'worksheets': worksheet_count,
                    'grid_cells': total_grid, 'expanded_range_cells': total_expansion,
                    'resource_revision': RESOURCE_REVISION}
    except (BadZipFile, expat.ExpatError, OSError, RuntimeError) as error:
        raise ValueError('invalid bounded XLSX package/XML') from error
