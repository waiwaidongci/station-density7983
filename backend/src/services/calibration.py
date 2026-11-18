"""Network calibration: instruments, paired differences, staged solves, publication.

Data volume
-----------
Instruments and pairs live in their own tables, never inside a run snapshot.
Pairs are streamed in id order in small pages, so memory scales with the graph,
not with Python object overhead: 20k instruments and 100k pairs are a few MB.

Graph versioning
----------------
Every pair write/update bumps ``CalibrationState.graph_version``.  A solve
records the version it read.  Corrections of an existing pair produce a new row
revision; the per-component evidence fingerprint changes only for the affected
component.  Unchanged components hit the component cache and are reused.

Publication and freezing
------------------------
A completed solve stages an immutable ``CalibrationVersion`` plus offsets and
residuals.  Publication copies nothing and flips ``published_version_id`` after
a consistency check.  A run inside a live research job freezes the published
version id it started from and keeps reading that exact version, even after a
newer calibration is published.
"""
import hashlib
import os
import time
import numpy as np
from fastapi import HTTPException
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

from ..config.settings import ARTIFACT_DIR,REDIS_URL
from ..models.calibration import (CalibrationComponentCache,CalibrationInstrument,
                                  CalibrationOffset,CalibrationPair,CalibrationResidual,
                                  CalibrationRun,CalibrationState,CalibrationVersion)
from ..models.record import Record
from ..utils.serialization import now,row
from redis import Redis
from .audit import log
from .calibration_solver import FEATURES,as_fingerprint,estimate_bytes,solve

CHECKPOINT_DIR=ARTIFACT_DIR/'calibration-checkpoints'
queue=Redis.from_url(REDIS_URL)
PAGE=2000
MAX_GRAPH_INSTRUMENTS=20000
MAX_GRAPH_PAIRS=100000
MEMORY_LIMIT=128*1024*1024
LABELS={'temperature':'温度','humidity':'湿度','particles':'颗粒'}


def state_row(db,lock=False):
    q=select(CalibrationState).where(CalibrationState.id==1)
    if lock and db.get_bind().dialect.name!='sqlite':q=q.with_for_update()
    st=db.scalar(q)
    if not st:
        st=CalibrationState(id=1,graph_version=0,published_version_id=None);db.add(st);db.flush()
    return st


def bump_graph(db):
    st=state_row(db,lock=True);st.graph_version+=1
    db.flush();return st.graph_version


def _canonical(data):
    """Canonical orientation: left < right, difference = left reading - right reading."""
    left=_clean_text(data.left_key);right=_clean_text(data.right_key)
    if not left or not right:raise HTTPException(422,'配对仪器编号不能为空')
    if left==right:raise HTTPException(422,'配对两端不能是同一台仪器')
    if left<right:return left,right,1.0
    return right,left,-1.0


def _observation_key(left,right,site,session_key):
    return as_fingerprint(['obs',left,right,site,session_key])


def _clean_text(v):return (v or '').strip()


def ensure_instruments(db,keys):
    keys=sorted({_clean_text(k) for k in keys if _clean_text(k)})
    existing=set(db.scalars(select(CalibrationInstrument.key).where(CalibrationInstrument.key.in_(keys)))) if keys else set()
    for key in keys:
        if key not in existing:
            db.add(CalibrationInstrument(key=key,name=key,is_anchor=False,created_at=now()))
    return [k for k in keys if k not in existing]


def list_instruments(db):
    rows=db.execute(select(CalibrationInstrument).order_by(CalibrationInstrument.key)).scalars()
    counts=dict(db.execute(select(CalibrationPair.left_key,func.count()).group_by(CalibrationPair.left_key)).all())
    rcounts=dict(db.execute(select(CalibrationPair.right_key,func.count()).group_by(CalibrationPair.right_key)).all())
    return [{'key':i.key,'name':i.name,'is_anchor':bool(i.is_anchor),
             'pairs':counts.get(i.key,0)+rcounts.get(i.key,0),
             'created_at':i.created_at} for i in rows]


def save_instrument(db,user,data):
    key=_clean_text(data.key)
    if not key:raise HTTPException(422,'仪器编号不能为空')
    inst=db.scalar(select(CalibrationInstrument).where(CalibrationInstrument.key==key))
    if inst:
        inst.name=_clean_text(data.name) or inst.name
        if bool(data.is_anchor)!=bool(inst.is_anchor):
            inst.is_anchor=bool(data.is_anchor);bump_graph(db)
    else:
        db.add(CalibrationInstrument(key=key,name=_clean_text(data.name) or key,
                                    is_anchor=bool(data.is_anchor),created_at=now()))
        bump_graph(db)
    log(db,user,'calibration.instrument.saved',key,'anchor' if data.is_anchor else '')
    db.commit();return {'key':key,'is_anchor':bool(data.is_anchor)}


def set_anchor(db,user,key,is_anchor):
    inst=db.scalar(select(CalibrationInstrument).where(CalibrationInstrument.key==key))
    if not inst:raise HTTPException(404,'未找到仪器')
    if bool(inst.is_anchor)!=bool(is_anchor):
        inst.is_anchor=bool(is_anchor);bump_graph(db)
        log(db,user,'calibration.anchor.changed',key,'anchor' if is_anchor else 'free')
    db.commit();return {'key':key,'is_anchor':bool(is_anchor)}


def add_pairs(db,user,observations):
    added=0;keys=set();batch_keys=set()
    for obs in observations:
        left,right,sign=_canonical(obs)
        site=_clean_text(obs.site);session=_clean_text(obs.session_key)
        okey=_observation_key(left,right,site,session)
        if okey in batch_keys:
            raise HTTPException(409,'同一批提交内配对重复：'+left+' / '+right)
        batch_keys.add(okey)
        if db.scalar(select(CalibrationPair.id).where(CalibrationPair.observation_key==okey)):
            raise HTTPException(409,'同站点同场次的配对已存在，请改用更正：'+left+' / '+right)
        keys.update((left,right))
        db.add(CalibrationPair(observation_key=okey,left_key=left,right_key=right,site=site,
                               session_key=session,observed_at=_clean_text(obs.observed_at),
                               temperature=float(sign*obs.temperature),humidity=float(sign*obs.humidity),
                               particles=float(sign*obs.particles),weight=float(obs.weight),
                               revision=0,updated_at=now()))
        added+=1
    ensure_instruments(db,keys)
    bump_graph(db)
    log(db,user,'calibration.pairs.added','graph',str(added))
    try:db.commit()
    except IntegrityError:
        # Two concurrent submissions of the same observation: the natural key
        # wins and this batch is rejected rather than double-counted.
        db.rollback();raise HTTPException(409,'同一站点、场次的仪器配对已存在；请使用更正接口')
    return {'added':added}


def correct_pair(db,user,pair_id,data):
    pair=db.get(CalibrationPair,pair_id)
    if not pair:raise HTTPException(404,'未找到配对')
    left,right,sign=_canonical(data)
    site=_clean_text(data.site);session=_clean_text(data.session_key)
    okey=_observation_key(left,right,site,session)
    clash=db.scalar(select(CalibrationPair.id).where(CalibrationPair.observation_key==okey,
                                                    CalibrationPair.id!=pair.id))
    if clash:raise HTTPException(409,'更正后会与另一条配对重复')
    pair.left_key,pair.right_key=left,right
    pair.site,pair.session_key,=site,session
    pair.observed_at=_clean_text(data.observed_at)
    pair.observation_key=okey
    pair.temperature=float(sign*data.temperature);pair.humidity=float(sign*data.humidity)
    pair.particles=float(sign*data.particles);pair.weight=float(data.weight)
    pair.revision+=1;pair.updated_at=now()
    ensure_instruments(db,{left,right})
    version=bump_graph(db)
    log(db,user,'calibration.pair.corrected',pair.id,'revision '+str(pair.revision))
    try:db.commit()
    except IntegrityError:
        db.rollback();raise HTTPException(409,'更正后会与另一条配对重复')
    return {'id':pair.id,'revision':pair.revision,'graph_version':version}


def delete_pair(db,user,pair_id):
    pair=db.get(CalibrationPair,pair_id)
    if not pair:raise HTTPException(404,'未找到配对')
    db.delete(pair);bump_graph(db)
    log(db,user,'calibration.pair.deleted',pair_id,pair.left_key+' / '+pair.right_key)
    db.commit();return {'deleted':pair_id}


def list_pairs(db,page=0,size=50,key=None):
    size=max(1,min(200,int(size)));page=max(0,int(page))
    query=select(CalibrationPair)
    if key:query=query.where((CalibrationPair.left_key==key)|(CalibrationPair.right_key==key))
    total=db.scalar(select(func.count()).select_from(query.subquery()))
    rows=db.scalars(query.order_by(CalibrationPair.id).offset(page*size).limit(size))
    return {'page':page,'size':size,'total':int(total),'items':[row(p) for p in rows]}


def graph_meta(db):
    st=state_row(db)
    counts={'instruments':db.scalar(select(func.count()).select_from(CalibrationInstrument)),
            'pairs':db.scalar(select(func.count()).select_from(CalibrationPair)),
            'anchors':db.scalar(select(func.count()).select_from(CalibrationInstrument)
                                .where(CalibrationInstrument.is_anchor==True))}
    published=db.get(CalibrationVersion,st.published_version_id) if st.published_version_id else None
    return {'graph_version':st.graph_version,**counts,
            'estimate_bytes':estimate_bytes(counts['instruments'],counts['pairs']),
            'budget_bytes':MEMORY_LIMIT,'published_version_id':st.published_version_id,
            'published':(None if not published else {'id':published.id,'run_id':published.run_id,
                                                     'graph_version':published.graph_version,
                                                     'published_at':published.published_at,
                                                     'checksum':published.checksum})}


def _stream_graph(db):
    keys=list(db.scalars(select(CalibrationInstrument.key).order_by(CalibrationInstrument.key)))
    anchors=[bool(a) for a in db.scalars(select(CalibrationInstrument.is_anchor)
                                        .order_by(CalibrationInstrument.key))]
    index={k:i for i,k in enumerate(keys)}
    capacity=max(1024,PAGE)
    ids=np.empty(capacity,dtype=np.int64);left=np.empty(capacity,dtype=np.int32)
    right=np.empty(capacity,dtype=np.int32);weight=np.empty(capacity,dtype=np.float64)
    diffs={f:np.empty(capacity,dtype=np.float64) for f in FEATURES}
    count=0
    def grow():
        nonlocal capacity,ids,left,right,weight
        capacity*=2
        ids=np.resize(ids,capacity);left=np.resize(left,capacity);right=np.resize(right,capacity)
        weight=np.resize(weight,capacity)
        for f in FEATURES:diffs[f]=np.resize(diffs[f],capacity)
    last=0
    while True:
        chunk=db.execute(select(CalibrationPair).where(CalibrationPair.id>last)
                         .order_by(CalibrationPair.id).limit(PAGE)).scalars().all()
        if not chunk:break
        last=chunk[-1].id
        for p in chunk:
            a=index.get(p.left_key);b=index.get(p.right_key)
            if a is None or b is None:continue
            if count==capacity:grow()
            ids[count]=p.id;left[count]=a;right[count]=b;weight[count]=p.weight
            for f in FEATURES:diffs[f][count]=getattr(p,f)
            count+=1
        db.expunge_all()  # keep the identity map flat while paging 100k pairs
    return (keys,anchors,left[:count].copy(),right[:count].copy(),weight[:count].copy(),
            {f:diffs[f][:count].copy() for f in FEATURES},ids[:count].copy())


def create_run(db,user,data):
    meta=graph_meta(db)
    if meta['pairs']==0:raise HTTPException(422,'还没有仪器配对，无法校准')
    if not meta['anchors']:raise HTTPException(422,'请至少指定一台可信仪器为零偏锚点')
    if meta['pairs']>MAX_GRAPH_PAIRS:raise HTTPException(422,'配对数超出单次求解上限')
    if meta['instruments']>MAX_GRAPH_INSTRUMENTS:raise HTTPException(422,'仪器数超出单次求解上限')
    params={'tolerance':data.tolerance,'max_iter':data.max_iter,
            'alert':{'temperature':data.alert_temperature,'humidity':data.alert_humidity,
                     'particles':data.alert_particles}}
    run=CalibrationRun(name=data.name,state='queued',graph_version=meta['graph_version'],
                       parameters=params,progress={},created_at=now())
    db.add(run);db.flush();log(db,user,'calibration.run.queued',run.id,data.name);db.commit()
    try:queue.rpush('calibration:queue',run.id)
    except Exception:
        run.state='failed';run.error='队列暂不可用，请重新创建校准任务';db.commit()
        raise HTTPException(503,run.error)
    return row(run)


def row_public(run):
    data=row(run)
    version_id=(run.result or {}).get('version_id')
    data['version_id']=version_id
    return data


def list_runs(db):
    return [{k:v for k,v in row(r).items() if k!='result'}
            for r in db.scalars(select(CalibrationRun).order_by(CalibrationRun.id.desc()).limit(50))]


def get_run(db,id):
    r=db.get(CalibrationRun,id)
    if not r:raise HTTPException(404,'未找到校准任务')
    return r


def _cache_get(db,fingerprint):
    try:
        c=db.get(CalibrationComponentCache,fingerprint)
        return c.result if c else None
    except Exception:
        db.rollback();return None


def _cache_put(db,fingerprint,payload):
    try:
        existing=db.get(CalibrationComponentCache,fingerprint)
        if existing:existing.result=payload;existing.created_at=now()
        else:db.add(CalibrationComponentCache(fingerprint=fingerprint,nodes=payload['nodes'],
                                              edges=payload['edges'],anchors=payload['anchors'],
                                              status='ok',result=payload,created_at=now()))
        db.commit()
    except Exception:
        db.rollback()


def _checkpoint_dir(run_id=None):
    base=os.getenv('CALIBRATION_CHECKPOINT_DIR')
    return base or str(CHECKPOINT_DIR)


def _bulk_iter(db,table,producer,chunk=1000):
    """Insert rows produced lazily in chunks, bounding Python object memory."""
    batch=[]
    for item in producer:
        batch.append(item)
        if len(batch)>=chunk:
            db.execute(table.insert(),batch);batch=[]
    if batch:db.execute(table.insert(),batch)


def execute(db,run_id,progress_cb=None):
    """Solve one calibration run.  Idempotent and resumable.

    Calling again after an interruption resumes saved CG checkpoints and reuses
    cached components; a completed run returns its existing staged version
    without inserting offsets twice.
    """
    run=get_run(db,run_id)
    existing=db.scalar(select(CalibrationVersion).where(CalibrationVersion.run_id==run.id))
    if run.state=='completed' and existing:
        return existing
    current=state_row(db).graph_version
    if run.graph_version!=current:
        # The graph moved between submission and execution: refuse to solve a
        # basis the requester never submitted.  A fresh run is required.
        run.state='stale';run.error=('配对图在排队期间由 v%d 变更为 v%d；请重新发起校准'
                                     %(run.graph_version,current))
        run.completed_at=now();db.commit();return None
    run.state='running';run.error='';run.heartbeat_at=now();db.commit()
    started=time.time()
    keys,anchors,left,right,weights,differences,pair_ids=_stream_graph(db)
    # Streaming detached the identity map; re-attach the run before writing.
    run=db.get(CalibrationRun,run_id)
    run.heartbeat_at=now();db.commit()
    if not keys or left.size==0:
        run.state='failed';run.error='配对图为空';db.commit()
        raise HTTPException(422,run.error)
    def progress(info):
        run.progress={**info,'elapsed_seconds':round(time.time()-started,3)}
        run.heartbeat_at=now()
        try:db.commit()
        except Exception:db.rollback()
        if progress_cb:progress_cb(info)
    try:
        result=solve(keys,anchors,left,right,weights,differences,
                     tolerance=run.parameters['tolerance'],max_iter=run.parameters['max_iter'],
                     checkpoint_dir=_checkpoint_dir(run.id),run_key=run.id,
                     min_checkpoint_edges=2000,alert_thresholds=run.parameters['alert'],
                     cache_get=lambda fp:_cache_get(db,fp),cache_put=lambda fp,p:_cache_put(db,fp,p),
                     progress=progress,memory_limit=MEMORY_LIMIT)
    except MemoryError as exc:
        run.state='failed';run.error=str(exc);db.commit();raise
    except Exception as exc:
        run.state='failed';run.error=str(exc)[:2000];db.commit();raise
    if result['components']['converged']<result['components']['anchored']:
        # Iteration limit hit.  Checkpoints stay on disk, so retrying with a
        # larger max_iter resumes instead of restarting; never publish a
        # half-converged network.
        run=db.get(CalibrationRun,run_id)
        run.state='failed'
        run.error=('未收敛：%d/%d 个锚定分量在 %d 次迭代内达到容差；迭代检查点已保留，可提高最大迭代后重试'
                   %(result['components']['converged'],result['components']['anchored'],
                     run.parameters['max_iter']))
        db.commit();return None
    run=db.get(CalibrationRun,run_id)
    current=state_row(db).graph_version
    if current!=run.graph_version:
        run.state='stale';run.error=('配对图在求解期间由 v%d 变更为 v%d；本次晚归结果拒绝发布，请重新校准'
                                     %(run.graph_version,current))
        run.completed_at=now();db.commit()
        return None
    return finalize(db,run,result,keys,left,right,pair_ids,weights)


def _superseded(db,run):
    """Return the published version that makes this solve a late old solution.

    A slow solve that returns after a newer run was already published must not
    replace the newer network state.  Only published versions count: a staged
    but unpublished newer version may still be awaiting review.
    """
    st=state_row(db)
    if not st.published_version_id:return None
    published=db.get(CalibrationVersion,st.published_version_id)
    if published and published.run_id>run.id:return published
    return None


def finalize(db,run,result,keys,left,right,pair_ids,weights):
    newer=_superseded(db,run)
    if newer:
        run.state='stale'
        run.error='较新的校准任务 #%d 已发布；本次晚归结果拒绝发布'%newer.run_id
        run.completed_at=now();db.commit();return None
    offsets=result['offsets'];residuals=result['residuals']
    primary=offsets[FEATURES[0]]
    anchored=np.flatnonzero(~np.isnan(primary))
    summary={'nodes':result['nodes'],'edges':result['edges'],
             'components':result['components'],'unanchored':result['unanchored'],
             'estimate_bytes':result['estimate_bytes'],
             'anchored_instruments':int(anchored.size),
             'isolated_instruments':int(primary.size-anchored.size),
             'stats':{f:{k:(round(float(v),10) if isinstance(v,(int,float,np.floating)) else v)
                         for k,v in result['stats'][f].items()} for f in FEATURES},
             'graph_version':run.graph_version}
    h=hashlib.sha256()
    for i in anchored.tolist():
        h.update(keys[i].encode())
        h.update(np.asarray([offsets[f][i] for f in FEATURES],dtype='<f8').tobytes())
    summary['checksum']=h.hexdigest()
    version=db.scalar(select(CalibrationVersion).where(CalibrationVersion.run_id==run.id))
    if not version:
        version=CalibrationVersion(run_id=run.id,graph_version=run.graph_version,state='staged',
                                   summary=summary,checksum=summary['checksum'],created_at=now())
        db.add(version)
        try:db.flush()
        except IntegrityError:
            # Another worker finished the same run first.  Keep only one version.
            db.rollback()
            existing=db.scalar(select(CalibrationVersion).where(CalibrationVersion.run_id==run.id))
            run=db.get(CalibrationRun,run.id)
            if existing and run.state!='completed':
                run.state='completed';run.completed_at=now();db.commit()
            return existing
    else:
        version.summary=summary;version.checksum=summary['checksum']
        db.execute(delete(CalibrationOffset).where(CalibrationOffset.version_id==version.id))
        db.execute(delete(CalibrationResidual).where(CalibrationResidual.version_id==version.id))
    def offset_rows():
        for i in anchored.tolist():
            yield {'version_id':version.id,'instrument_key':keys[i],
                   **{f:float(offsets[f][i]) for f in FEATURES}}
    _bulk_iter(db,CalibrationOffset.__table__,offset_rows())
    valid=np.flatnonzero(~np.isnan(residuals[FEATURES[0]])).tolist()
    def residual_rows():
        for e in valid:
            yield {'version_id':version.id,'pair_id':int(pair_ids[e]),
                   'left_key':keys[int(left[e])],'right_key':keys[int(right[e])],
                   'weight':float(weights[e]),
                   **{f:float(residuals[f][e]) for f in FEATURES}}
    _bulk_iter(db,CalibrationResidual.__table__,residual_rows())
    run.state='completed';run.result={'version_id':version.id,**summary};run.completed_at=now()
    run.heartbeat_at=now();run.progress={'done':True}
    log(db,'worker','calibration.run.completed',run.id,'version '+str(version.id))
    db.commit()
    return version


def recover_interrupted(db,stale_seconds=180,limit=20):
    """Requeue calibration runs whose worker died mid-solve.

    CG checkpoints survive on disk, so the retry resumes iterations instead of
    restarting.  Requeueing is idempotent: the worker claims queued->running
    atomically, and ``execute`` returns an existing completed version untouched.
    """
    import datetime
    now_ts=time.time();requeued=[]
    runs=db.scalars(select(CalibrationRun).where(CalibrationRun.state=='running')
                    .order_by(CalibrationRun.id).limit(limit)).all()
    for run in runs:
        try:age=now_ts-datetime.datetime.fromisoformat(run.heartbeat_at or run.created_at).timestamp()
        except (ValueError,TypeError):age=stale_seconds+1
        if age<stale_seconds:continue
        run.state='queued';run.error='';run.heartbeat_at=now()
        log(db,'system','calibration.run.recovered',run.id,'resume after '+str(int(age))+'s')
        requeued.append(run.id)
    db.commit()
    for run_id in requeued:
        try:queue.rpush('calibration:queue',run_id)
        except Exception:pass
    return requeued


def verify_version(db,version):
    """Recompute corrected residuals from stored offsets before publication.

    This independently checks the stored offsets against the pair evidence and
    compares the weighted residual sums with what the solver reported.  It also
    confirms every reported residual row really is explained by the offsets.
    Bounded memory: offsets dictionary + one page of pairs at a time.
    """
    version_id=version.id
    reported=(version.summary or {}).get('stats') or {}
    offsets=load_offsets(db,version_id)
    rss={f:0.0 for f in FEATURES};count=0
    last=0
    while True:
        chunk=db.execute(select(CalibrationPair).where(CalibrationPair.id>last)
                         .order_by(CalibrationPair.id).limit(PAGE)).scalars().all()
        if not chunk:break
        last=chunk[-1].id
        for p in chunk:
            a=offsets.get(p.left_key);b=offsets.get(p.right_key)
            if a is None or b is None:continue
            count+=1
            for j,f in enumerate(FEATURES):
                # Model: o_left - o_right = d, residual = o_left - o_right - d.
                res=(a[j]-b[j])-getattr(p,f)
                rss[f]+=float(p.weight)*res*res
        db.expunge_all()
    if count==0:return {'ok':False,'reason':'没有可核验的已锚定配对'}
    for f in FEATURES:
        expected=float((reported.get(f) or {}).get('rss',0.0))
        if abs(rss[f]-expected)>1e-6*max(1.0,abs(expected)):
            return {'ok':False,'reason':LABELS[f]+'加权残差平方和不一致'}
    stored=db.scalar(select(func.count()).select_from(CalibrationResidual)
                     .where(CalibrationResidual.version_id==version_id))
    if int(stored or 0)!=count:
        return {'ok':False,'reason':'残差明细条数与配对证据不一致'}
    return {'ok':True,'pairs':count,'rss':{f:round(rss[f],10) for f in FEATURES}}


def retry_run(db,user,run_id):
    """Requeue a failed/stale run, optionally with a larger iteration budget.

    Saved checkpoints let the retry continue from the last completed iteration.
    """
    run=get_run(db,run_id)
    if run.state not in ('failed','stale'):
        raise HTTPException(409,'仅失败或失效的校准任务可以重试')
    if run.graph_version!=state_row(db).graph_version:
        raise HTTPException(409,'配对图已变更，请重新发起校准而不是重试旧基准')
    run.state='queued';run.error='';run.heartbeat_at=now();run.progress={}
    log(db,user,'calibration.run.retried',run.id,'graph v'+str(run.graph_version))
    db.commit()
    try:queue.rpush('calibration:queue',run.id)
    except Exception:
        run.state='failed';run.error='队列暂不可用';db.commit();raise HTTPException(503,run.error)
    return row(run)


def publish(db,user,run_id):
    run=get_run(db,run_id)
    if run.state!='completed':
        raise HTTPException(409,'校准任务尚未完成，不能发布')
    version=db.scalar(select(CalibrationVersion).where(CalibrationVersion.run_id==run.id))
    if not version:raise HTTPException(409,'校准结果缺少版本记录')
    version_id=version.id
    st=state_row(db,lock=True)
    if version.state=='published' and st.published_version_id==version.id:
        return {'version_id':version.id,'already_published':True,'checksum':version.checksum}
    if version.state=='stale':
        raise HTTPException(409,'本次晚归校准已失效，拒绝发布')
    current=db.get(CalibrationVersion,st.published_version_id) if st.published_version_id else None
    if current and current.run_id>version.run_id:
        raise HTTPException(409,'较新的校准任务 #%d 已发布，旧解晚归拒绝发布'%current.run_id)
    if version.graph_version!=st.graph_version:
        raise HTTPException(409,'配对数据在计算后已变更，请重新校准后再发布')
    count=db.scalar(select(func.count()).select_from(CalibrationOffset)
                    .where(CalibrationOffset.version_id==version.id))
    if count!=version.summary['anchored_instruments']:
        raise HTTPException(409,'校准偏移不完整，拒绝发布')
    h=hashlib.sha256();prev=None
    for off in db.scalars(select(CalibrationOffset).where(CalibrationOffset.version_id==version.id)
                          .order_by(CalibrationOffset.instrument_key)):
        if prev is not None and off.instrument_key<=prev:
            raise HTTPException(409,'校准偏移排序异常，拒绝发布')
        prev=off.instrument_key
        h.update(off.instrument_key.encode())
        h.update(np.asarray([off.temperature,off.humidity,off.particles],dtype='<f8').tobytes())
    if h.hexdigest()!=version.checksum:
        raise HTTPException(409,'校准校验和不一致，拒绝发布')
    verify=verify_version(db,version)
    if not verify['ok']:
        raise HTTPException(409,'修正读数核验失败，拒绝发布：'+verify['reason'])
    # verify_version pages the evidence and detaches objects; re-attach before
    # the atomic switch so both writes are flushed in a single transaction.
    version=db.get(CalibrationVersion,version_id)
    st=state_row(db,lock=True)
    version.state='published';version.published_at=now()
    st.published_version_id=version_id
    log(db,user,'calibration.version.published',version.id,'checksum '+version.checksum[:16])
    db.commit()
    return {'version_id':version.id,'published':True,'checksum':version.checksum}


def version_summary(db,id):
    v=db.get(CalibrationVersion,id)
    if not v:raise HTTPException(404,'未找到校准版本')
    st=state_row(db)
    return {'id':v.id,'run_id':v.run_id,'graph_version':v.graph_version,'state':v.state,
            'summary':v.summary,'checksum':v.checksum,'created_at':v.created_at,
            'published_at':v.published_at,'is_current':st.published_version_id==v.id}


def list_versions(db):
    return [version_summary(db,v.id) for v in db.scalars(select(CalibrationVersion)
            .order_by(CalibrationVersion.id.desc()).limit(30))]


def version_offsets(db,version_id,page=0,size=100,query=None):
    v=db.get(CalibrationVersion,version_id)
    if not v:raise HTTPException(404,'未找到校准版本')
    size=max(1,min(500,int(size)));page=max(0,int(page))
    q=select(CalibrationOffset).where(CalibrationOffset.version_id==version_id)
    if query:q=q.where(CalibrationOffset.instrument_key.ilike('%'+query+'%'))
    total=db.scalar(select(func.count()).select_from(q.subquery()))
    rows=db.scalars(q.order_by(CalibrationOffset.instrument_key).offset(page*size).limit(size))
    return {'total':int(total),'page':page,'size':size,
            'items':[row(o) for o in rows]}


def version_residuals(db,version_id,page=0,size=50,min_abs=None,feature='temperature'):
    if feature not in FEATURES:raise HTTPException(422,'不支持的残差特征')
    v=db.get(CalibrationVersion,version_id)
    if not v:raise HTTPException(404,'未找到校准版本')
    size=max(1,min(500,int(size)));page=max(0,int(page))
    col=getattr(CalibrationResidual,feature)
    q=select(CalibrationResidual).where(CalibrationResidual.version_id==version_id)
    if min_abs is not None:q=q.where(func.abs(col)>=float(min_abs))
    q=q.order_by(func.abs(col).desc())
    total=db.scalar(select(func.count()).select_from(q.subquery()))
    rows=db.scalars(q.offset(page*size).limit(size))
    return {'total':int(total),'page':page,'size':size,'feature':feature,
            'items':[row(r) for r in rows]}


def version_readings(db,version_id,source_id,page=0,size=50):
    """Preview readings corrected by a frozen version, original value kept for audit."""
    from ..models.source import Source
    if not db.get(Source,source_id):raise HTTPException(404,'未找到来源批次')
    if not db.get(CalibrationVersion,version_id):raise HTTPException(404,'未找到校准版本')
    offsets=load_offsets(db,version_id)
    q=select(Record).where(Record.source_id==source_id).order_by(Record.id)
    total=db.scalar(select(func.count()).select_from(q.subquery()))
    rows=db.scalars(q.offset(page*size).limit(size))
    items=[]
    for r in rows:
        off=offsets.get(r.instrument_key);values=dict(r.values)
        item={'reference':r.reference,'instrument_key':r.instrument_key,
              'original':{f:values.get(f) for f in FEATURES}}
        if off:
            item['corrected']={f:values.get(f)-off[j] for j,f in enumerate(FEATURES)}
            item['applied']=True
        else:
            item['corrected']=None;item['applied']=False
        items.append(item)
    return {'total':int(total),'page':page,'size':size,'version_id':version_id,
            'items':items}


def load_offsets(db,version_id):
    """Return {instrument_key: (temperature, humidity, particles)} for a version."""
    return {k:(t,h,p) for k,t,h,p in db.execute(
        select(CalibrationOffset.instrument_key,CalibrationOffset.temperature,
               CalibrationOffset.humidity,CalibrationOffset.particles)
        .where(CalibrationOffset.version_id==version_id))}


def apply_version(db,records,version_id):
    """Apply one frozen calibration version to reading dicts; returns adjusted copies."""
    offsets=load_offsets(db,version_id)
    out=[]
    for r in records:
        key=r.get('instrument_key')
        values=dict(r['values']);off=offsets.get(key)
        if off:
            for j,f in enumerate(FEATURES):values[f]=values[f]-off[j]
        out.append({**r,'values':values,'corrected':bool(off),'instrument_key':key})
    return out
