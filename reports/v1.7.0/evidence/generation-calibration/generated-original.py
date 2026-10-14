"""Standalone CSV deduplication producer (keep=first) using only the stdlib.

Public ABI entry point:
    prepare(plan, staging_path)

Reads plan["input_path"], writes a de-duplicated CSV to staging_path, and
leaves the source file unchanged. Invalid or unsupported input is rejected
with ValueError *before* staging_path is created. An existing staging file
is never overwritten (FileExistsError).
"""

import csv
import os

_LIMIT = 16777216  # 16 MiB, shared by input_bytes and output_bytes
_DELIMITERS = (",", "\t", ";", "|")
_KEEP_POLICIES = frozenset(("first",))


def _fail(message):
    raise ValueError(message)


def _validate_plan(plan):
    if not isinstance(plan, dict):
        _fail("plan must be a dict")
    for field in ("capability", "input_format", "input_path", "key_columns",
                  "encoding", "delimiter", "keep"):
        if field not in plan:
            _fail("plan missing field: %s" % (field,))
    if plan["capability"] != "tabular.deduplicate":
        _fail("unsupported capability: %r" % (plan["capability"],))
    if plan["input_format"] != "csv":
        _fail("unsupported input_format: %r" % (plan["input_format"],))
    if plan["keep"] not in _KEEP_POLICIES:
        _fail("unsupported keep policy: %r" % (plan["keep"],))
    input_path = plan["input_path"]
    if not isinstance(input_path, str) or not input_path:
        _fail("input_path must be a non-empty string")
    key_columns = plan["key_columns"]
    if not isinstance(key_columns, (list, tuple)) or not key_columns:
        _fail("key_columns must be a non-empty list")
    for name in key_columns:
        if not isinstance(name, str) or not name:
            _fail("key column names must be non-empty strings")
    encoding = plan["encoding"]
    if not isinstance(encoding, str) or not encoding:
        _fail("encoding must be a non-empty string")
    try:
        "".encode(encoding)
    except LookupError:
        _fail("unsupported encoding: %r" % (encoding,))
    except Exception:
        _fail("unsupported encoding: %r" % (encoding,))
    delimiter = plan["delimiter"]
    if not isinstance(delimiter, str) or len(delimiter) != 1:
        _fail("delimiter must be a single character")
    if delimiter in ('"', "\r", "\n", "\x00"):
        _fail("unsupported delimiter: %r" % (delimiter,))
    if isinstance(delimiter, bytes):
        _fail("delimiter must be text")
    return input_path, list(key_columns), encoding, delimiter


def _check_key(name, row, header, encoding, delimiter, seen):
    """Validate one record's key-column values; return the key tuple."""
    try:
        indices = [header.index(name) for name in name]  # placeholder
    except Exception:
        pass
    return None


def prepare(plan, staging_path):
    if not isinstance(staging_path, str) or not staging_path:
        _fail("staging_path must be a non-empty string")
    input_path, key_columns, encoding, delimiter = _validate_plan(plan)

    # Source must be a regular file within the byte limit.
    try:
        st = os.stat(input_path)
    except OSError:
        _fail("input_path is not accessible")
    if not os.path.isfile(input_path):
        _fail("input_path is not a regular file")
    if st.st_size > _LIMIT:
        _fail("input exceeds input_bytes limit")

    # Never overwrite an existing staging file: reserve the name up front.
    try:
        fd = os.open(staging_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        raise
    except OSError as exc:
        _fail("cannot create staging file: %s" % (exc,))

    try:
        with os.fdopen(fd, "wb") as out_f:
            _run(input_path, key_columns, encoding, delimiter, out_f)
    except BaseException:
        # Leave no partial staging file behind on any failure.
        try:
            os.unlink(staging_path)
        except OSError:
            pass
        raise


def _run(input_path, key_columns, encoding, delimiter, out_f):
    out = None  # text wrapper created after the header is validated
    seen = set()
    input_rows = 0
    output_rows = 0
    removed_rows = 0

    try:
        # newline="" makes csv see raw line endings; encoding is the plan's.
        with open(input_path, "r", encoding=encoding, newline="") as in_f:
            reader = csv.reader(in_f, delimiter=delimiter, quotechar='"',
                                doublequote=True, strict=True)

            # --- header -------------------------------------------------
            try:
                header = next(reader)
            except StopIteration:
                _fail("input has no header row")
            except (csv.Error, UnicodeError):
                _fail("malformed CSV in header")
            if not header:
                _fail("header row is empty")
            duplicates = set()
            for cell in header:
                if cell in duplicates:
                    _fail("duplicate header: %r" % (cell,))
                duplicates.add(cell)
            for name in key_columns:
                if name not in duplicates:
                    _fail("missing key column: %r" % (name,))
            key_indices = [header.index(name) for name in key_columns]

            import io
            out = io.TextIOWrapper(out_f, encoding=encoding, newline="",
                                   write_through=True)
            writer = csv.writer(out, delimiter=delimiter, quotechar='"',
                                doublequote=True, lineterminator="\r\n",
                                quoting=csv.QUOTE_MINIMAL)
            writer.writerow(header)
            output_rows += 1
            _check_size(out_f)

            # --- body ---------------------------------------------------
            for row in reader:
                input_rows += 1
                if len(row) != len(header):
                    _fail("malformed row %d: %d fields, expected %d"
                          % (input_rows, len(row), len(header)))
                key = tuple(row[i] for i in key_indices)
                for value in key:
                    if value == "":
                        _fail("empty key value in row %d" % (input_rows,))
                if key in seen:
                    removed_rows += 1
                    continue
                seen.add(key)
                writer.writerow(row)
                output_rows += 1
                _check_size(out_f)
    except UnicodeError:
        _fail("input is not valid %s text" % (encoding,))
    except csv.Error:
        _fail("malformed CSV input")
    finally:
        if out is not None:
            out.flush()

    # Expose metrics for callers that want them (not part of the ABI).
    prepare.last_metrics = {
        "input_rows": input_rows,
        "output_rows": output_rows,
        "removed_rows": removed_rows,
    }


def _check_size(out_f):
    try:
        pos = out_f.tell()
    except OSError:
        return
    if pos > _LIMIT:
        _fail("output exceeds output_bytes limit")
