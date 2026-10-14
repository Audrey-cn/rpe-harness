import csv


def prepare(plan, staging_path):
    with open(plan['input_path'], encoding=plan['encoding'], newline='') as source:
        rows = list(csv.reader(source, delimiter=plan['delimiter'], strict=True))
    columns = [rows[0].index(key) for key in plan['key_columns']]
    seen, kept = set(), []
    for row in rows[1:]:
        key = tuple(row[index] for index in columns)
        if key not in seen:
            seen.add(key)
            kept.append(row)
    with open(staging_path, 'w', encoding=plan['encoding'], newline='') as result:
        csv.writer(result, delimiter=plan['delimiter']).writerows([rows[0], *kept])
