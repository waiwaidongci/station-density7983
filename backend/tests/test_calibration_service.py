import pytest
from fastapi import HTTPException

from src.schemas.calibration import CalibrationRunInput,InstrumentInput,PairObservation
from src.services import calibration as svc


def pair(left,right,session='1',site='S',**kw):
    base=dict(left_key=left,right_key=right,site=site,session_key=session,
              temperature=1.0,humidity=2.0,particles=3.0,weight=1.0)
    base.update(kw);return PairObservation(**base)


def setup_network(env,stub_queue):
    from src.models.user import User
    with env['Session']() as db:
        u=db.get(User,env['user_id'])
        for key,anchor in [('A',True),('B',False),('C',False),('D',False),('E',False),
                           ('X',True),('Y',False)]:
            svc.save_instrument(db,u,InstrumentInput(key=key,name=key,is_anchor=anchor))
        svc.add_pairs(db,u,[pair('A','B',temperature=1.0,humidity=1.0,particles=1.0),
                            pair('B','C',temperature=2.0,humidity=2.0,particles=2.0)])
        svc.add_pairs(db,u,[pair('X','Y',temperature=1.0,humidity=1.0,particles=1.0)])
        svc.add_pairs(db,u,[pair('D','E',temperature=3.0,humidity=3.0,particles=3.0)])


def test_duplicate_observation_for_same_site_session_is_rejected(env,stub_queue):
    from src.models.user import User
    setup_network(env,stub_queue)
    with env['Session']() as db:
        u=db.get(User,env['user_id'])
        with pytest.raises(HTTPException) as exc:
            svc.add_pairs(db,u,[pair('A','B',temperature=9.0)])
        assert exc.value.status_code==409


def test_reversed_orientation_is_canonicalised(env,stub_queue):
    from src.models.user import User
    setup_network(env,stub_queue)
    with env['Session']() as db:
        u=db.get(User,env['user_id'])
        svc.add_pairs(db,u,[pair('C','B',session='2',temperature=2.0)])
        p=db.query(svc.CalibrationPair).filter_by(session_key='2').one()
        assert p.left_key=='B' and p.right_key=='C'
        assert p.temperature==-2.0


def test_solve_publish_and_component_reuse(env,stub_queue):
    from src.models.user import User
    setup_network(env,stub_queue)
    with env['Session']() as db:
        u=db.get(User,env['user_id'])
        first=svc.create_run(db,u,CalibrationRunInput(name='first'))
        v1=svc.execute(db,first['id'])
        assert v1.summary['components']['solved']==2
        assert v1.summary['isolated_instruments']==2
        p=db.query(svc.CalibrationPair).filter_by(left_key='X').one()
        svc.correct_pair(db,u,p.id,pair('X','Y',temperature=1.5))
        second=svc.create_run(db,u,CalibrationRunInput(name='second'))
        v2=svc.execute(db,second['id'])
        assert v2.summary['components']['cached']==1
        assert v2.summary['components']['solved']==1
        o1={r['instrument_key']:r for r in svc.version_offsets(db,v1.id)['items']}
        o2={r['instrument_key']:r for r in svc.version_offsets(db,v2.id)['items']}
        for key in ('A','B','C'):
            assert abs(o1[key]['temperature']-o2[key]['temperature'])<1e-12
        assert abs(o2['Y']['temperature']-(-1.5))<1e-9
        result=svc.publish(db,u,second['id'])
        assert result['published']
        with pytest.raises(HTTPException):
            svc.publish(db,u,first['id'])


def test_stale_queued_run_is_refused(env,stub_queue):
    from src.models.user import User
    setup_network(env,stub_queue)
    with env['Session']() as db:
        u=db.get(User,env['user_id'])
        run=svc.create_run(db,u,CalibrationRunInput(name='queued'))
        p=db.query(svc.CalibrationPair).filter_by(left_key='A').one()
        svc.correct_pair(db,u,p.id,pair('A','B',temperature=1.2,humidity=1.0,particles=1.0))
        assert svc.execute(db,run['id']) is None
        assert svc.get_run(db,run['id']).state=='stale'
        with pytest.raises(HTTPException):
            svc.publish(db,u,run['id'])


def test_late_old_solution_cannot_replace_newer_publication(env,stub_queue):
    from src.models.user import User
    setup_network(env,stub_queue)
    with env['Session']() as db:
        u=db.get(User,env['user_id'])
        old=svc.create_run(db,u,CalibrationRunInput(name='old'))
        newer=svc.create_run(db,u,CalibrationRunInput(name='newer'))
        v_new=svc.execute(db,newer['id'])
        svc.publish(db,u,newer['id'])
        assert svc.execute(db,old['id']) is None
        assert svc.get_run(db,old['id']).state=='stale'
        assert svc.graph_meta(db)['published_version_id']==v_new.id


def test_retry_of_completed_run_does_not_duplicate_offsets(env,stub_queue):
    from src.models.user import User
    setup_network(env,stub_queue)
    with env['Session']() as db:
        u=db.get(User,env['user_id'])
        run=svc.create_run(db,u,CalibrationRunInput(name='retry'))
        first=svc.execute(db,run['id'])
        offsets=len(svc.version_offsets(db,first.id)['items'])
        again=svc.execute(db,run['id'])
        assert again.id==first.id
        assert len(svc.version_offsets(db,first.id)['items'])==offsets


def test_interrupted_run_is_requeued(env,stub_queue):
    import datetime
    from src.models.user import User
    setup_network(env,stub_queue)
    with env['Session']() as db:
        u=db.get(User,env['user_id'])
        run=svc.create_run(db,u,CalibrationRunInput(name='interrupted'))
        svc.get_run(db,run['id']).state='running'
        svc.get_run(db,run['id']).heartbeat_at=datetime.datetime.now(datetime.timezone.utc).isoformat()
        db.commit()
        assert svc.recover_interrupted(db)==[]
        svc.get_run(db,run['id']).heartbeat_at='2020-01-01T00:00:00+00:00'
        db.commit()
        assert svc.recover_interrupted(db)==[run['id']]
        assert svc.get_run(db,run['id']).state=='queued'


def test_non_converged_run_fails_and_retry_resumes(env,stub_queue):
    from src.models.user import User
    setup_network(env,stub_queue)
    # A long chain needs many CG iterations, so ten cannot reach 1e-30.
    with env['Session']() as db:
        u=db.get(User,env['user_id'])
        for i in range(40):
            svc.save_instrument(db,u,InstrumentInput(key='N%02d'%i,name='',is_anchor=False))
        obs=[pair('A','N00',session='z',temperature=1.0,humidity=2.0,particles=3.0)]
        obs+=[pair('N%02d'%i,'N%02d'%(i+1),session='s%02d'%i,
                   temperature=1+i*.013,humidity=2+i*.007,particles=3-i*.004) for i in range(39)]
        svc.add_pairs(db,u,obs)
        run=svc.create_run(db,u,CalibrationRunInput(name='nonconv',max_iter=10,tolerance=1e-30))
        assert svc.execute(db,run['id']) is None
        failed=svc.get_run(db,run['id'])
        assert failed.state=='failed' and '未收敛' in failed.error
        with pytest.raises(HTTPException):
            svc.publish(db,u,run['id'])
        retried=svc.retry_run(db,u,run['id'])
        assert retried['state']=='queued'
        assert svc.execute(db,run['id']) is not None or svc.get_run(db,run['id']).state=='failed'


def test_publish_verifies_offsets_against_evidence(env,stub_queue):
    from src.models.user import User
    setup_network(env,stub_queue)
    with env['Session']() as db:
        u=db.get(User,env['user_id'])
        run=svc.create_run(db,u,CalibrationRunInput(name='verify'))
        version=svc.execute(db,run['id'])
        off=db.query(svc.CalibrationOffset).filter_by(version_id=version.id).first()
        off.temperature+=0.5;db.commit()
        with pytest.raises(HTTPException):
            svc.publish(db,u,run['id'])


def test_reading_correction_uses_frozen_version(env,stub_queue):
    from src.models.user import User
    from src.models.record import Record
    from src.models.source import Source
    setup_network(env,stub_queue)
    with env['Session']() as db:
        u=db.get(User,env['user_id'])
        run=svc.create_run(db,u,CalibrationRunInput(name='readings'))
        version=svc.execute(db,run['id'])
        s=Source(key='src',name='src',filename='s.csv',description='')
        db.add(s);db.flush()
        db.add(Record(source_id=s.id,reference='r1',instrument_key='B',
                      values={'temperature':21.0,'humidity':50.0,'particles':30.0}))
        db.add(Record(source_id=s.id,reference='r2',instrument_key='ZZ',
                      values={'temperature':21.0,'humidity':50.0,'particles':30.0}))
        db.commit()
        rows=svc.version_readings(db,version.id,s.id)['items']
        corrected={r['instrument_key']:r for r in rows}
        offsets={o['instrument_key']:o for o in svc.version_offsets(db,version.id)['items']}
        assert abs(corrected['B']['corrected']['temperature']-(21.0-offsets['B']['temperature']))<1e-12
        assert corrected['ZZ']['corrected'] is None


def test_duplicate_within_one_batch_is_rejected(env,stub_queue):
    from src.models.user import User
    setup_network(env,stub_queue)
    with env['Session']() as db:
        u=db.get(User,env['user_id'])
        with pytest.raises(HTTPException) as exc:
            svc.add_pairs(db,u,[pair('C','D',session='dup',temperature=1.0),
                                pair('D','C',session='dup',temperature=-1.0)])
        assert exc.value.status_code==409
