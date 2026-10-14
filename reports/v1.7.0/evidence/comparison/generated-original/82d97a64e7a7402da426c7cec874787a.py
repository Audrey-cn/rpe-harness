import csv
import hashlib
import os
import sys
from typing import Any, Dict, List

MAX_INPUT_BYTES = 16777216  # 16 MiB
MAX_OUTPUT_BYTES = 16777216  # 16 MiB

VALID_CAPABILITY = "tabular.deduplicate"
VALID_INPUT_FORMAT = "csv"
VALID_KEEP = "first"


def prepare(plan: Dict[str, Any], staging_path: str) -> None:
    # Validate plan fields
    if not isinstance(plan, dict):
        raise ValueError("plan must be a dict")
    
    required_fields = ["capability", "input_format", "input_path", "key_columns", 
                      "encoding", "delimiter", "keep"]
    for field in required_fields:
        if field not in plan:
            raise ValueError(f"Missing required plan field: {field}")
    
    # Validate capability
    if plan["capability"] != VALID_CAPABILITY:
        raise ValueError(f"Unsupported capability: {plan['capability']}")
    
    # Validate input format
    if plan["input_format"] != VALID_INPUT_FORMAT:
        raise ValueError(f"Unsupported input format: {plan['input_format']}")
    
    # Validate keep policy
    if plan["keep"] != VALID_KEEP:
        raise ValueError(f"Unsupported keep policy: {plan['keep']}")
    
    # Validate key_columns
    key_columns = plan["key_columns"]
    if not isinstance(key_columns, list) or len(key_columns) == 0:
        raise ValueError("key_columns must be a non-empty list")
    
    for col in key_columns:
        if not isinstance(col, str) or len(col) == 0:
            raise ValueError("Each key column must be a non-empty string")
    
    # Validate encoding
    encoding = plan["encoding"]
    if not isinstance(encoding, str) or len(encoding) == 0:
        raise ValueError("encoding must be a non-empty string")
    
    # Validate delimiter
    delimiter = plan["delimiter"]
    if not isinstance(delimiter, str) or len(delimiter) != 1:
        raise ValueError("delimiter must be a single character")
    
    # Validate input_path
    input_path = plan["input_path"]
    if not isinstance(input_path, str) or len(input_path) == 0:
        raise ValueError("input_path must be a non-empty string")
    
    # Check if staging file already exists
    if os.path.exists(staging_path):
        raise ValueError(f"Staging file already exists: {staging_path}")
    
    # Check input file size
    if not os.path.isfile(input_path):
        raise ValueError(f"Input path is not a file: {input_path}")
    
    input_size = os.path.getsize(input_path)
    if input_size > MAX_INPUT_BYTES:
        raise ValueError(f"Input file exceeds maximum size: {input_size} > {MAX_INPUT_BYTES}")
    
    # Read and process CSV
    try:
        with open(input_path, 'r', encoding=encoding, newline='') as infile:
            reader = csv.reader(infile, delimiter=delimiter)
            
            # Read header
            try:
                header = next(reader)
            except StopIteration:
                raise ValueError("Input CSV is empty")
            
            # Check for duplicate headers
            if len(header) != len(set(header)):
                raise ValueError("Duplicate headers found")
            
            # Check if all key columns exist in header
            key_indices = []
            for key_col in key_columns:
                if key_col not in header:
                    raise ValueError(f"Key column not found in header: {key_col}")
                key_indices.append(header.index(key_col))
            
            # Process rows
            seen_keys = set()
            output_rows = []
            output_rows.append(header)  # Add header
            
            row_count = 0
            for row in reader:
                row_count += 1
                
                # Check row length matches header
                if len(row) != len(header):
                    raise ValueError(f"Row {row_count} has {len(row)} fields, expected {len(header)}")
                
                # Extract key values
                key_values = tuple(row[idx] for idx in key_indices)
                
                # Check for empty key values
                for i, key_val in enumerate(key_values):
                    if len(key_val) == 0:
                        raise ValueError(f"Empty key value in column '{key_columns[i]}' at row {row_count}")
                
                # Check for duplicate keys
                if key_values in seen_keys:
                    continue  # Skip duplicate (keep first)
                
                seen_keys.add(key_values)
                output_rows.append(row)
    
    except UnicodeDecodeError as e:
        raise ValueError(f"Failed to decode input with encoding {encoding}: {e}")
    except csv.Error as e:
        raise ValueError(f"CSV parsing error: {e}")
    
    # Write output
    try:
        with open(staging_path, 'x', encoding=encoding, newline='') as outfile:
            writer = csv.writer(outfile, delimiter=delimiter, lineterminator='\n')
            for row in output_rows:
                writer.writerow(row)
            
            # Check output size
            outfile.flush()
            output_size = outfile.tell()
            if output_size > MAX_OUTPUT_BYTES:
                raise ValueError(f"Output exceeds maximum size: {output_size} > {MAX_OUTPUT_BYTES}")
    
    except FileExistsError:
        raise ValueError(f"Staging file already exists: {staging_path}")
    except OSError as e:
        # Clean up partial file
        if os.path.exists(staging_path):
            try:
                os.remove(staging_path)
            except OSError:
                pass
        raise ValueError(f"Failed to write staging file: {e}")
