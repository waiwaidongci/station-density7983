import numpy as np
import pytest

from src.services.calibration_solver import FEATURES,estimate_bytes,solve


def oracle(keys,anchors,left,right,weights,differences):
    n=len(keys);adj={i:set() for i in range(n)}
    for a,b in zip(left,right):adj[a].add(b);adj[b].add(a)
    seen=set();anchored_nodes=set()
    for i in range(n):
        if i in seen:continue
        stack=[i];comp=[]
        while stack:
            v=stack.pop()
            if v in seen:continue
            seen.add(v);comp.append(v);stack.extend(adj[v]-seen)
        if any(anchors[v] for v in comp):anchored_nodes.update(comp)
    free=[i for i in range(n) if i in anchored_nodes and not anchors[i]]
    idx={g:i for i,g in enumerate(free)}
    out={f:np.full(n,np.nan) for f in FEATURES}
    for f in FEATURES:
        rows=[];rhs=[]
        for a,b,w,d in zip(left,right,weights,differences[f]):
            if a not in anchored_nodes or b not in anchored_nodes:continue
            row=np.zeros(len(free))
            if a in idx:row[idx[a]]+=1
            if b in idx:row[idx[b]]-=1
            rows.append(row*np.sqrt(w));rhs.append(d*np.sqrt(w))
        x=np.linalg.lstsq(np.array(rows),np.array(rhs),rcond=None)[0] if rows else np.zeros(len(free))
        out[f][free]=x
        for i in anchored_nodes:
            if anchors[i]:out[f][i]=0.0
    return out


def run(keys,anchors,left,right,weights,diffs,**kw):
    return solve(keys,np.asarray(anchors),np.asarray(left,np.int32),np.asarray(right,np.int32),
                 np.asarray(weights,float),{f:np.asarray(diffs[f],float) for f in FEATURES},**kw)


def test_weighted_least_squares_matches_dense_oracle():
    keys=['a','b','c'];anchors=[True,False,False]
    left=[0,1,2];right=[1,2,0];weights=[1,2,3]
    diffs={'temperature':[1,2,.2],'humidity':[10,20,2],'particles':[3,4,.5]}
    got=run(keys,anchors,left,right,weights,diffs)
    expected=oracle(keys,np.array(anchors),left,right,weights,diffs)
    for f in FEATURES:
        assert np.allclose(got['offsets'][f],expected[f],atol=1e-8)


def test_loop_contradiction_retained_as_nonzero_residual():
    keys=['a','b','c'];anchors=[True,False,False]
    left=[0,1,2];right=[1,2,0];weights=[1,2,3]
    diffs={f:[1.0,2.0,0.2] for f in FEATURES}
    got=run(keys,anchors,left,right,weights,diffs)
    for f in FEATURES:
        assert np.any(np.abs(got['residuals'][f])>1e-6)
        assert got['stats'][f]['rss']>0


def test_unanchored_component_is_isolated():
    keys=['a','b','c','d'];anchors=[True,False,False,False]
    left=[0,2];right=[1,3];weights=[1,1]
    diffs={f:[1.0,5.0] for f in FEATURES}
    got=run(keys,anchors,left,right,weights,diffs)
    assert np.isnan(got['offsets']['temperature'][2:]).all()
    assert got['unanchored']['instruments']==2
    assert got['unanchored']['pairs']==1


def test_anchor_offsets_are_zero_and_anchored_component_solved():
    keys=['a','b'];anchors=[True,False]
    got=run(keys,anchors,[0],[1],[1],{f:[2.0] for f in FEATURES})
    for f in FEATURES:
        assert got['offsets'][f][0]==0.0
        assert got['offsets'][f][1]==-2.0


def test_retry_resumes_without_double_counting(tmp_path):
    rng=np.random.default_rng(3);n=120
    left=rng.integers(0,n,900);right=(left+rng.integers(1,n,900))%n
    anchors=np.zeros(n,bool);anchors[[0,5]]=True
    bias=rng.normal(0,1,n);bias[anchors]=0
    diffs={f:bias[left]-bias[right]+rng.normal(0,.01,left.size) for f in FEATURES}
    keys=[f'i{k}' for k in range(n)];weights=np.ones(left.size)
    kwargs=dict(checkpoint_dir=str(tmp_path),run_key='resume',max_iter=20000)
    one=run(keys,anchors,left,right,weights,diffs,**kwargs)
    import shutil
    shutil.rmtree(tmp_path/'calibration-resume',ignore_errors=True)
    first=run(keys,anchors,left,right,weights,diffs,checkpoint_dir=str(tmp_path),run_key='resume',max_iter=2)
    second=run(keys,anchors,left,right,weights,diffs,**kwargs)
    assert second['iterations']>first['iterations']
    for f in FEATURES:
        assert np.allclose(one['offsets'][f],second['offsets'][f],atol=1e-6)
        assert abs(one['stats'][f]['rss']-second['stats'][f]['rss'])<1e-8


def test_twenty_thousand_instruments_under_memory_budget():
    rng=np.random.default_rng(5);n=20000;e=100000
    left=np.concatenate([np.arange(n-1),rng.integers(0,n,e-(n-1))])
    right=np.concatenate([np.arange(1,n),rng.integers(0,n,e-(n-1))])
    keep=left!=right;left,right=left[keep],right[keep]
    anchors=np.zeros(n,bool);anchors[rng.choice(n,100,replace=False)]=True
    bias=rng.normal(0,1,n);bias[anchors]=0
    diffs={f:bias[left]-bias[right]+rng.normal(0,.02,left.size) for f in FEATURES}
    keys=[f'INS-{i:05d}' for i in range(n)]
    assert estimate_bytes(n,left.size)<128*1024*1024
    got=run(keys,anchors,left,right,np.ones(left.size),diffs,tolerance=1e-9,max_iter=5000)
    assert got['components']['converged']==got['components']['anchored']
    err=got['offsets']['temperature'][~anchors]-bias[~anchors]
    assert float(np.sqrt(np.mean(err**2)))<0.05


def test_memory_budget_refuses_oversized_graph():
    with pytest.raises(MemoryError):
        solve([str(i) for i in range(10)],np.zeros(10,bool),np.array([0]),np.array([1]),
              np.array([1.0]),{f:np.array([0.0]) for f in FEATURES},memory_limit=16)
