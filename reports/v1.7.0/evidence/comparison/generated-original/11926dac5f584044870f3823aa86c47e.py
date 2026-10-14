import csv
import os
from typing import Any, Dict

MAX_INPUT_BYTES = 16777216
MAX_OUTPUT_BYTES = 16777216

VALID_CAPABILITY = "tabular.deduplicate"
VALID_INPUT_FORMAT = "csv"
VALID_KEEP = "first"


def prepare(plan: Dict[str, Any], staging_path: str) -> None:
    if not isinstance(plan, dict):
        raise ValueError("plan must be a dict")

    required_fields = ["capability", "input_format", "input_path", "key_columns",
                       "encoding", "delimiter", "keep"]
    for field in required_fields:
        if field not in plan:
            raise ValueError(f"Missing required plan field: {field}")

    if plan["capability"] != VALID_CAPABILITY:
        raise ValueError(f"Unsupported capability: {plan['capability']}")
    if plan["input_format"] != VALID_INPUT_FORMAT:
        raise ValueError(f"Unsupported input format: {plan['input_format']}")
    if plan["keep"] != VALID_KEEP:
        raise ValueError(f"Unsupported keep policy: {plan['keep']}")

    key_columns = plan["key_columns"]
    if not isinstance(key_columns, list) or len(key_columns) == 0:
        raise ValueError("key_columns must be a non-empty list")
    for col in key_columns:
        if not isinstance(col, str) or len(col) == 0:
            raise ValueError("Each key column must be a non-empty string")

    encoding = plan["encoding"]
    if not isinstance(encoding, str) or len(encoding) == 0:
        raise ValueError("encoding must be a non-empty string")

    delimiter = plan["delimiter"]
    if not isinstance(delimiter, str) or len(delimiter) != 1:
        raise ValueError("delimiter must be a single character")

    input_path = plan["input_path"]
    if not isinstance(input_path, str) or len(input_path) == 0:
        raise ValueError("input_path must be a non-empty string")

    if not isinstance(staging_path, str) or len(staging_path) == 0:
        raise ValueError("staging_path must be a non-empty string")

    if os.path.exists(staging_path):
        raise ValueError(f"Staging file already exists: {staging_path}")

    if not os.path.isfile(input_path):
        raise ValueError(f"Input path is not a file: {input_path}")

    input_size = os.path.getsize(input_path)
    if input_size > MAX_INPUT_BYTES:
        raise ValueError(f"Input file exceeds maximum size: {input_size} > {MAX_INPUT_BYTES}")

    try:
        with open(input_path, 'r', encoding=encoding, newline='') as infile:
            reader = csv.reader(infile, delimiter=delimiter, strict=True)

            try:
                header = next(reader)
            except StopIteration:
                raise ValueError("Input CSV is empty")

            if len(header) != len(set(header)):
                raise ValueError("Duplicate headers found")

            key_indices = []
            for key_col in key_columns:
                if key_col not in header:
                    raise ValueError(f"Key column not found in header: {key_col}")
                key_indices.append(header.index(key_col))

            seen_keys = set()
            output_rows = [header]

            row_count = 0
            for row in reader:
                row_count += 1

                if len(row) != len(header):
                    raise ValueError(f"Row {row_count} has {len(row)} fields, expected {len(header)}")

                key_values = tuple(row[idx] for idx in key_indices)

                for i, key_val in enumerate(key_values):
                    if len(key_val) == 0:
                        raise ValueError(f"Empty key value in column '{key_columns[i]}' at row {row_count}")

                if key_values in seen_keys:
                    continue

                seen_keys.add(key_values)
                output_rows.append(row)

    except UnicodeDecodeError as e:
        raise ValueError(f"Failed to decode input with encoding {encoding}: {e}")
    except csv.Error as e:
        raise ValueError(f"CSV parsing error: {e}")

    try:
        with open(staging_path, 'x', encoding=encoding, newline='') as outfile:
            writer = csv.writer(outfile, delimiter=delimiter, lineterminator='\n')
            total = 0
            for row in output_rows:
                # Re-serialize the row exactly as csv.writer would, to enforce
                # the output size limit before anything is committed.
                line = csv.writer(_SizeProbe(), delimiter=delimiter,
                                  lineterminator='\n').writerow(row)
                total += _SizeProbe.last_size
                if total > MAX_OUTPUT_BYTES:
                    raise ValueError(f"Output exceeds maximum size: {total} > {MAX_OUTPUT_BYTES}")
                writer.writerow(row)

            outfile.flush()
            output_size = outfile.tell()
            if output_size > MAX_OUTPUT_BYTES:
                raise ValueError(f"Output exceeds maximum size: {output_size} > {MAX_OUTPUT_BYTES}")
    except FileExistsError:
        raise ValueError(f"Staging file already exists: {staging_path}")
    except OSError as e:
        if os.path.exists(staging_path):
            try:
                os.remove(staging_path)
            except OSError:
                pass
        raise ValueError(f"Failed to write staging file: {e}")
    except ValueError:
        if os.path.exists(staging_path):
            try:
                os.remove(staging_path)
            except OSError:
                pass
        raise


class _SizeProbe:
    """File-like sink that counts bytes csv.writer would emit."""
    last_size = 0

    def write(self, data):
        _SizeProbe.last_size += len(data.encode('utf-8'))
        return len(data)

    def __init__(self):
        _SizeProbe.last_size = 0
