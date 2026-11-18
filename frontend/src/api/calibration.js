import {api} from './client.js';
export const calibrationApi={
  instruments:()=>api('/calibration/instruments'),
  saveInstrument:data=>api('/calibration/instruments',{method:'POST',body:JSON.stringify(data)}),
  setAnchor:(key,is_anchor)=>api('/calibration/instruments/'+encodeURIComponent(key)+'/anchor',{method:'POST',body:JSON.stringify({is_anchor})}),
  pairs:(page=0,size=20,key='')=>api('/calibration/pairs?page='+page+'&size='+size+(key?'&key='+encodeURIComponent(key):'')),
  addPairs:observations=>api('/calibration/pairs',{method:'POST',body:JSON.stringify({observations})}),
  correctPair:(id,data)=>api('/calibration/pairs/'+id,{method:'PUT',body:JSON.stringify(data)}),
  deletePair:id=>api('/calibration/pairs/'+id,{method:'DELETE'}),
  runs:()=>api('/calibration/runs'),
  run:id=>api('/calibration/runs/'+id),
  createRun:data=>api('/calibration/runs',{method:'POST',body:JSON.stringify(data)}),
  publish:id=>api('/calibration/runs/'+id+'/publish',{method:'POST'}),
  retry:id=>api('/calibration/runs/'+id+'/retry',{method:'POST'}),
  versions:()=>api('/calibration/versions'),
  version:id=>api('/calibration/versions/'+id),
  offsets:(id,page=0,size=20,q='')=>api('/calibration/versions/'+id+'/offsets?page='+page+'&size='+size+(q?'&q='+encodeURIComponent(q):'')),
  residuals:(id,page=0,size=20,feature='temperature',minAbs='')=>api('/calibration/versions/'+id+'/residuals?page='+page+'&size='+size+'&feature='+feature+(minAbs!==''?'&min_abs='+minAbs:'')),
  readings:(id,sourceId,page=0,size=10)=>api('/calibration/versions/'+id+'/readings?source_id='+sourceId+'&page='+page+'&size='+size)
};
