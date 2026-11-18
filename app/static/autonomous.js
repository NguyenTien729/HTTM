/* All server/model text is rendered as text, never executable HTML. */
'use strict';
const $ = id => document.getElementById(id);
const stages = ['literature_search','literature_read','literature_synthesis','research_plan','data_collection','design','analysis','introduction','related_work','discussion','manuscript','verification'];
const roles = [
  ['Tài liệu & bằng chứng',[0,1,2]],['Câu hỏi & giả thuyết',[3]],['Thu thập dữ liệu',[4]],
  ['Phương pháp & phân tích',[5,6]],['Viết bài khoa học',[7,8,9,10]],['Kiểm chứng',[11]]
];
const labels = {queued:'Đang chờ xử lý',running:'AI đang xử lý',failed:'Cần xử lý lỗi',interrupted:'Bị gián đoạn',
  awaiting_research_approval:'Chờ duyệt câu hỏi',awaiting_design_approval:'Chờ duyệt thiết kế',
  awaiting_final_approval:'Chờ duyệt bản thảo',completed:'Đã hoàn tất',cancelled:'Đã dừng'};
let current = null, shownVersion = null, timer = null, busy = false, historyBusy = false;
function node(tag, text, cls) { const n = document.createElement(tag); if(text != null)n.textContent=text;if(cls)n.className=cls;return n; }
function card(title){const n=node('section',null,'card');if(title)n.append(node('h2',title));return n;}
function para(parent,text,cls){parent.append(node('p',text,cls));}
function details(parent,title,value){const d=node('details');d.append(node('summary',title),node('pre',typeof value==='string'?value:JSON.stringify(value,null,2)));parent.append(d);}
function table(parent,heads,rows){const wrap=node('div',null,'scroll'),t=node('table'),h=node('tr');heads.forEach(x=>h.append(node('th',x)));t.append(h);rows.forEach(values=>{const r=node('tr');values.forEach(v=>r.append(node('td',String(v??''))));t.append(r);});wrap.append(t);parent.append(wrap);}
function link(parent,text,path){const a=node('a',text);a.href=path;parent.append(a);return a;}
function button(parent,text,fn,cls){const b=node('button',text,cls);b.type='button';b.onclick=fn;parent.append(b);return b;}
function error(err){$('message').hidden=false;$('message').textContent=err.message||String(err);}
async function api(path,method='GET',body){const r=await fetch('/autonomous'+path,{method,headers:body?{'Content-Type':'application/json'}:{},body:body?JSON.stringify(body):undefined});if(!r.ok){let e;try{e=await r.json();}catch{e={detail:'Không kết nối được máy chủ.'};}throw new Error(typeof e.detail==='string'?e.detail:JSON.stringify(e.detail));}return r.json();}
async function loadHistory(){if(historyBusy)return;historyBusy=true;try{const runs=await api('/runs');$('history').replaceChildren();if(!runs.length)para($('history'),'Chưa có tiến trình.');runs.forEach(r=>{const b=button($('history'),'#'+r.id+' · '+(r.topic||'Project '+r.project_id),()=>openRun(r.id),'history-item');b.append(node('small',labels[r.status]||r.status));});}catch(e){error(e);}finally{historyBusy=false;}}
async function openRun(id){clearTimeout(timer);shownVersion=null;current={id:Number(id)};localStorage.setItem('httmAutonomousRun',id);window.history.replaceState(null,'','?run='+id);await poll();}
async function poll(){const id=current?.id;if(!id)return;try{const r=await api('/runs/'+id);if(current?.id!==id||(current.version&&r.version<current.version))return;current=r;if(shownVersion!==r.version||JSON.stringify(r.artifacts.collection_progress)!==window.__collectionProgress){window.__collectionProgress=JSON.stringify(r.artifacts.collection_progress);render(r);shownVersion=r.version;}}catch(e){error(e);}finally{if(current?.id===id)timer=setTimeout(poll,4000);}}
async function act(action,body={}){if(busy)return;busy=true;const id=current.id;try{$('message').hidden=true;const r=await api('/runs/'+id+'/'+action,'POST',{expected_version:current.version,...body});if(current.id===id){current=r;shownVersion=r.version;render(r);}loadHistory();}catch(e){error(e);shownVersion=null;}finally{busy=false;}}
function render(run){if(run.artifacts.settings?.paper_type==='review'){renderReview(run);return;}const w=$('workspace'),a=run.artifacts;const savedNotes=$('decision-notes')?.value||'';w.replaceChildren();
  const progress=card();const heading=node('div',null,'heading');heading.append(node('h2','Nghiên cứu #'+run.id),node('span',labels[run.status]||run.status,'badge'+(['failed','interrupted'].includes(run.status)?' error':'')));progress.append(heading);para(progress,a.topic);progress.append(node('small','Project '+run.project_id+' · Phiên bản kế hoạch '+run.revision));const timeline=node('div',null,'stage-list'),index=stages.indexOf(run.stage);roles.forEach(([name,indices])=>{const box=node('div',name,'stage'+(indices.includes(index)?' current':Math.max(...indices)<index||run.status==='completed'?' done':''));box.append(node('small',Math.max(...indices)<index||run.status==='completed'?'Đã xử lý':indices.includes(index)?'Bước hiện tại':'Sắp tới'));timeline.append(box);});progress.append(timeline);if(run.stage==='data_collection'&&a.collection_progress){const cp=a.collection_progress;para(progress,cp.message);para(progress,'T?m bi?n: '+cp.searches_done+'/'+cp.searches_total+' ? Ki?m tra ngu?n: '+cp.sources_done+'/'+cp.sources_total+' ? ?? l?u: '+cp.sources_saved+' ngu?n ? '+cp.elapsed_seconds+' gi?y');}if(run.error)progress.append(node('div',run.error,'error-box'));const actions=node('div',null,'actions');if(['failed','interrupted'].includes(run.status))button(actions,'Tiếp tục từ bước bị gián đoạn',()=>act('resume'),'primary');if(!['completed','cancelled'].includes(run.status))button(actions,'Dừng tiến trình',()=>act('cancel'));progress.append(actions);w.append(progress);
  if(a.research_plan){const b=a.research_plan,c=card('Câu hỏi & kế hoạch dữ liệu');para(c,b.question,'pre');para(c,b.gap);para(c,'Địa bàn: '+b.geography+' · Thời gian: '+b.time_scope);if(b.hypotheses.length){c.append(node('h3','Giả thuyết dự kiến'));b.hypotheses.forEach(h=>para(c,h.label+': '+h.statement));}table(c,['Biến cần đo','Vai trò','Đơn vị','Độ phân giải'],b.data_needs.map(n=>[n.concept,n.role,n.unit,n.resolution]));para(c,b.feasibility_notes);details(c,'Bằng chứng & hồ sơ đầy đủ',b);w.append(c);}
  if(a.data_collection){const c=card('Nguồn dữ liệu đã kiểm tra'),sources=a.data_collection.sources;if(!sources.length)para(c,'Chưa tìm được nguồn có dữ liệu bảng phù hợp. Có thể yêu cầu tìm lại hoặc điều chỉnh câu hỏi.');sources.forEach(s=>{const box=node('div',null,'source');const candidate=s.candidate;box.append(node('h3',s.source_key+' · '+candidate.title));try{const url=new URL(candidate.dataset_url);if(url.protocol==='https:'){const l=link(box,'Mở nguồn dữ liệu',url.href);l.target='_blank';l.rel='noopener noreferrer';}}catch{}para(box,s.rows.toLocaleString('vi-VN')+' dòng snapshot · '+s.columns.length+' cột · Giấy phép: '+(candidate.license||'Chưa xác nhận'));if(candidate.relevance_note)para(box,candidate.relevance_note,'warning');details(box,'Các cột và giá trị mẫu',{columns:s.columns,sample:s.sample});details(box,'Nguồn gốc & kiểm tra toàn vẹn',{metadata:s.metadata,sha256:s.sha256});c.append(box);});if(a.data_collection.search_coverage?.length)table(c,['Biến cần tìm','Bắt buộc','Ứng viên metadata','Lượt tìm'],a.data_collection.search_coverage.map(x=>[x.concept,x.required?'Có':'Không',x.candidate_count,x.search_rounds.join(', ')||'Chưa có lượt tìm hoàn tất']));details(c,'Nhật ký tìm kiếm theo từng biến',{coverage:a.data_collection.search_coverage,searches:a.data_collection.searches,rejected_previous_sources:a.data_collection.rejected_previous_sources,errors:a.data_collection.errors,guidance:a.data_collection.search_guidance});w.append(c);}
  if(a.design){const d=a.design,v=a.design_validation||{},c=card(d.adequate?'Phương án phân tích':'Chưa có phương án phân tích khả thi');para(c,d.rationale);para(c,'Phương pháp: '+d.method+' · Biến kết quả: '+(d.target||'Chưa xác định'));if(d.predictors.length)para(c,'Biến giải thích: '+d.predictors.join(', '));if(d.missing_requirements.length)c.append(node('div','Chưa đáp ứng: '+d.missing_requirements.join('; '),'warning'));if(v.coverage)table(c,['Biến cần có','Vai trò','Đơn vị','Độ phân giải','Nguồn/cột đã đối chiếu'],v.coverage.map(x=>[x.concept+' ('+x.key+')'+(x.required?' *':''),x.role,x.unit,x.resolution,x.sources.map(s=>s.source_key+': '+s.column).join(', ')||'Chưa tìm thấy']));if(d.adequate){d.sources.forEach(s=>{c.append(node('h3','Ánh xạ nguồn '+s.source_key));table(c,['Biến','Cột gốc','Đổi đơn vị','Lý do'],s.bindings.map(b=>[b.variable,b.column,b.source_unit+' → '+b.target_unit+'; '+b.transform+'; ×'+b.scale+' +'+b.offset,b.rationale]));});details(c,'Điều kiện lọc mẫu theo địa bàn và thời gian',d.sources.map(s=>({source:s.source_key,filters:s.filters||[]})));details(c,'Khóa ghép, giả định và giới hạn',d);}else if(v.candidate_mappings?.length)details(c,'Ánh xạ thử (chưa được chấp nhận)',v.candidate_mappings);if(v.next_actions?.length){c.append(node('h3','Bước tiếp theo'));v.next_actions.forEach(x=>para(c,'• '+x));}details(c,'Kiểm tra bằng mã chương trình',v);w.append(c);}  if(run.status.startsWith('awaiting_')){const c=card('Quyết định của bạn');const label=node('label','Ghi chú hoặc yêu cầu điều chỉnh'),notes=node('textarea');notes.id='decision-notes';notes.rows=3;notes.value=savedNotes;label.append(notes);c.append(label);const select=node('select');const options=run.status==='awaiting_research_approval'?[['research_plan','Lập lại câu hỏi & yêu cầu dữ liệu']]:run.status==='awaiting_design_approval'?[['data_collection','Tìm và kiểm tra lại nguồn'],['design','Đề xuất lại phương pháp với dữ liệu hiện có'],['research_plan','Điều chỉnh câu hỏi nghiên cứu']]:[['introduction','Viết và kiểm chứng lại bản thảo'],['design','Điều chỉnh phương pháp'],['research_plan','Điều chỉnh câu hỏi nghiên cứu']];options.forEach(([value,text])=>{const o=node('option',text);o.value=value;select.append(o);});const sl=node('label','Khi yêu cầu điều chỉnh, quay lại');sl.append(select);c.append(sl);const row=node('div',null,'actions');const b=button(row,'Phê duyệt & tiếp tục',()=>act('decision',{action:'approve',notes:notes.value}),'primary');b.disabled=(run.status==='awaiting_design_approval'&&!a.design_validation?.ok)||(run.status==='awaiting_final_approval'&&!a.verification?.ok);button(row,'Yêu cầu điều chỉnh',()=>act('decision',{action:'revise',notes:notes.value,restart_from:select.value}));c.append(row);if(b.disabled)c.append(node('div','Còn yêu cầu dữ liệu hoặc lỗi kiểm chứng chưa được giải quyết. Hãy xem báo cáo và yêu cầu điều chỉnh.','warning'));w.append(c);}
  if(a.analysis){const c=card('Kết quả tính từ dữ liệu');para(c,a.analysis.results_draft,'pre');const sums=a.analysis.column_summaries||{};table(c,['Biến','N','Trung bình','Độ lệch chuẩn'],Object.entries(sums).filter(([,s])=>s.kind==='numeric').map(([name,s])=>[name,s.n,s.mean,s.std]));(a.analysis.charts||[]).filter(ch=>ch.type==='histogram').slice(0,4).forEach(ch=>histogram(c,ch));details(c,'Kết quả, nguồn và phép phân tích đầy đủ',a.analysis);w.append(c);}
  if(a.verification){const c=card('Kiểm chứng khoa học');para(c,a.verification.ok?'Không phát hiện lỗi chặn trong lần kiểm tra tự động này.':'Cần sửa các vấn đề trước khi duyệt.');table(c,['Mức độ','Phần','Tuyên bố','Vấn đề'],a.verification.issues.map(i=>[i.severity,i.section,i.claim,i.reason]));details(c,'Báo cáo đầy đủ',a.verification);w.append(c);}
  if(a.manuscript){const d=a.manuscript,c=card();c.classList.add('manuscript');c.append(node('h2',d.title));para(c,d.authors.map(x=>x.name).join(', ')||'Chưa cung cấp tác giả');c.append(node('h3','Abstract'));para(c,d.abstract,'pre');para(c,'Keywords: '+d.keywords.join(', '));Object.entries(d.sections).forEach(([name,text])=>{c.append(node('h3',name));para(c,text,'pre');});c.append(node('h3','References'));d.references.forEach(r=>para(c,'['+r.id+'] '+r.authors.join('; ')+' ('+(r.year||'n.d.')+'). '+r.title+'. '+(r.doi||'')));const links=node('div',null,'links');link(links,'Tải bản thảo Markdown','/autonomous/runs/'+run.id+'/manuscript.md');if(run.status==='completed')link(links,'Tải PDF đã duyệt','/autonomous/runs/'+run.id+'/export.pdf');c.append(links);w.append(c);}
  const log=card('Nhật ký & quyết định');const list=node('ul',null,'log');[...run.events].reverse().forEach(e=>{const li=node('li');const time=node('time',new Date(e.at).toLocaleString('vi-VN'));li.append(time,document.createTextNode(e.message));list.append(li);});log.append(list);link(log,'Tải hồ sơ kiểm toán JSON','/autonomous/runs/'+run.id+'/audit.json');w.append(log);
}
function histogram(parent,ch){const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');svg.setAttribute('viewBox','0 0 480 180');svg.setAttribute('role','img');svg.setAttribute('aria-label','Phân phối '+ch.column);svg.classList.add('chart');const max=Math.max(1,...ch.counts),width=430/Math.max(1,ch.counts.length);ch.counts.forEach((count,i)=>{const rect=document.createElementNS(svg.namespaceURI,'rect');const height=count/max*120;for(const [k,v]of Object.entries({x:30+i*width,y:145-height,width:Math.max(1,width-2),height,fill:'#31765b'}))rect.setAttribute(k,v);const title=document.createElementNS(svg.namespaceURI,'title');title.textContent=ch.edges[i]+'–'+ch.edges[i+1]+': '+count;rect.append(title);svg.append(rect);});parent.append(node('h3','Phân phối '+ch.column),svg);}
$('start-form').onsubmit=async e=>{e.preventDefault();if(busy)return;busy=true;const b=e.target.querySelector('button[type=submit]');b.disabled=true;try{$('message').hidden=true;const project=$('project').value;const body={paper_type:$('paper-type').value,target_paper_count:Number($('papers').value),max_sources:Number($('sources').value),max_rows:Number($('rows').value),authors:$('authors').value.split('\n').filter(x=>x.trim()).map(line=>{const [name,affiliation='',email='']=line.split('|').map(x=>x.trim());return {name,affiliation,email};})};if(project)body.project_id=Number(project);else{body.topic=$('topic').value.trim();body.region=$('region').value.trim()||null;}const r=await api('/runs','POST',body);busy=false;await openRun(r.id);loadHistory();}catch(err){error(err);}finally{busy=false;b.disabled=false;}};
$('project').oninput=()=>{$('topic').required=!$('project').value;};
$('refresh').onclick=loadHistory;
const renderWithProposal = render;
function renderReview(run) {
  const w=$('workspace'), a=run.artifacts, saved=$('decision-notes')?.value||'';
  w.replaceChildren();
  const progress=card('Nghiên cứu tài liệu #'+run.id);
  para(progress,a.topic); para(progress,labels[run.status]||run.status);
  const steps=[['literature_search','Tìm tài liệu'],['literature_read','Đọc và trích xuất'],['literature_synthesis','Tổng hợp chủ đề'],['research_plan','Duyệt phạm vi'],['manuscript','Viết bài tổng quan'],['verification','Kiểm chứng và tự sửa']];
  const timeline=node('div',null,'stage-list'), index=steps.findIndex(s=>s[0]===run.stage);
  steps.forEach(([key,name],i)=>timeline.append(node('div',name,'stage'+(run.status==='completed'||i<index?' done':i===index?' current':''))));
  progress.append(timeline);
  if(run.error)para(progress,run.error,'error-box');
  if(['failed','interrupted'].includes(run.status))button(progress,'Tiếp tục bước đang dở',()=>act('resume'));
  if(!['completed','cancelled'].includes(run.status))button(progress,'Dừng tiến trình',()=>act('cancel'));
  w.append(progress);
  if(a.review_corpus){const c=card('Tập bằng chứng được sử dụng');
    table(c,['Nguồn','Năm','Công trình','Phương pháp','Phát hiện','Hạn chế'],a.review_corpus.papers.map(p=>['['+p.id+']',p.year,p.title,p.method,p.key_finding,p.limitation]));
    details(c,'Nhật ký tìm kiếm',a.review_corpus.search_audit);details(c,'Trích đoạn nguồn',a.review_corpus.evidence_sources);w.append(c);}
  if(a.research_plan){const p=a.research_plan,c=card('Phạm vi bài tổng quan');para(c,p.question);para(c,p.gap);para(c,p.geography+' · '+p.time_scope);para(c,p.feasibility_notes);w.append(c);}
  if(a.manuscript){const d=a.manuscript,c=card(d.title);para(c,d.abstract,'pre');
    const order=d.section_order||a.review_document.sections.filter(s=>s.key!=='abstract').map(s=>s.title);
    order.forEach(name=>{c.append(node('h3',name));para(c,d.sections[name],'pre');});
    c.append(node('h3','Tài liệu tham khảo'));d.references.forEach(p=>para(c,'['+p.id+'] '+p.authors.join('; ')+' ('+(p.year||'n.d.')+'). '+p.title+'. '+(p.doi||'')));
    const links=node('div',null,'links');link(links,'Tải bản thảo Markdown','/autonomous/runs/'+run.id+'/manuscript.md');
    if(run.status==='completed'){link(links,'Tải PDF','/autonomous/runs/'+run.id+'/export.pdf');link(links,'Tải Word','/autonomous/runs/'+run.id+'/export.docx');}c.append(links);w.append(c);}
  if(a.verification){const c=card('Kiểm chứng và phản biện bằng chứng');para(c,a.verification.ok?'Đã qua kiểm tra tự động; chờ người nghiên cứu duyệt.':'Còn nội dung cần điều chỉnh.');
    table(c,['Phần','Vấn đề'],a.verification.issues.map(x=>[x.section,x.reason]));details(c,'Liên kết bằng chứng, kiểm chứng nội dung và lịch sử tự sửa',a.verification);w.append(c);}
  if(run.status.startsWith('awaiting_')){const c=card('Quyết định của bạn'),notes=node('textarea');notes.id='decision-notes';notes.value=saved;notes.rows=3;notes.placeholder='Ghi yêu cầu chỉnh sửa nếu cần';c.append(notes);
    const approve=button(c,'Phê duyệt và tiếp tục',()=>act('decision',{action:'approve',notes:notes.value}),'primary');approve.disabled=run.status==='awaiting_final_approval'&&!a.verification?.ok;
    if(run.status==='awaiting_final_approval')button(c,'Sửa bằng chứng và kiểm chứng lại',()=>act('decision',{action:'revise',notes:notes.value.trim()||'Sửa liên kết bằng chứng và kiểm chứng lại bản thảo hiện tại.',restart_from:'verification'}));
    button(c,'Yêu cầu chỉnh sửa',()=>act('decision',{action:'revise',notes:notes.value,restart_from:run.status==='awaiting_research_approval'?'research_plan':'manuscript'}));w.append(c);}
  const log=card('Nhật ký nghiên cứu');[...run.events].reverse().forEach(e=>para(log,new Date(e.at).toLocaleString('vi-VN')+' · '+e.message));link(log,'Tải hồ sơ bằng chứng và kiểm toán','/autonomous/runs/'+run.id+'/audit.json');w.append(log);
}
render = function(run){renderWithProposal(run);const d=run.artifacts.proposal_draft;if(!d)return;const c=card('Bản đề xuất nghiên cứu · chưa có kết quả thực nghiệm');para(c,d.disclaimer,'warning');para(c,d.abstract,'pre');if(d.blockers?.length){c.append(node('h3','Blockers cần giải quyết trước bài thực nghiệm'));d.blockers.forEach(x=>para(c,'• '+x,'warning'));}Object.entries(d.sections||{}).forEach(([name,text])=>{c.append(node('h3',name));para(c,text,'pre');});details(c,'Paper story',d.paper_story);details(c,'Claim–evidence map',d.claim_evidence_map);link(c,'Tải bản đề xuất Markdown','/autonomous/runs/'+run.id+'/proposal.md');$('workspace').append(c);};
const proposalEdits = new Map();
const renderWithProposalContinuation = render;
render = function(run) {
  renderWithProposalContinuation(run);
  if (!run.artifacts.proposal_draft) return;
  let edit = proposalEdits.get(run.id);
  if (!edit) {
    edit = {text: '', pending: false, message: ''};
    proposalEdits.set(run.id, edit);
  }
  const c = card('Viết tiếp bản đề xuất');
  para(c, 'Bạn có thể mở rộng bản đề xuất trong khi bổ sung dữ liệu cho phần thực nghiệm.');
  const label = node('label', 'Yêu cầu chỉnh sửa (không bắt buộc)');
  const instruction = node('textarea');
  instruction.rows = 3;
  instruction.value = edit.text;
  instruction.placeholder = 'Để trống để mở rộng tổng quan và giải thích rõ phương pháp dự kiến.';
  instruction.disabled = edit.pending;
  instruction.oninput = () => { edit.text = instruction.value; };
  label.append(instruction);
  c.append(label);
  const notice = node('p', edit.message);
  notice.setAttribute('role', 'status');
  notice.setAttribute('aria-live', 'polite');
  const submit = button(c, edit.pending ? 'Đang viết…' : 'Viết tiếp bản đề xuất', async () => {
    if (edit.pending) return;
    if (busy) {
      notice.textContent = 'Một yêu cầu khác đang xử lý. Hãy đợi hoàn tất rồi thử lại.';
      return;
    }
    const text = instruction.value.trim() ||
      'Mở rộng phần tổng quan tài liệu và giải thích chi tiết phương pháp dự kiến dựa trên bằng chứng hiện có. Không rút gọn bản thảo, không tạo kết quả thực nghiệm.';
    edit.text = text;
    instruction.value = text;
    edit.pending = true;
    busy = true;
    edit.message = 'Đang viết bản mới. Nội dung sẽ cập nhật khi máy chủ xử lý xong.';
    notice.textContent = edit.message;
    submit.textContent = 'Đang viết…';
    submit.disabled = true;
    instruction.disabled = true;
    $('message').hidden = true;
    try {
      const result = await api('/runs/' + run.id + '/proposal/continue', 'POST', {
        expected_version: run.version, instruction: text
      });
      edit.message = 'Đã lưu bản đề xuất mới. Bạn có thể đọc phần nội dung phía trên hoặc tải lại bản Markdown.';
      if (current?.id === run.id) {
        current = result;
        shownVersion = result.version;
      }
      loadHistory();
    } catch (err) {
      edit.message = 'Chưa viết tiếp được: ' + (err.message || String(err));
    } finally {
      edit.pending = false;
      busy = false;
      if (current?.id === run.id) render(current);
    }
  }, 'primary');
  submit.disabled = edit.pending;
  c.append(notice);
  $('workspace').append(c);
};
const renderWithProposalError = render;
render = function(run){renderWithProposalError(run);const reason=run.artifacts.proposal_draft_error;if(!reason)return;const c=card('Bản đề xuất chưa tạo được');para(c,'Bước viết đề xuất gặp lỗi '+reason+'. Hồ sơ phân tích và lý do chặn vẫn nằm trong báo cáo bên trên.','warning');$('workspace').append(c);};
const renderWithEvidence = render;
render = function(run){renderWithEvidence(run);const a=run.artifacts;if(a.proposal_draft||(!a.paper_story&&!a.claim_evidence_map&&!a.paper_state))return;const c=card('Story và bằng chứng của bản thảo');if(a.paper_state)details(c,'Trạng thái bài và blockers',a.paper_state);if(a.paper_story)details(c,'Paper story',a.paper_story);if(a.claim_evidence_map)details(c,'Claim–evidence map',a.claim_evidence_map);$('workspace').append(c);};
loadHistory();
const initial=new URLSearchParams(location.search).get('run')||localStorage.getItem('httmAutonomousRun');if(initial&&/^\d+$/.test(initial))openRun(initial);
