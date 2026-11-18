from fastapi import APIRouter,Depends
from ..config.database import db_session
from ..schemas.calibration import (AnchorInput,CalibrationRunInput,InstrumentInput,
                                   PairBatch,PairCorrection)
from ..services.auth import admin,current_user,writer
from ..services import calibration as svc
router=APIRouter(prefix='/calibration')


@router.get('/instruments')
def instruments(db=Depends(db_session),u=Depends(current_user)):
    return {'items':svc.list_instruments(db),'graph':svc.graph_meta(db)}


@router.post('/instruments')
def save_instrument(data:InstrumentInput,db=Depends(db_session),u=Depends(writer)):
    return svc.save_instrument(db,u,data)


@router.post('/instruments/{key}/anchor')
def anchor(key:str,data:AnchorInput,db=Depends(db_session),u=Depends(admin)):
    return svc.set_anchor(db,u,key,data.is_anchor)


@router.get('/pairs')
def pairs(page:int=0,size:int=50,key:str|None=None,db=Depends(db_session),u=Depends(current_user)):
    return svc.list_pairs(db,page,size,key)


@router.post('/pairs')
def add_pairs(data:PairBatch,db=Depends(db_session),u=Depends(writer)):
    return svc.add_pairs(db,u,data.observations)


@router.put('/pairs/{pair_id}')
def correct_pair(pair_id:int,data:PairCorrection,db=Depends(db_session),u=Depends(writer)):
    return svc.correct_pair(db,u,pair_id,data)


@router.delete('/pairs/{pair_id}')
def delete_pair(pair_id:int,db=Depends(db_session),u=Depends(writer)):
    return svc.delete_pair(db,u,pair_id)


@router.get('/runs')
def runs(db=Depends(db_session),u=Depends(current_user)):
    return {'items':svc.list_runs(db)}


@router.post('/runs')
def create_run(data:CalibrationRunInput,db=Depends(db_session),u=Depends(writer)):
    return svc.create_run(db,u,data)


@router.get('/runs/{id}')
def run_detail(id:int,db=Depends(db_session),u=Depends(current_user)):
    return svc.row_public(svc.get_run(db,id))


@router.post('/runs/{id}/retry')
def retry(id:int,db=Depends(db_session),u=Depends(writer)):
    return svc.retry_run(db,u,id)


@router.post('/runs/{id}/publish')
def publish(id:int,db=Depends(db_session),u=Depends(admin)):
    return svc.publish(db,u,id)


@router.get('/versions')
def versions(db=Depends(db_session),u=Depends(current_user)):
    return {'items':svc.list_versions(db)}


@router.get('/versions/{id}')
def version(id:int,db=Depends(db_session),u=Depends(current_user)):
    return svc.version_summary(db,id)


@router.get('/versions/{id}/offsets')
def offsets(id:int,page:int=0,size:int=100,q:str|None=None,db=Depends(db_session),u=Depends(current_user)):
    return svc.version_offsets(db,id,page,size,q)


@router.get('/versions/{id}/readings')
def readings(id:int,source_id:int,page:int=0,size:int=50,db=Depends(db_session),
             u=Depends(current_user)):
    return svc.version_readings(db,id,source_id,page,size)


@router.get('/versions/{id}/residuals')
def residuals(id:int,page:int=0,size:int=50,min_abs:float|None=None,
              feature:str='temperature',db=Depends(db_session),u=Depends(current_user)):
    return svc.version_residuals(db,id,page,size,min_abs,feature)
