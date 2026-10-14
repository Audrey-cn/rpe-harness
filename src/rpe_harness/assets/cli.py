"""Unified local capability assets; execution remains in the evolution commands."""
from pathlib import Path


def register_assets(commands):
    assets = commands.add_parser('assets', help='inert capability packages and local Registry')
    actions = assets.add_subparsers(dest='assets_command', required=True)
    for name in ('inspect', 'import'):
        command = actions.add_parser(name)
        command.add_argument('--package', type=Path, required=True)
        if name == 'inspect':
            command.add_argument('--keyring', type=Path)
        else:
            command.add_argument('--owned-root', type=Path, required=True)
            command.add_argument('--db')
    for name in ('list', 'show'):
        command = actions.add_parser(name)
        if name == 'show':
            command.add_argument('asset_id')
        command.add_argument('--db')
        command.add_argument('--keyring', type=Path)
    propose = actions.add_parser('propose')
    propose.add_argument('asset_id')
    propose.add_argument('--keyring', type=Path, required=True)
    propose.add_argument('--evidence-run-id', action='append', required=True)
    propose.add_argument('--owned-root', type=Path)
    propose.add_argument('--db')
    for name in ('registry-list', 'registry-import'):
        command = actions.add_parser(name)
        command.add_argument('--registry', type=Path, required=True)
        if name == 'registry-import':
            command.add_argument('--name', required=True)
            command.add_argument('--db')
            command.add_argument('--owned-root', type=Path, required=True)
    export = actions.add_parser('export')
    export.add_argument('version_id')
    export.add_argument('--db')
    export.add_argument('--destination', type=Path, required=True)
    export.add_argument('--identity', type=Path)
    build = actions.add_parser('registry-build', help='build a local signed release bundle')
    build.add_argument('--db', type=Path, required=True)
    build.add_argument('--version-id', action='append', required=True)
    build.add_argument('--destination', type=Path, required=True)
    build.add_argument('--creator-identity', type=Path, required=True)
    build.add_argument('--registry-identity', type=Path, required=True)
    build.add_argument('--keyring', type=Path, required=True)
    validate = actions.add_parser('registry-validate', help='validate without activation or publishing')
    validate.add_argument('--bundle', type=Path, required=True)
    validate.add_argument('--keyring', type=Path, required=True)
    validate.add_argument('--mode', choices=('integrity', 'acceptance'), default='integrity')
    validate.add_argument('--owned-root', type=Path)
    validate.add_argument('--report', type=Path)


def handle_assets(args):
    from ..runtime import default_db_path
    from .lifecycle import _verify_source, asset_status, export_version, propose_asset
    from .manifest import inspect_package
    from .registry import import_registry_asset, read_registry
    from .store import AssetStore
    from .identity import load_local_json
    action = args.assets_command
    if action == 'inspect':
        result = inspect_package(args.package)
        if args.keyring:
            try:
                result['source_verification'] = _verify_source(result, load_local_json(args.keyring))
            except (ValueError, OSError, TypeError) as error:
                result['source_verification'] = {'passed': False, 'reason': str(error)}
        return result
    if action == 'registry-list':
        return {'assets': read_registry(args.registry), 'purpose': 'discovery_only'}
    if action == 'registry-validate':
        from .catalog import validate_release_bundle
        result = validate_release_bundle(args.bundle, keyring=args.keyring,
            mode=args.mode, owned_root=args.owned_root)
        if args.report:
            from .prepublish import write_validation_report
            write_validation_report(args.report, result, args.bundle)
        return result
    if action == 'registry-build':
        from .catalog import build_release_bundle
        return build_release_bundle(args.version_id, args.db, args.destination,
            creator_identity=args.creator_identity, registry_identity=args.registry_identity,
            keyring=args.keyring)
    db_path = Path(args.db) if args.db else default_db_path()
    if action == 'import':
        return AssetStore(db_path, args.owned_root).import_package(args.package)
    if action == 'registry-import':
        return import_registry_asset(AssetStore(db_path, args.owned_root), args.registry, args.name)
    if action == 'show':
        return asset_status(args.asset_id, db_path, keyring=args.keyring)
    if action == 'list':
        store = AssetStore(db_path)
        return {'assets': [asset_status(item['asset_id'], db_path, keyring=args.keyring)
                           for item in store.list_assets()]}
    if action == 'propose':
        return propose_asset(args.asset_id, db_path, keyring=args.keyring,
            evidence_run_ids=args.evidence_run_id, owned_root=args.owned_root)
    return export_version(args.version_id, db_path, args.destination, identity=args.identity)
