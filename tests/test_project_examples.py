"""Approval provenance and atomic refresh of examples from real project files."""
import hashlib
import json
from pathlib import Path
import shutil

import pytest

from auto_hand_router.cli import DEFAULT_BOARD
from auto_hand_router.model import Board
from tools import sync_project_examples as sync


@pytest.fixture
def approved(tmp_path,monkeypatch):
    folder=tmp_path/"project"
    folder.mkdir()
    project=folder/"example.kicad_pcb"
    for ext in (".kicad_pcb",".kicad_pro",".kicad_sch"):
        shutil.copyfile(DEFAULT_BOARD.with_suffix(ext),project.with_suffix(ext))
    project.with_suffix('.kicad_dru').write_text('(version 1)')
    board=Board.load(project)
    minx,miny,maxx,maxy=board.boundary.bounds
    (folder/'fp-lib-table').write_text('local configuration, excluded from examples')
    hashes={project.with_suffix(ext).name:hashlib.sha256(project.with_suffix(ext).read_bytes()).hexdigest()
            for ext in sync.NATIVE_EXTENSIONS}
    report=dict(status='PASS',copper=dict(passed=True,arcs=sum(p.mid is not None for p in board.original_primitives),
                     segments=sum(p.mid is None for p in board.original_primitives),vias=len(board.original_vias),teardrops=0,
                     track_length_mm=0,unconnected_components=0,minimum_extra_clearance_mm=.1),
                kicad_drc=dict(passed=True),project_audit=dict(passed=True),
                schematic_parity=dict(new_findings=0,inherited_findings=0),
                erc=dict(electrical_errors=0,findings={}),tangency=dict(free_kinks=0),
                track_geometry_unchanged=True,teardrops=dict(size=.875,generated=0),
                via_policy=dict(mode='courtyards'),board_mm=dict(width=maxx-minx,length=maxy-miny),
                kicad_version='10.0.8',router_commit='29054a3',artifact_sha256=hashes,source_sha256=hashes)
    audit=folder/'verification/auto-hand-router-audit.json'
    audit.parent.mkdir()
    audit.write_text(json.dumps(report))
    def render(board,panels,destination,labels=False):
        Path(destination).write_text('<svg xmlns="http://www.w3.org/2000/svg" width="4" height="4"/>')
    monkeypatch.setattr(sync,'svg',render)
    output=tmp_path/'examples/example'
    output.mkdir(parents=True)
    (output/'previous').write_text('previous valid example')
    return project,audit,report,output,tmp_path/'images'


def test_approved_example_copies_exact_native_bytes_and_records_source_commit(approved):
    project,audit,_,output,images=approved
    summary=sync.sync_project(project,output,images,'a05fa00')
    for ext in sync.NATIVE_EXTENSIONS:
        assert (output/project.with_suffix(ext).name).read_bytes()==project.with_suffix(ext).read_bytes()
    assert not (output/'fp-lib-table').exists()
    assert (output/'.auto-hand-router').is_file()
    assert summary['source_revision']=='a05fa00'
    assert summary['source_audit_sha256']==hashlib.sha256(audit.read_bytes()).hexdigest()
    assert (images/'example.png').exists()
    assert not output.with_name('example.previous').exists()


def test_stale_approval_does_not_replace_the_previous_example(approved):
    project,_,_,output,images=approved
    project.with_suffix('.kicad_pro').write_bytes(project.with_suffix('.kicad_pro').read_bytes()+b'\n')
    with pytest.raises(ValueError,match='differs from its approved audit'):
        sync.sync_project(project,output,images,'a05fa00')
    assert (output/'previous').read_text()=='previous valid example'
    assert not images.exists()


@pytest.mark.parametrize('section,field,value',[
    ('copper','passed',False),('kicad_drc','passed',False),('project_audit','passed',False),
    ('schematic_parity','new_findings',1),('erc','electrical_errors',1),('tangency','free_kinks',1)])
def test_failed_project_checks_block_publication(approved,section,field,value):
    project,audit,report,output,images=approved
    report[section][field]=value
    audit.write_text(json.dumps(report))
    with pytest.raises(ValueError,match='passing current'):
        sync.sync_project(project,output,images,'a05fa00')
    assert (output/'previous').exists() and not images.exists()


def test_source_edit_during_render_keeps_previous_example_and_previews(approved,monkeypatch):
    project,_,_,output,images=approved
    def edit_source(board,panels,destination,labels=False):
        Path(destination).write_text('<svg xmlns="http://www.w3.org/2000/svg" width="4" height="4"/>')
        project.write_bytes(project.read_bytes()+b'\n')
    monkeypatch.setattr(sync,'svg',edit_source)
    with pytest.raises(ValueError,match='changed during example rendering'):
        sync.sync_project(project,output,images,'a05fa00')
    assert (output/'previous').read_text()=='previous valid example'
    assert not images.exists()
    assert not list(output.parent.glob('.auto-hand-router-stage-*'))


@pytest.mark.parametrize('section,field', [('board_mm','length'),('copper','arcs')])
def test_inconsistent_dimensions_or_geometry_counts_are_rejected(approved,section,field):
    project,audit,report,output,images=approved
    report[section][field]+=1
    audit.write_text(json.dumps(report))
    with pytest.raises(ValueError,match='differ from the approved audit'):
        sync.sync_project(project,output,images,'a05fa00')
    assert (output/'previous').exists() and not images.exists()
