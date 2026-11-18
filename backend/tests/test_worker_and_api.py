import json

import pytest
from fastapi.testclient import TestClient


def make_app(env,monkeypatch):
    monkeypatch.setenv('SEED_DEMO','false')
    from src.main import app
    return app


def login(client,email='admin@test.demo',password='x'):
    response=client.post('/auth/login',json={'email':email,'password':password})
    assert response.status_code==200,response.text
    return {'Authorization':'Bearer '+response.json()['token']}


def test_worker_claims_each_job_once(env,stub_queue,monkeypatch):
    monkeypatch.setenv('SEED_DEMO','false')
    from src.config.database import Session
    from src.models.run import Run
    from src.models.source import Source
    from src.models.record import Record
    from src.worker import execute
    with Session() as db:
        s=Source(key='s',name='src',filename='f.csv',description='');db.add(s);db.flush()
        records=[]
        for i in range(6):
            rec=Record(source_id=s.id,reference='r%d'%i,values={'temperature':20+i*.1,'humidity':50+i*.2,'particles':30+i*.3})
            db.add(rec);db.flush()
            records.append({'id':rec.id,'source_id':s.id,'reference':rec.reference,
                            'instrument_key':None,'values':rec.values})
        r=Run(source_id=s.id,name='job',state='queued',parameters={'eps':.5,'min_samples':2},
              snapshot={'source':{'filename':'f.csv'},'records':records,'calibration_version_id':None},
              created_at='2026-01-01T00:00:00+00:00')
        db.add(r);db.commit();run_id=r.id
    execute(run_id)
    with Session() as db:
        first=db.get(Run,run_id)
        assert first.state=='completed'
        result=first.result
    execute(run_id)  # duplicate queue message must not recompute or alter the result
    with Session() as db:
        second=db.get(Run,run_id)
        assert second.state=='completed' and second.result==result


def test_api_requires_auth_and_validates(env,monkeypatch,stub_queue):
    app=make_app(env,monkeypatch)
    client=TestClient(app)
    assert client.get('/calibration/instruments').status_code==401
    headers=login(client)
    assert client.get('/calibration/instruments',headers=headers).status_code==200
    bad=client.post('/calibration/pairs',headers=headers,json={'observations':[
        {'left_key':'A','right_key':'A','temperature':1,'humidity':1,'particles':1,'weight':1}]})
    assert bad.status_code==422


def test_api_add_pairs_and_solve(env,monkeypatch,stub_queue):
    app=make_app(env,monkeypatch)
    client=TestClient(app)
    headers=login(client)
    for key,anchor in [('A',True),('B',False)]:
        response=client.post('/calibration/instruments',headers=headers,
                             json={'key':key,'name':key,'is_anchor':anchor})
        assert response.status_code==200,response.text
    response=client.post('/calibration/pairs',headers=headers,json={'observations':[
        {'left_key':'A','right_key':'B','site':'S','session_key':'1',
         'temperature':1.0,'humidity':2.0,'particles':3.0,'weight':1.0}]})
    assert response.status_code==200 and response.json()['added']==1
    duplicate=client.post('/calibration/pairs',headers=headers,json={'observations':[
        {'left_key':'B','right_key':'A','site':'S','session_key':'1',
         'temperature':-1.0,'humidity':-2.0,'particles':-3.0,'weight':1.0}]})
    assert duplicate.status_code==409
    created=client.post('/calibration/runs',headers=headers,json={'name':'api solve'})
    assert created.status_code==200,created.text
    run_id=created.json()['id']
    from src.services import calibration as svc
    with env['Session']() as db:svc.execute(db,run_id)
    published=client.post('/calibration/runs/%d/publish'%run_id,headers=headers)
    assert published.status_code==200,published.text
    versions=client.get('/calibration/versions',headers=headers).json()['items']
    assert versions[0]['is_current']
    offsets=client.get('/calibration/versions/%d/offsets'%versions[0]['id'],headers=headers).json()
    assert offsets['total']==2
    residuals=client.get('/calibration/versions/%d/residuals'%versions[0]['id'],headers=headers).json()
    assert residuals['total']==1
    meta=client.get('/calibration/instruments',headers=headers).json()['graph']
    assert meta['published_version_id']==versions[0]['id']


def test_viewer_reads_but_cannot_calibrate(env,monkeypatch,stub_queue):
    from src.models.user import User
    app=make_app(env,monkeypatch)
    with env['Session']() as db:
        db.add(User(email='viewer@test.demo',password_hash=__import__('src.utils.security',fromlist=['hash_password']).hash_password('x'),role='viewer'))
        db.commit()
    client=TestClient(app)
    headers=login(client,'viewer@test.demo','x')
    assert client.get('/calibration/instruments',headers=headers).status_code==200
    assert client.get('/calibration/versions',headers=headers).status_code==200
    assert client.post('/calibration/pairs',headers=headers,json={'observations':[]}).status_code==403
    assert client.post('/calibration/runs',headers=headers,json={'name':'x'}).status_code==403


def test_new_research_freezes_published_version_and_existing_is_immutable(env,monkeypatch,stub_queue):
    app=make_app(env,monkeypatch)
    client=TestClient(app)
    headers=login(client)
    for key,anchor in [('A',True),('B',False)]:
        client.post('/calibration/instruments',headers=headers,json={'key':key,'name':key,'is_anchor':anchor})
    client.post('/calibration/pairs',headers=headers,json={'observations':[
        {'left_key':'A','right_key':'B','site':'S','session_key':'1',
         'temperature':1.0,'humidity':2.0,'particles':3.0,'weight':1.0}]})
    run_id=client.post('/calibration/runs',headers=headers,json={'name':'freeze'}).json()['id']
    from src.services import calibration as svc
    with env['Session']() as db:svc.execute(db,run_id)
    client.post('/calibration/runs/%d/publish'%run_id,headers=headers)
    source=client.post('/sources',headers=headers,json={'name':'来源','filename':'s.csv','description':''}).json()
    client.post('/sources/%d/records'%source['id'],headers=headers,json={'records':[
        {'reference':'r%d'%i,'instrument_key':'B','values':{'temperature':21+i*.1,'humidity':50,'particles':30}}
        for i in range(6)]})
    research=client.post('/runs',headers=headers,json={'source_id':source['id'],'name':'研究',
                                                       'parameters':{'eps':.5,'min_samples':2}}).json()
    detail=client.get('/runs/%d'%research['id'],headers=headers).json()
    frozen=detail['snapshot']['calibration_version_id']
    assert frozen is not None
    # Corrections and a new publication must not touch the existing run snapshot.
    pair=client.get('/calibration/pairs',headers=headers).json()['items'][0]
    client.put('/calibration/pairs/%d'%pair['id'],headers=headers,json={
        'left_key':'A','right_key':'B','site':'S','session_key':'1',
        'temperature':1.4,'humidity':2.0,'particles':3.0,'weight':1.0})
    new_run=client.post('/calibration/runs',headers=headers,json={'name':'freeze again'}).json()['id']
    with env['Session']() as db:svc.execute(db,new_run)
    client.post('/calibration/runs/%d/publish'%new_run,headers=headers)
    again=client.get('/runs/%d'%research['id'],headers=headers).json()
    assert again['snapshot']['calibration_version_id']==frozen
    assert again['snapshot']==detail['snapshot']
