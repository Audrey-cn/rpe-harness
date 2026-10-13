import csv


def prepare(plan: dict, staging_path: str) -> None:
    key_columns = plan['key_columns']
    if not isinstance(key_columns, (list, tuple)) or not key_columns:
        raise ValueError('key_columns must be a nonempty list or tuple')
    if any(not isinstance(column, str) or column == '' for column in key_columns):
        raise ValueError('Key column names must be nonempty strings')
    if len(set(key_columns)) != len(key_columns):
        raise ValueError('Duplicate key columns')

    encoding = plan['encoding']
    delimiter = plan['delimiter']
    rows = []
    seen = set()

    with open(plan['input_path'], 'r', encoding=encoding, newline='') as source:
        reader = csv.reader(source, delimiter=delimiter, strict=True)
        header = next(reader, None)
        if not header or any(column == '' for column in header):
            raise ValueError('Missing or empty headers')
        if len(set(header)) != len(header):
            raise ValueError('Duplicate headers')
        if any(column not in header for column in key_columns):
            raise ValueError('Missing key columns')
        key_indices = [header.index(column) for column in key_columns]

        for row in reader:
            if len(row) != len(header):
                raise ValueError('Malformed row length')
            key = tuple(row[index] for index in key_indices)
            if any(cell == '' for cell in key):
                raise ValueError('Empty key cell')
            if key not in seen:
                seen.add(key)
                rows.append(row)

    with open(staging_path, 'x', encoding=encoding, newline='') as staging:
        writer = csv.writer(staging, delimiter=delimiter)
        writer.writerow(header)
        writer.writerows(rows)
