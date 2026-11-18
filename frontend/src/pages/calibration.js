import {domain} from '../constants/domain.js';import {research} from '../stores/research.js';import {session} from '../stores/session.js';import {calibrationApi} from '../api/calibration.js';import {sourceOptions} from '../components/common/business.js';import {status} from '../components/common/status.js';import {table} from '../components/common/table.js';import {esc,fmt,formObject,localTime} from '../utils/format.js';import {action,toast,bind} from '../hooks/actions.js';

const state={instruments:[],graph:null,pairs:null,pairPage:0,pairKey:'',runs:[],versions:[],version:null,offsets:null,offsetPage:0,residuals:null,feature:'temperature',residualPage:0,minAbs:'',readings:null,sourceId:null};
const anchors=()=>state.instruments.filter(i=>i.is_anchor).map(i=>i.key);
const versionOptions=()=>state.versions.map(v=>'<option value="'+v.id+'" '+(state.version?.id==v.id?'selected':'')+'>v'+v.id+' · '+(v.is_current?'当前发布':'历史')+' · #'+v.run_id+'</option>').join('');
const runOptions=()=>state.runs.map(r=>'<option value="'+r.id+'">#'+r.id+' '+esc(r.name)+' · '+r.state+'</option>').join('');

async function load(){
  const [inst,runs,versions]=await Promise.all([calibrationApi.instruments(),calibrationApi.runs(),calibrationApi.versions()]);
  state.instruments=inst.items;state.graph=inst.graph;state.runs=runs.items;state.versions=versions.items;
  if(!state.version&&state.versions.length)state.version=state.versions.find(v=>v.is_current)||state.versions[0];
  state.pairs=await calibrationApi.pairs(state.pairPage,20,state.pairKey);
  if(state.version){
    [state.offsets,state.residuals]=await Promise.all([calibrationApi.offsets(state.version.id,state.offsetPage,20),calibrationApi.residuals(state.version.id,state.residualPage,20,state.feature,state.minAbs)]);
  }
}

const pairForm=()=>'<form id="pair-form"><div class="parameter-fields"><label>仪器甲<input name="left_key" required placeholder="HL-INSTR-01"></label><label>仪器乙<input name="right_key" required placeholder="HL-INSTR-02"></label></div><div class="parameter-fields"><label>站点<input name="site" maxlength="120" placeholder="同一地点"></label><label>场次<input name="session_key" maxlength="120" placeholder="同一次比对"></label></div><div class="parameter-fields">'+domain.fields.map(([key,label])=>'<label>配对差 · '+esc(label)+'<input name="'+key+'" type="number" step="any" required placeholder="甲读数 − 乙读数"></label>').join('')+'<label>权重<input name="weight" type="number" step="any" min="0.000001" value="1" required></label></div><p class="fine">差值按“仪器甲 − 仪器乙”录入，方向由系统规范化；同一站点与场次的配对只能录入一次，更正请用配对表的“更正”。</p><button type="submit">录入配对 →</button></form>';

export async function calibrationPage(){
  await load();
  const graph=state.graph;
  const version=state.version;
  const summary=version?.summary;
  const budgetPct=graph?Math.min(100,graph.estimate_bytes/graph.budget_bytes*100):0;

  const anchorRows=state.instruments.map(i=>({key:i.key,is_anchor:i.is_anchor?'零偏锚点':'自由',pairs:i.pairs}));
  const pairRows=state.pairs.items.map(p=>({id:p.id,left_key:p.left_key,right_key:p.right_key,site:p.site,
    session_key:p.session_key,weight:p.weight,temperature:p.temperature,humidity:p.humidity,
    particles:p.particles,revision:p.revision}));
  const pairActions=state.pairs.items.map(p=>'<tr><td>#'+p.id+'</td><td>'+esc(p.left_key)+' / '+esc(p.right_key)+'</td><td>'+esc(p.site)+' · '+esc(p.session_key)+'</td><td><form class="inline-correct" data-pair="'+p.id+'"><input name="weight" type="number" step="any" min="0.000001" value="'+p.weight+'" required style="width:90px"> <input name="temperature" type="number" step="any" value="'+p.temperature+'" required style="width:100px"> <input name="humidity" type="number" step="any" value="'+p.humidity+'" required style="width:100px"> <input name="particles" type="number" step="any" value="'+p.particles+'" required style="width:100px"> <button type="submit" class="secondary">更正</button></form></td></tr>').join('');
  const pairEditor=state.pairs.items.length?'<details><summary>更正配对差（只失效受影响分量）</summary><div class="table-wrap"><table><thead><tr><th>#</th><th>仪器</th><th>站点·场次</th><th>权重 / 温差 / 湿差 / 颗粒差</th></tr></thead><tbody>'+pairActions+'</tbody></table></div></details>':'';

  const instrumentBlock='<h2>仪器与可信锚点 <small>'+state.instruments.length+' 台 · '+anchors().length+' 台锚点</small></h2>'
    +table(anchorRows,[{key:'key',label:'仪器编号'},{key:'is_anchor',label:'角色'},{key:'pairs',label:'参与配对'}])
    +(session.writable?'<details><summary>登记仪器 / 指定锚点</summary><form id="instrument-form"><div class="parameter-fields"><label>仪器编号<input name="key" required placeholder="HL-INSTR-03"></label><label>名称<input name="name" placeholder="可选"></label></div><label><span><input type="checkbox" name="is_anchor" value="1" style="width:auto;margin-right:8px">设为零偏可信仪器</span></label><button type="submit">保存仪器</button></form></details>':'');

  const pairBlock='<h2>同地同次配对差 <small>共 '+(state.pairs.total||0)+' 条</small></h2>'
    +(session.writable?pairForm():'<p class="fine">阅读者可查看配对与校准状态；录入由分析师或负责人完成。</p>')
    +'<form id="pair-filter" class="compact"><label>按仪器筛选<input name="key" value="'+esc(state.pairKey)+'" placeholder="仪器编号"></label><button type="submit" class="secondary">筛选</button></form>'
    +table(pairRows,[{key:'id',label:'#'},{key:'left_key',label:'仪器甲'},{key:'right_key',label:'仪器乙'},{key:'site',label:'站点'},{key:'session_key',label:'场次'},{key:'temperature',label:'温差'},{key:'humidity',label:'湿差'},{key:'particles',label:'颗粒差'},{key:'weight',label:'权重'},{key:'revision',label:'修订'}])
    +(state.pairs.total>20?'<div class="pagination"><button id="pair-prev" '+(state.pairPage===0?'disabled':'')+'>上一页</button><span>'+(state.pairPage+1)+' / '+Math.ceil(state.pairs.total/20)+'</span><button id="pair-next" '+((state.pairPage+1)*20>=state.pairs.total?'disabled':'')+'>下一页</button></div>':'')
    +(session.writable?pairEditor:'');

  const budgetBlock='<p class="eyebrow">GRAPH / MEMORY</p><h2>求解预算</h2>'
    +(graph?'<p>仪器 '+fmt(graph.instruments)+' · 配对 '+fmt(graph.pairs)+' · 图版本 v'+graph.graph_version+'</p><p class="fine">估算工作集 '+fmt(Math.round(graph.estimate_bytes/1024))+' KB / 预算 '+Math.round(graph.budget_bytes/1024/1024)+' MB（'+budgetPct.toFixed(2)+'%）· 图 Laplacian 不建稠密方阵</p>':'');

  const solveBlock=(session.writable?'<h2>发起校准</h2><form id="calibration-form"><label>任务名称<input name="name" required value="全网零偏校准"></label><div class="parameter-fields"><label>收敛容差<input name="tolerance" type="number" step="any" value="1e-8"></label><label>最大迭代<input name="max_iter" type="number" value="5000"></label></div><div class="parameter-fields"><label>温差告警阈值<input name="alert_temperature" type="number" step="any" value="1"></label><label>湿差告警阈值<input name="alert_humidity" type="number" step="any" value="5"></label><label>颗粒差告警阈值<input name="alert_particles" type="number" step="any" value="10"></label></div><button type="submit">冻结配对图并求解 →</button></form>':'')
    +'<h2>校准任务</h2>'
    +(state.runs.length?'<div class="ledger">'+state.runs.slice(0,6).map(r=>'<div style="padding:10px 0;border-bottom:1px solid var(--line)"><div style="display:flex;justify-content:space-between;gap:8px"><span>#'+r.id+' '+esc(r.name)+'</span>'+status(r.state)+'</div>'+(r.state==='failed'||r.state==='stale'?'<p class="fine">'+esc(r.error||'')+'</p>'+(r.state==='failed'&&session.writable?'<button class="secondary" data-retry="'+r.id+'" style="font-size:.7rem;padding:4px 10px">从检查点重试 ↻</button>':''):'')+'</div>').join('')+'</div>':'<p class="fine">还没有校准任务。</p>')
    +(state.runs.some(r=>r.state==='completed')&&session.user.role==='admin'?'<form id="publish-form"><label>发布已完成任务<select name="run_id">'+state.runs.filter(r=>r.state==='completed').map(r=>'<option value="'+r.id+'">#'+r.id+' '+esc(r.name)+'</option>').join('')+'</select></label><button type="submit" class="secondary">核验并原子发布 ✓</button></form>':'');

  const versionPicker='<h2>校准版本 <small>新研究冻结提交时的已发布版本，既有研究不变</small></h2>'
    +(state.versions.length?'<form id="version-form" class="compact"><label>查看版本<select name="version_id">'+versionOptions()+'</select></label></form>':'<p class="empty">尚未发布校准版本。</p>');

  let detail='';
  if(version&&summary){
    const statRows=domain.fields.map(([f,label])=>({feature:label,rss:summary.stats[f].rss,max_abs:summary.stats[f].max_abs,
      weighted_mean_abs:summary.stats[f].weighted_mean_abs,over_alert:summary.stats[f].over_alert,alert:summary.stats[f].alert}));
    const isolation=summary.unanchored&&summary.unanchored.instruments
      ?'<p>'+fmt(summary.unanchored.instruments)+' 台仪器、'+fmt(summary.unanchored.pairs)+' 条配对缺少可信锚点，整段不输出偏移。</p>'
        +table((summary.unanchored.examples||[]).map((x,i)=>({group:'#'+(i+1),instruments:x.instruments,pairs:x.pairs,keys:x.keys.join('、')})),[{key:'group',label:'分量'},{key:'instruments',label:'仪器数'},{key:'pairs',label:'配对数'},{key:'keys',label:'示例仪器'}],{limit:10})
      :'<p>全部分量均含可信锚点，没有仪器被隔离。</p>';
    const offsetRows=state.offsets.items.map(o=>({instrument_key:o.instrument_key,temperature:o.temperature,humidity:o.humidity,particles:o.particles}));
    const readingBlock=research.sources.length
      ?'<h2>修正读数核验 <small>逐条保留原始读数；未校准时整行标注</small></h2><form id="readings-form" class="compact"><div class="parameter-fields"><label>来源批次<select name="source_id">'+sourceOptions(research.sources,state.sourceId||research.sourceId)+'</select></label><button type="submit" class="secondary" style="align-self:end">核验修正读数</button></div></form>'
        +(state.readings?table(state.readings.items.map(x=>({reference:x.reference,instrument_key:x.instrument_key,applied:x.applied?'已修正':'未校准',
            original_temperature:x.original.temperature,corrected_temperature:x.corrected?.temperature,
            original_humidity:x.original.humidity,corrected_humidity:x.corrected?.humidity})),
            [{key:'reference',label:'样本编号'},{key:'instrument_key',label:'仪器'},{key:'applied',label:'状态'},{key:'original_temperature',label:'原温度'},{key:'corrected_temperature',label:'修正温度'},{key:'original_humidity',label:'原湿度'},{key:'corrected_humidity',label:'修正湿度'}]):'')
      :'';
    detail='<div class="run-title"><h2>v'+version.id+' <small>任务 #'+version.run_id+'</small></h2>'+status(version.state)
      +(version.is_current?'<span class="status completed">当前发布</span>':'')
      +'<small>图版本 v'+version.graph_version+' · 校验和 <code>'+esc(version.checksum.slice(0,24))+'</code></small></div>'
      +'<div class="metrics"><div><dt>已校准仪器</dt><dd>'+fmt(summary.anchored_instruments)+'</dd></div><div><dt>隔离仪器（无锚点分量）</dt><dd>'+fmt(summary.isolated_instruments)+'</dd></div><div><dt>复用分量</dt><dd>'+fmt(summary.components.cached)+'</dd></div><div><dt>新解分量</dt><dd>'+fmt(summary.components.solved)+'</dd></div></div>'
      +'<div class="result-grid"><section><h2>加权残差 · 三特征</h2>'
      +table(statRows,[{key:'feature',label:'特征'},{key:'rss',label:'加权残差平方和'},{key:'max_abs',label:'最大绝对残差'},{key:'weighted_mean_abs',label:'加权平均绝对残差'},{key:'over_alert',label:'超阈值配对'},{key:'alert',label:'阈值'}])
      +'<p class="fine">环路矛盾不能同时满足，矛盾证据保留在上表残差中，不会被摊入零偏。</p></section><aside class="method-sheet"><p class="eyebrow">ISOLATION</p><h2>无锚点隔离</h2>'+isolation+'</aside></div>'
      +'<h2>仪器零偏 <small>原始量纲；修正值 = 读数 − 零偏</small></h2>'
      +table(offsetRows,[{key:'instrument_key',label:'仪器编号'},{key:'temperature',label:'温度零偏'},{key:'humidity',label:'湿度零偏'},{key:'particles',label:'颗粒零偏'}])
      +'<div class="pagination"><button id="off-prev" '+(state.offsetPage===0?'disabled':'')+'>上一页</button><span>'+(state.offsetPage+1)+' / '+Math.max(1,Math.ceil(state.offsets.total/20))+'</span><button id="off-next" '+((state.offsetPage+1)*20>=state.offsets.total?'disabled':'')+'>下一页</button></div>'
      +'<h2>配对残差明细 <small>共 '+state.residuals.total+' 条，按绝对残差降序</small></h2>'
      +'<form id="residual-form" class="compact"><div class="parameter-fields"><label>特征<select name="feature">'+domain.fields.map(([f,label])=>'<option value="'+f+'" '+(state.feature===f?'selected':'')+'>'+esc(label)+'</option>').join('')+'</select></label><label>最小绝对残差<input name="min_abs" type="number" step="any" value="'+esc(state.minAbs)+'"></label></div><button type="submit" class="secondary">筛选残差</button></form>'
      +table(state.residuals.items,[{key:'pair_id',label:'配对 #'},{key:'left_key',label:'仪器甲'},{key:'right_key',label:'仪器乙'},{key:'weight',label:'权重'},{key:'temperature',label:'温度残差'},{key:'humidity',label:'湿度残差'},{key:'particles',label:'颗粒残差'}])
      +'<div class="pagination"><button id="res-prev" '+(state.residualPage===0?'disabled':'')+'>上一页</button><span>'+(state.residualPage+1)+' / '+Math.max(1,Math.ceil(state.residuals.total/20))+'</span><button id="res-next" '+((state.residualPage+1)*20>=state.residuals.total?'disabled':'')+'>下一页</button></div>'
      +readingBlock;
  }

  return '<section class="page-heading"><p class="eyebrow">03 / NETWORK CALIBRATION</p><h1>先对齐全网零偏，再比较微站差异。</h1><p>同地同次配对差建立仪器零偏方程；可信仪器固定为零偏，加权残差平方和最小。无锚点的连通分量整段隔离，环路矛盾保留为残差，不摊进偏移。</p></section>'
    +'<div class="split"><section>'+instrumentBlock+pairBlock+'</section><aside class="method-sheet">'+budgetBlock+solveBlock+'</aside></div>'
    +versionPicker+detail;
}

export function bindCalibration(refresh){
  action('#instrument-form',async form=>{const data=formObject(form);await calibrationApi.saveInstrument({key:data.key,name:data.name||'',is_anchor:Boolean(data.is_anchor)});await refresh();toast('仪器已保存')});
  action('#pair-form',async form=>{const data=formObject(form);const obs={left_key:data.left_key,right_key:data.right_key,site:data.site,session_key:data.session_key,observed_at:new Date().toISOString(),weight:Number(data.weight),temperature:Number(data.temperature),humidity:Number(data.humidity),particles:Number(data.particles)};await calibrationApi.addPairs([obs]);await refresh();toast('配对已录入，图版本已更新')});
  action('#pair-filter',async form=>{state.pairKey=formObject(form).key.trim();state.pairPage=0;await refresh()});
  for(const b of document.querySelectorAll('[data-retry]'))b.onclick=async()=>{
    try{await calibrationApi.retry(b.dataset.retry);await refresh();toast('校准任务 #'+b.dataset.retry+' 已重新入队')}
    catch(error){toast(error.message,true)}
  };
  for(const form of document.querySelectorAll('.inline-correct'))form.addEventListener('submit',async event=>{
    event.preventDefault();
    const data=formObject(form),pair=state.pairs.items.find(p=>p.id===Number(form.dataset.pair));
    try{
      await calibrationApi.correctPair(pair.id,{left_key:pair.left_key,right_key:pair.right_key,
        site:pair.site,session_key:pair.session_key,observed_at:pair.observed_at||'',
        weight:Number(data.weight),temperature:Number(data.temperature),
        humidity:Number(data.humidity),particles:Number(data.particles)});
      await refresh();toast('配对 #'+pair.id+' 已更正，图版本已更新');
    }catch(error){toast(error.message,true)}
  });
  action('#calibration-form',async form=>{const data=formObject(form);const run=await calibrationApi.createRun({name:data.name,tolerance:Number(data.tolerance),max_iter:Number(data.max_iter),alert_temperature:Number(data.alert_temperature),alert_humidity:Number(data.alert_humidity),alert_particles:Number(data.alert_particles)});await refresh();toast('校准任务 #'+run.id+' 已进入队列')});
  action('#publish-form',async form=>{const r=await calibrationApi.publish(Number(formObject(form).run_id));await refresh();toast('已核验并发布 v'+r.version_id)});
  action('#version-form',async form=>{const id=Number(formObject(form).version_id);state.version=state.versions.find(v=>v.id===id);state.offsetPage=0;state.residualPage=0;await refresh()});
  action('#residual-form',async form=>{const data=formObject(form);state.feature=data.feature;state.minAbs=data.min_abs;state.residualPage=0;await refresh()});
  action('#readings-form',async form=>{state.sourceId=Number(formObject(form).source_id);state.readings=await calibrationApi.readings(state.version.id,state.sourceId);await refresh();toast('已核验 '+state.readings.total+' 条读数的修正结果')});
  bind('#pair-prev','click',async()=>{state.pairPage--;await refresh()});
  bind('#pair-next','click',async()=>{state.pairPage++;await refresh()});
  bind('#off-prev','click',async()=>{state.offsetPage--;await refresh()});
  bind('#off-next','click',async()=>{state.offsetPage++;await refresh()});
  bind('#res-prev','click',async()=>{state.residualPage--;await refresh()});
  bind('#res-next','click',async()=>{state.residualPage++;await refresh()});
}

export function calibrationReadings(){return state.readings}
