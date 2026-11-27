"""Real filesystem races at the authority boundaries (no sandbox impersonation)."""
import hashlib
import os

import pytest

from rpe_harness.path_binding import BoundExecution, read_source


@pytest.fixture
def bound_paths(tmp_path):
    workspace=tmp_path/'workspace'
    source_dir=workspace/'input'
    output_dir=workspace/'output'
    source_dir.mkdir(parents=True)
    output_dir.mkdir()
    source=source_dir/'source.csv'
    source.write_bytes(b'id,value\na,first\na,later\n')
    staging=tmp_path/'trusted-state'/'staging'
    staging.mkdir(parents=True)
    outside=tmp_path/'outside'
    outside.mkdir()
    payload={'execution':{'input_path':str(source),'output_path':str(output_dir/'result.csv')}}
    binding=BoundExecution.capture(payload,workspace,staging,hashlib.sha256(source.read_bytes()).hexdigest())
    try:
        yield binding,workspace,outside
    finally:
        binding.close()


def test_open_source_authority_survives_parent_and_leaf_replacement(bound_paths):
    binding,workspace,outside=bound_paths
    original=binding.source.read_bytes()
    binding.source.parent.rename(workspace/'original-input')
    (outside/'source.csv').write_bytes(b'secret outside data')
    binding.source.parent.symlink_to(outside,target_is_directory=True)
    with binding.activate():
        assert read_source(binding.source)==original
    with pytest.raises(ValueError,match='bound path'):
        binding.check_paths()


def test_final_link_destination_remains_bound_after_last_path_check(bound_paths,monkeypatch):
    binding,workspace,outside=bound_paths
    stage=binding.staging_root/'verified.csv'
    stage.write_bytes(b'id,value\na,first\n')
    original=os.link
    def racing_link(*args,**kwargs):
        binding.output.parent.rename(workspace/'original-output')
        binding.output.parent.symlink_to(outside,target_is_directory=True)
        return original(*args,**kwargs)
    monkeypatch.setattr(os,'link',racing_link)
    with pytest.raises(ValueError,match='bound path'):
        binding.publish(stage)
    assert not list(outside.iterdir())
    assert (workspace/'original-output'/'result.csv').read_bytes()==stage.read_bytes()


@pytest.mark.parametrize('kind',['file','symlink'])
def test_bound_publication_never_overwrites_a_racing_target(bound_paths,monkeypatch,kind):
    binding,workspace,outside=bound_paths
    stage=binding.staging_root/'verified.csv'
    stage.write_bytes(b'id,value\na,first\n')
    victim=outside/'existing'
    victim.write_bytes(b'preserve me')
    original=os.link
    def racing_link(*args,**kwargs):
        if kind=='symlink':
            binding.output.symlink_to(victim)
        else:
            binding.output.write_bytes(b'existing output')
        return original(*args,**kwargs)
    monkeypatch.setattr(os,'link',racing_link)
    with pytest.raises(FileExistsError):
        binding.publish(stage)
    assert victim.read_bytes()==b'preserve me'
    assert binding.output.is_symlink() if kind=='symlink' else binding.output.read_bytes()==b'existing output'


@pytest.mark.parametrize('kind',['source_symlink','parent_symlink','source_fifo'])
def test_capture_rejects_symlinks_and_special_sources_before_reading(tmp_path,kind):
    workspace=tmp_path/'workspace'
    workspace.mkdir()
    parent=workspace/'input'
    parent.mkdir()
    outside=tmp_path/'outside'
    outside.mkdir()
    source=parent/'source.csv'
    if kind=='source_symlink':
        (outside/'source.csv').write_bytes(b'outside')
        source.symlink_to(outside/'source.csv')
    elif kind=='parent_symlink':
        parent.rmdir()
        parent.symlink_to(outside,target_is_directory=True)
        (outside/'source.csv').write_bytes(b'outside')
    else:
        os.mkfifo(source)
    payload={'execution':{'input_path':str(source),'output_path':str(workspace/'result.csv')}}
    with pytest.raises((OSError,ValueError)):
        BoundExecution.capture(payload,workspace,tmp_path,'0'*64)
    assert not (workspace/'result.csv').exists()
