"""Bounded-memory calibration solver.

Model
-----
Every instrument ``i`` has one additive bias ``o_i`` per feature.  A pair
observation says ``reading_left - reading_right = d`` for co-located,
same-session instruments, so the model equation is::

    o_left - o_right = d        (weight w)

Trusted instruments are pinned to ``o = 0``.  The weighted least-squares
normal equations are the weighted graph Laplacian of the instrument graph.
Temperature, humidity and particles share nodes, edges and weights, so they
form one joint model with three separable right-hand sides.

Memory
------
The Laplacian is never materialised.  Edge arrays cost O(E), CG vectors cost
O(N), and components are solved one at a time, so a 20k-instrument / 100k-pair
graph needs only a few megabytes.  No dense matrix is ever built.

Interruption
------------
CG state ``(x, r, p, rz)`` is checkpointed atomically per component and
feature, keyed by an evidence fingerprint.  Right-hand sides and the diagonal
are recomputed deterministically from the edges on resume, so a retry continues
from the saved iteration and can never count evidence twice.  A corrected
component has a new fingerprint, so it cannot resume from stale state.
"""
import hashlib
import os
import struct
from pathlib import Path

import numpy as np

FEATURES=('temperature','humidity','particles')
SOLVER_VERSION='lap-cg-1'
DEFAULT_TOLERANCE=1e-8
DEFAULT_MAX_ITER=5000


def as_fingerprint(parts):
    h=hashlib.sha256();h.update(SOLVER_VERSION.encode())
    for part in parts:
        h.update(b'\x00')
        h.update(part if isinstance(part,bytes) else str(part).encode())
    return h.hexdigest()


def component_roots(n,left,right):
    """Iterative union-find with path halving; returns one root per node."""
    parent=np.arange(n,dtype=np.int64)
    for a,b in zip(left.tolist(),right.tolist()):
        while parent[a]!=a:
            parent[a]=parent[parent[a]];a=int(parent[a])
        while parent[b]!=b:
            parent[b]=parent[parent[b]];b=int(parent[b])
        if a!=b:
            if a<b:parent[b]=a
            else:parent[a]=b
    roots=np.empty(n,dtype=np.int64)
    for i in range(n):
        a=i
        while parent[a]!=a:
            parent[a]=parent[parent[a]];a=int(parent[a])
        roots[i]=a
    return roots


def component_fingerprint(keys,anchor_flags,left,right,weights,differences):
    """Stable hash of one connected component's evidence.

    Instrument keys are hashed in sorted order together with their anchor flag;
    edges are hashed in canonical sorted order with weight and every feature
    difference.  Values, not row revisions, are hashed, so an idempotent
    re-submission does not invalidate cached work, while a real correction
    changes exactly the affected component's fingerprint.
    """
    order=sorted(range(len(keys)),key=lambda i:keys[i])
    rank=np.empty(len(keys),dtype=np.int64);rank[order]=np.arange(len(keys),dtype=np.int64)
    h=hashlib.sha256();h.update(SOLVER_VERSION.encode())
    buf=bytearray()
    for i in order:
        data=keys[i].encode()
        buf+=struct.pack('<I',len(data))+data+(b'\x01' if anchor_flags[i] else b'\x00')
        if len(buf)>65536:h.update(buf);buf.clear()
    h.update(buf);buf.clear()
    left=np.asarray(left);right=np.asarray(right)
    edge_order=np.lexsort((rank[right],rank[left]))
    wf=np.asarray(weights,dtype='<f8');dfeat=[np.asarray(differences[f],dtype='<f8') for f in FEATURES]
    for e in edge_order.tolist():
        buf+=struct.pack('<qqd',int(rank[left[e]]),int(rank[right[e]]),float(wf[e]))
        for arr in dfeat:buf+=struct.pack('<d',float(arr[e]))
        if len(buf)>65536:h.update(buf);buf.clear()
    h.update(buf)
    return h.hexdigest()


class CheckpointStore:
    """Atomic per-run, per-component CG checkpoints.

    The evidence fingerprint is part of the filename, so a corrected component
    writes a fresh checkpoint and can never resume from stale state.
    ``save`` uses ``os.replace``; retries read and continue, never accumulate.
    """
    def __init__(self,directory,run_key,min_edges=0):
        self.directory=(Path(directory)/('calibration-'+str(run_key))) if directory else None
        if self.directory:self.directory.mkdir(parents=True,exist_ok=True)
        self.min_edges=min_edges
    def enabled(self,edges):
        return bool(self.directory) and (self.min_edges==0 or edges>=self.min_edges)
    def _path(self,fingerprint,feature):
        return self.directory/(fingerprint+'-'+feature+'.npz')
    def load(self,fingerprint,edges):
        if not self.enabled(edges):return None
        out={}
        for feature in FEATURES:
            try:
                with np.load(self._path(fingerprint,feature)) as data:
                    if str(data['fingerprint'])!=fingerprint:return None
                    out[feature]={'iteration':int(data['iteration']),'x':data['x'].copy(),
                                  'r':data['r'].copy(),'p':data['p'].copy(),'rz':float(data['rz'])}
            except (FileNotFoundError,OSError,KeyError,ValueError):
                return None
        return out
    def save(self,fingerprint,edges,feature,iteration,x,r,p,rz):
        if not self.enabled(edges):return
        path=self._path(fingerprint,feature);tmp=path.with_suffix('.tmp.npz')
        np.savez(tmp,fingerprint=np.array(fingerprint),iteration=np.array(iteration),
                 x=x,r=r,p=p,rz=np.array(rz))
        os.replace(tmp,path)
    def clear(self,fingerprint):
        if not self.directory:return
        for feature in FEATURES:
            try:self._path(fingerprint,feature).unlink()
            except FileNotFoundError:pass


def _matvec(x,ue,ve,w,nfree):
    ext=np.empty(nfree+1,dtype=np.float64);ext[:nfree]=x;ext[nfree]=0.0
    wd=w*(ext[ue]-ext[ve])
    return (np.bincount(ue,weights=wd,minlength=nfree+1)[:nfree]
            -np.bincount(ve,weights=wd,minlength=nfree+1)[:nfree])


def solve_feature(n,left,right,weights,differences,is_anchor,store,fingerprint,feature,
                  tolerance=DEFAULT_TOLERANCE,max_iter=DEFAULT_MAX_ITER,checkpoint_every=50):
    """Solve one feature on a connected component; anchors held at zero."""
    free=~np.asarray(is_anchor,dtype=bool);nfree=int(free.sum())
    if nfree==0:
        return {'x':np.zeros(n),'converged':True,'iterations':0,'residual_norm':0.0}
    free_index=np.full(n,-1,dtype=np.int32);free_index[free]=np.arange(nfree,dtype=np.int32)
    # Anchors map to a sentinel slot whose value stays zero.
    ue=np.where(free[left],free_index[left],nfree).astype(np.int32)
    ve=np.where(free[right],free_index[right],nfree).astype(np.int32)
    w=np.asarray(weights,dtype=np.float64);d=np.asarray(differences,dtype=np.float64)
    diag=np.bincount(ue,weights=w,minlength=nfree+1)[:nfree]+np.bincount(ve,weights=w,minlength=nfree+1)[:nfree]
    diag=np.maximum(diag,1e-300)
    wd=w*d
    b=(np.bincount(ue,weights=wd,minlength=nfree+1)[:nfree]
       -np.bincount(ve,weights=wd,minlength=nfree+1)[:nfree])
    norm_b=float(np.linalg.norm(b))
    if norm_b==0:
        store.clear(fingerprint)
        return {'x':np.zeros(n),'converged':True,'iterations':0,'residual_norm':0.0}
    state=store.load(fingerprint,left.size)
    saved=state[feature] if state and state[feature]['x'].size==nfree else None
    if saved:
        x,r,p,rz,iteration=(saved['x'].copy(),saved['r'].copy(),saved['p'].copy(),
                            saved['rz'],saved['iteration'])
    else:
        x=np.zeros(nfree,dtype=np.float64);r=b.copy();z=r/diag;p=z.copy()
        rz=float(np.dot(r,z));iteration=0
    threshold=tolerance*norm_b;converged=False
    while iteration<max_iter:
        Ap=_matvec(p,ue,ve,w,nfree)
        curvature=float(np.dot(p,Ap))
        if not np.isfinite(curvature) or curvature<=0:break
        alpha=rz/curvature
        x+=alpha*p;r-=alpha*Ap;iteration+=1
        if float(np.linalg.norm(r))<=threshold:
            converged=True;break
        z=r/diag;rz_new=float(np.dot(r,z))
        if rz_new<=0:break
        p=z+(rz_new/rz)*p;rz=rz_new
        if checkpoint_every and iteration%checkpoint_every==0:
            store.save(fingerprint,left.size,feature,iteration,x,r,p,rz)
    residual_norm=float(np.linalg.norm(r))
    if residual_norm<=threshold:converged=True
    if converged:store.clear(fingerprint)
    else:store.save(fingerprint,left.size,feature,iteration,x,r,p,rz)
    full=np.zeros(n,dtype=np.float64);full[free]=x
    return {'x':full,'converged':converged,'iterations':iteration,'residual_norm':residual_norm}


def estimate_bytes(nodes,edges,features=3):
    """Conservative working-set estimate for this streaming solver."""
    per_edge=8*3+8*features
    per_node=8*(1+features+4)
    return nodes*per_node+edges*per_edge


def solve(keys,anchor_flags,left,right,weights,differences,*,tolerance=DEFAULT_TOLERANCE,
          max_iter=DEFAULT_MAX_ITER,checkpoint_dir=None,run_key='',min_checkpoint_edges=0,
          checkpoint_every=50,alert_thresholds=None,cache_get=None,cache_put=None,
          progress=None,memory_limit=128*1024*1024):
    """Solve the joint three-feature calibration graph.

    ``differences[f]`` is a length-E array defined as ``o_left - o_right``.
    Returns per-instrument offsets (NaN for isolated unanchored components),
    per-pair residuals, weighted statistics, component accounting and an
    explicit isolation report.  ``cache_get``/``cache_put`` work on
    per-component fingerprints, so a correction re-solves only its component.
    """
    keys=[str(k) for k in keys]
    n=len(keys);left=np.asarray(left,dtype=np.int32);right=np.asarray(right,dtype=np.int32)
    weights=np.asarray(weights,dtype=np.float64).copy();e=int(left.size)
    alert_thresholds=alert_thresholds or {f:1.0 for f in FEATURES}
    stats={f:{'rss':0.0,'max_abs':0.0,'weighted_mean_abs':0.0,'over_alert':0,
              'alert':float(alert_thresholds[f]),'iterations':0} for f in FEATURES}
    if e==0:
        return {'offsets':{f:np.full(n,np.nan) for f in FEATURES},
                'residuals':{f:np.full(0,np.nan) for f in FEATURES},
                'stats':stats,
                'components':{'anchored':0,'solved':0,'cached':0,'unanchored':0,'converged':0},
                'unanchored':{'instruments':n,'pairs':0,'examples':[]},
                'iterations':0,'estimate_bytes':estimate_bytes(n,0),
                'component_count':0,'nodes':n,'edges':0,'pair_edges':np.full(0,-1,dtype=np.int64)}
    anchor_flags=np.asarray(anchor_flags,dtype=bool)
    estimated=estimate_bytes(n,e)
    if estimated>memory_limit:
        raise MemoryError('校准图工作集超出 %d MB 求解预算'%(memory_limit//(1024*1024)))
    roots=component_roots(n,left,right)
    comp_ids,inv=np.unique(roots,return_inverse=True)
    node_order=np.argsort(inv,kind='stable')
    node_bounds=np.searchsorted(inv[node_order],np.arange(comp_ids.size+1))
    edge_comp=inv[left]
    edge_order=np.argsort(edge_comp,kind='stable')
    edge_bounds=np.searchsorted(edge_comp[edge_order],np.arange(comp_ids.size+1))
    anchored_nodes=np.bincount(inv[anchor_flags],minlength=comp_ids.size)
    offsets={f:np.full(n,np.nan,dtype=np.float64) for f in FEATURES}
    residuals={f:np.full(e,np.nan,dtype=np.float64) for f in FEATURES}
    store=CheckpointStore(checkpoint_dir,run_key,min_edges=min_checkpoint_edges)
    summary={'anchored':0,'solved':0,'cached':0,'unanchored':0,'converged':0}
    unanchored_examples=[];total_iterations=0;anchored_instruments=0;cached_pairs=0
    all_diffs={f:np.asarray(differences[f],dtype=np.float64) for f in FEATURES}
    for cid in range(comp_ids.size):
        nodes=node_order[node_bounds[cid]:node_bounds[cid+1]]
        if nodes.size==0:continue
        edge_idx=edge_order[edge_bounds[cid]:edge_bounds[cid+1]]
        if anchored_nodes[cid]==0:
            summary['unanchored']+=1
            unanchored_examples.append({'instruments':int(nodes.size),'pairs':int(edge_idx.size),
                                        'keys':[keys[i] for i in sorted(nodes.tolist())[:5]]})
            continue
        summary['anchored']+=1;anchored_instruments+=int(nodes.size)
        nodes=sorted(nodes.tolist(),key=lambda i:keys[i])  # stable local order
        local={g:i for i,g in enumerate(nodes)};nodes_arr=np.asarray(nodes,dtype=np.int64)
        lu=np.asarray([local[int(i)] for i in left[edge_idx]],dtype=np.int32)
        lv=np.asarray([local[int(i)] for i in right[edge_idx]],dtype=np.int32)
        anc=anchor_flags[nodes_arr];w=weights[edge_idx]
        local_diffs={f:all_diffs[f][edge_idx] for f in FEATURES}
        fp=component_fingerprint([keys[i] for i in nodes],anc,lu,lv,w,local_diffs)
        cache_key=as_fingerprint([fp,tolerance,max_iter])
        cached=cache_get(cache_key) if cache_get else None
        if cached and cached.get('status')=='ok' and \
                all(len(cached['offsets'][f])==nodes_arr.size for f in FEATURES):
            comp_offsets={f:np.asarray(cached['offsets'][f],dtype=np.float64) for f in FEATURES}
            summary['cached']+=1;summary['converged']+=1;cached_pairs+=int(edge_idx.size)
        else:
            comp_offsets={};comp_stats={}
            for f in FEATURES:
                sol=solve_feature(nodes_arr.size,lu,lv,w,local_diffs[f],anc,store,fp,f,
                                  tolerance=tolerance,max_iter=max_iter,
                                  checkpoint_every=checkpoint_every)
                comp_offsets[f]=sol['x'];total_iterations+=sol['iterations']
                stats[f]['iterations']+=sol['iterations']
                comp_stats[f]={'iterations':sol['iterations'],'converged':bool(sol['converged']),
                               'residual_norm':sol['residual_norm']}
            summary['solved']+=1
            if all(v['converged'] for v in comp_stats.values()):
                summary['converged']+=1
                if cache_put:
                    cache_put(cache_key,{'status':'ok','nodes':int(nodes_arr.size),
                                         'edges':int(edge_idx.size),'anchors':int(anc.sum()),
                                         'offsets':{f:comp_offsets[f].tolist() for f in FEATURES},
                                         'stats':comp_stats})
        for f in FEATURES:
            x=comp_offsets[f];offsets[f][nodes_arr]=x
            res=x[lu]-x[lv]-local_diffs[f];residuals[f][edge_idx]=res
            aw=np.abs(res);st=stats[f]
            st['rss']+=float((w*res*res).sum())
            if aw.size:st['max_abs']=max(st['max_abs'],float(aw.max()))
            st['weighted_mean_abs']+=float((aw*w).sum());st['over_alert']+=int((aw>st['alert']).sum())
        if progress:
            progress({'anchored':summary['anchored'],'unanchored':summary['unanchored'],
                      'cached':summary['cached'],'converged':summary['converged'],
                      'iterations':total_iterations})
    total_w=float(weights.sum())
    for f in FEATURES:stats[f]['weighted_mean_abs']/=total_w if total_w else 1.0
    unanchored_examples.sort(key=lambda x:(-x['pairs'],-x['instruments']))
    unanchored_pairs=int(np.isnan(residuals[FEATURES[0]]).sum())
    return {'offsets':offsets,'residuals':residuals,
            'stats':stats,'components':summary,
            'unanchored':{'instruments':int(n-anchored_instruments),'pairs':unanchored_pairs,
                          'examples':unanchored_examples[:10]},
            'iterations':total_iterations,'estimate_bytes':estimated,
            'component_count':int(comp_ids.size),'nodes':n,'edges':e,
            'pair_edges':np.arange(e,dtype=np.int64),'cached_pairs':cached_pairs}
