"""Bounded checks of literal values in already mapped review requirements.

This optional gate does not classify requests, fill omissions, rewrite drafts or
prove semantic completeness. The caller still validates the ordinary proposal
schema and authenticates context sources. A null quote remains a human-reviewed
proposal of a default, never proof that the request supplied no explicit value.
"""
from __future__ import annotations

import codecs
import copy
import json
import re

from .capability_catalog import _canonical
from .context import SourceRef
from .review_quotes import resolve_quote_ref, source_quote_anchors


VALUE_EVIDENCE_CONTRACT = (
    'Literal parameter evidence values-v1: Keep the ordinary five-field draft schema. '
    'For each mapped encoding or delimiter, select a short exact literal quote, or an exact '
    'published quote_ref whose text contains one unambiguous supported literal value. '
    'The typed execution value must equal that literal after its registered normalization. '
    'Encoding aliases such as UTF-8 and utf8 are equivalent; utf-8-sig is a different '
    'encoding and must never replace an explicit UTF-8 request as a default. '
    'For TAB, execution.delimiter is the real one-character TAB value; its source witness '
    'may be an exact TAB character, the literal name TAB or 制表符, or a quoted JSON TAB escape. '
    'A comma witness may use the literal name comma or 逗号. Other delimiter characters '
    'need an exact one-character quote or a complete quoted JSON character. '
    'For keep, mode and style, retain supported literal values such as first, rows and pretty '
    'when the source supplies them; free prose alone is not deterministic value evidence. '
    'Explicit attachment facts must retain their verified source_ref and exact source quote. '
    'A proposed default must be a separate requirement with quote=null and remains subject '
    'to human review; it never proves that the full request omitted an explicit value. '
    'Map only fields actually present in this draft and supported by the selected source. '
    'Do not invent literal witnesses or claim complete natural-language understanding. '
    'Missing or conflicting evidence blocks confirmation and execution until resolved.')


_PARAMETERS = {
    'tabular.deduplicate': {'encoding': 'codec', 'delimiter': 'character', 'keep': ('first',)},
    'json.normalize': {'style': ('minify', 'pretty')},
    'xlsx.inspect': {'mode': ('rows', 'sheets', 'formulas')},
}
_TOKENS = re.compile(r'[A-Za-z][A-Za-z0-9_.-]*')
_DELIMITERS = {'tab': '\t', 'comma': ',', 'semicolon': ';', 'pipe': '|',
               '制表符': '\t', '逗号': ',', '分号': ';', '竖线': '|'}
_COVERAGE_LIMITS = [
    'Only literal values in model-mapped registered parameter rows are checked; '
    'unmapped instructions and semantic completeness still require human review.',
    'A proposed default does not prove that the source omitted an explicit value.',
    'Free prose without a supported enum literal remains unverified.',
]


def _canonical_value(value, specification):
    if type(value) is not str:
        raise ValueError('parameter must be a string')
    if specification == 'codec':
        try:
            return codecs.lookup(value).name
        except LookupError as error:
            raise ValueError('encoding name is not supported') from error
    if specification == 'character':
        if len(value) != 1 or value in '\r\n\x00':
            raise ValueError('delimiter must be one non-newline character')
    elif value not in specification:
        raise ValueError('parameter is not a registered enum value')
    return value


def _literal_values(quote, specification):
    values = set()
    tokens = [token for token in _TOKENS.findall(quote) if len(token) <= 128]
    if specification == 'codec':
        for token in tokens:
            try:
                values.add(codecs.lookup(token).name)
            except LookupError:
                pass
    elif specification == 'character':
        if len(quote) == 1 and quote not in '\r\n\x00':
            values.add(quote)
        # A complete quoted JSON character is an explicit literal, not prose.
        if quote.startswith('"') and quote.endswith('"'):
            try:
                value = json.loads(quote)
                if type(value) is str and len(value) == 1 and value not in '\r\n\x00':
                    values.add(value)
            except (ValueError, RecursionError):
                pass
        values.update(_DELIMITERS[token.casefold()] for token in tokens
                      if token.casefold() in _DELIMITERS)
        values.update(value for token, value in _DELIMITERS.items()
                      if not token.isascii() and token in quote)
    else:
        values.update(token for token in tokens if token in specification)
    return sorted(values)


def _sources(sources):
    if sources is None:
        return {}, []
    if type(sources) is not dict or len(sources) > 8:
        raise ValueError('at most eight already verified context sources are allowed')
    contents, anchors = {}, []
    for key, content in sources.items():
        if type(content) is not dict:
            raise ValueError('invalid verified context source')
        ref = SourceRef.from_dict({name: content[name]
                                  for name in ('source', 'event_id', 'sha256')}).to_dict()
        text, facts = content['payload']['text'], content['facts']
        if (key != _canonical(ref) or type(text) is not str or len(text.encode()) > 65536
                or type(facts) is not dict
                or len(_canonical(facts)) > 16384):
            raise ValueError('verified context source identity or bounds differ')
        contents[key] = content
        anchors.extend(source_quote_anchors(ref, text, facts).values())
    return contents, anchors


def _resolve(raw, row, sources, anchors):
    resolved = copy.deepcopy(row)
    span = None
    if 'quote_ref' in row:
        anchor = resolve_quote_ref(raw, row['quote_ref'], anchors)
        if ('quote' in row and row['quote'] != anchor['quote'] or
                'source_ref' in row and row['source_ref'] != anchor.get('source_ref')):
            raise ValueError('quote or source_ref differs from selected quote_ref')
        resolved.update({key: anchor[key] for key in ('quote', 'source_ref') if key in anchor})
        span = anchor['span']
    if 'quote' not in resolved:
        raise ValueError('literal quote or quote_ref is required')
    quote = resolved['quote']
    if 'source_ref' in resolved:
        ref = SourceRef.from_dict(resolved['source_ref']).to_dict()
        content = sources[_canonical(ref)]
        text, facts, origin = content['payload']['text'], content['facts'], 'context_source'
    else:
        ref, text, facts, origin = None, raw, None, 'source_quote'
    if quote is None:
        if ref is not None:
            raise ValueError('context source requires a literal quote')
        return None, None, 'proposed_default', None, None
    if type(quote) is not str or not quote or quote not in text:
        raise ValueError('quote is not an exact nonempty source substring')
    start = text.find(quote)
    return quote, span if span is not None else [start, start + len(quote)], origin, ref, facts


def validate_parameter_witnesses(raw, draft, sources=None):
    """Return literal checks, readable blockers and explicit coverage limitations.

    ``sources`` has the same canonical-SourceRef-bytes mapping as proposal._rows;
    it must come from the parent's validated ContextStore, never model input.
    No file, database, executor, model or mutable process state is accessed.
    """
    result = {'status': 'ready', 'witnesses': [], 'missing': [], 'errors': [],
              'coverage_limits': list(_COVERAGE_LIMITS)}

    def block(code, field, detail):
        result['missing'].append(field)
        result['errors'].append(code + ': ' + field + ': ' + detail)

    try:
        if (type(raw) is not str or not raw or len(raw.encode()) > 65536
                or type(draft) is not dict or type(draft.get('execution')) is not dict
                or type(draft.get('requirements')) is not list
                or len(draft['requirements']) > 32 or len(_canonical(draft)) > 65536):
            raise ValueError('source or draft exceeds parameter witness shape/bounds')
        contents, anchors = _sources(sources)
        specifications = _PARAMETERS.get(draft.get('capability'), {})
        for index, row in enumerate(draft['requirements']):
            if (type(row) is not dict or type(row.get('fields')) is not list
                    or any(type(field) is not str for field in row['fields'])):
                raise ValueError('requirement fields must be an array of strings')
            fields = [field for field in row['fields'] if field.startswith('/execution/')
                      and field.removeprefix('/execution/') in specifications]
            if not fields:
                continue
            try:
                quote, span, origin, ref, facts = _resolve(raw, row, contents, anchors)
            except (ValueError, TypeError, KeyError, RecursionError) as error:
                for field in fields:
                    block('parameter_witness_source_invalid', field, str(error))
                continue
            for field in fields:
                name = field.removeprefix('/execution/')
                specification = specifications[name]
                witness = {'requirement_id': 'r' + str(index + 1).zfill(4),
                           'field': field, 'origin': origin, 'quote': quote, 'span': span}
                if ref is not None:
                    witness['source_ref'] = ref
                result['witnesses'].append(witness)
                try:
                    value = _canonical_value(draft['execution'][name], specification)
                except (ValueError, KeyError) as error:
                    witness['status'] = 'invalid_value'
                    block('parameter_value_invalid', field, str(error))
                    continue
                witness['canonical_value'] = value
                if quote is None:
                    witness['status'] = 'requires_human_review'
                    continue
                if facts is not None:
                    try:
                        literal_values = [_canonical_value(facts[name], specification)]
                    except (ValueError, KeyError) as error:
                        witness['status'] = 'invalid_fact'
                        block('parameter_witness_fact_invalid', field, str(error))
                        continue
                else:
                    literal_values = _literal_values(quote, specification)
                witness['literal_values'] = literal_values
                if not literal_values:
                    witness['status'] = 'unverified_prose'
                    if specification in ('codec', 'character'):
                        block('parameter_literal_missing', field, 'no supported literal value in selected quote')
                elif len(literal_values) != 1:
                    witness['status'] = 'ambiguous'
                    block('parameter_literal_ambiguous', field,
                          'selected quote has multiple literal values ' + repr(literal_values))
                elif literal_values[0] != value:
                    witness['status'] = 'conflict'
                    block('parameter_value_conflict', field,
                          'candidate ' + repr(value) + ' differs from source ' + repr(literal_values[0]))
                else:
                    witness['status'] = 'verified_literal'
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError) as error:
        block('parameter_witness_invalid', 'parameter_witnesses', str(error))
    result['missing'] = sorted(set(result['missing']))
    if result['errors']:
        result['status'] = 'needs_context'
    return result
