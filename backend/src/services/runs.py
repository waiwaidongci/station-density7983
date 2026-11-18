from sqlalchemy import select
from fastapi import HTTPException
from redis import Redis
from ..config.settings import REDIS_URL
from ..models.run import Run
from ..utils.serialization import row,now
from .sources import source_or_404,list_records
from .calibration import state_row
from .domain_validation import validate_parameters
from .audit import log
queue=Redis.from_url(REDIS_URL)
def get_run(db,id):
    r=db.get(Run,id)
    if not r:raise HTTPException(404,'未找到分析任务')
    return r
def create_run(db,user,data):
    s=source_or_404(db,data.source_id);records=list_records(db,s.id)
    if len(records)<3:raise HTTPException(422,'至少录入3条样本后才能执行')
    try: params=validate_parameters(data.parameters)
    except ValueError as e:raise HTTPException(422,str(e))
    st=state_row(db)
    # Freeze the calibration version id (not its values) at submission time.
    # The worker later applies exactly this version, even if a newer one is published.
    # We also copy the checksum so the frozen basis is verifiable later without
    # depending on rows that a later publication could change.
    calibration_version=st.published_version_id
    calibration_checksum=''
    if calibration_version:
        from ..models.calibration import CalibrationVersion
        cv=db.get(CalibrationVersion,calibration_version)
        calibration_checksum=cv.checksum if cv else ''
    frozen={'source':row(s),'records':records,'calibration_version_id':calibration_version,
            'calibration_checksum':calibration_checksum}
    r=Run(source_id=s.id,name=data.name,parameters=params,snapshot=frozen,created_at=now(),state='queued')
    db.add(r);db.flush();log(db,user,'run.queued',r.id,data.name+
        ('' if not calibration_version else ' · calibration v'+str(calibration_version)));db.commit()
    try:queue.rpush('analysis:queue',r.id)
    except Exception:
        r.state='failed';r.error='队列暂不可用，请重新创建任务';db.commit();raise HTTPException(503,r.error)
    return row(r)
def list_runs(db):
    rows=[]
    for r in db.scalars(select(Run).order_by(Run.id.desc())):
        d={k:v for k,v in row(r).items() if k not in ('snapshot','result')}
        d['calibration_version_id']=(r.snapshot or {}).get('calibration_version_id')
        rows.append(d)
    return rows


def list_run_records(db,id):
    r=get_run(db,id)
    return {'calibration_version_id':(r.snapshot or {}).get('calibration_version_id'),
            'records':[{k:v for k,v in rec.items() if k in ('reference','instrument_key','corrected')}
                       for rec in r.snapshot['records']]}
