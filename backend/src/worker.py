import json,logging,time
from redis import Redis
from sqlalchemy import update
from .config.database import Session
from .config.settings import REDIS_URL,ARTIFACT_DIR
from .models.run import Run
from .models.calibration import CalibrationVersion,CalibrationOffset
from .models.calibration import CalibrationRun
from .models import source,record,user,note,audit,calibration
from .services.algorithm import calculate
from .services.calibration import apply_version,execute as execute_calibration,recover_interrupted
from .services.audit import log
from .services.export import csv_response
queue=Redis.from_url(REDIS_URL)
ARTIFACT_DIR.mkdir(parents=True,exist_ok=True)


def claim(db,model,id):
    """Atomically claim a queued job.

    The conditional UPDATE makes a duplicate or retried queue message a no-op:
    only the worker that flips queued -> running owns the job, so a retry can
    never execute or accumulate work twice.
    """
    result=db.execute(update(model).where(model.id==id,model.state=='queued')
                      .values(state='running'))
    db.commit()
    return result.rowcount==1


def execute(id):
    """Run one research job, applying the calibration version frozen at submit."""
    with Session() as db:
        if not claim(db,Run,id):return
        r=db.get(Run,id)
        try:
            records=r.snapshot['records']
            version_id=r.snapshot.get('calibration_version_id')
            calibration_info=None
            if version_id:
                offsets=db.scalars(__import__('sqlalchemy').select(CalibrationOffset)
                    .where(CalibrationOffset.version_id==version_id)).all()
                applied=apply_version(db,records,version_id)
                corrected=sum(1 for x in applied if x['corrected'])
                records=[{k:v for k,v in x.items() if k not in ('corrected',)} for x in applied]
                calibration_info={'version_id':version_id,'corrected':corrected,
                                  'uncalibrated':len(applied)-corrected,
                                  'offsets_available':len(offsets)}
            result=calculate(records,r.parameters)
            if calibration_info:
                result['calibration']={**calibration_info,
                    'checksum':db.get(CalibrationVersion,version_id).checksum}
            json.dumps(result,allow_nan=False)
            r.result=result;r.state='completed'
            response=csv_response(result,'run.csv')
            (ARTIFACT_DIR/f'run-{r.id}.csv').write_bytes(response.body)
            log(db,'worker','run.completed',r.id,
                '' if not calibration_info else 'calibration v'+str(version_id))
        except Exception as e:
            logging.exception('analysis failed');r.state='failed';r.error=str(e)[:2000]
            log(db,'worker','run.failed',r.id,r.error)
        db.commit()


def execute_calibration_job(id):
    with Session() as db:
        if not claim(db,CalibrationRun,id):return
        try:
            execute_calibration(db,id)
        except Exception:
            logging.exception('calibration failed')
            run=db.get(CalibrationRun,id)
            if run and run.state!='completed':
                run.state='failed';run.error=(run.error or '校准失败')[:2000]
                log(db,'worker','calibration.run.failed',id,run.error)
                db.commit()


def _recover():
    try:
        with Session() as db:recover_interrupted(db)
    except Exception:logging.exception('calibration recovery failed')


def main():
    _recover()
    tick=0
    while True:
        (ARTIFACT_DIR/'heartbeat').write_text(str(time.time()))
        tick+=1
        if tick%30==0:_recover()
        value=queue.blpop(['calibration:queue','analysis:queue'],timeout=2)
        if not value:continue
        name,raw=value[0].decode(),value[1]
        if name=='calibration:queue':execute_calibration_job(int(raw))
        else:execute(int(raw))
if __name__=='__main__':main()
