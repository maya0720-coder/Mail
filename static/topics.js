/* 토픽 그룹·일자별 RAG·참조 양식 보고서 화면. 원문 보기는 공통 app.js를 사용합니다. */
const topicCache=new Map(),openTopics=new Set(),reviewSelected=new Set();let mailLimit=25,worklogLoaded=false,mailCategory='all';
pageInfo.review=['확인할 토픽','같은 대화는 한 번만 표시합니다. 펼쳐서 메일별 요약과 업무 후보를 확인하세요.'];
pageInfo.mails=['토픽별 메일','최근 수신일순으로 정리된 메일을 토픽별로 살펴보세요.'];
pageInfo.worklog=['업무 히스토리','질문 하나로 관련 메일과 업무의 일자별 흐름을 찾아보세요.'];

function proposalCard(p){const v=JSON.parse(p.payload),summary=String(v.summary||v.evidence_quote||'').slice(0,900);return `<div class="nested-proposal"><span class="badge">${esc(names[p.action]||p.action)}</span><p class="candidate-summary">${esc(summary)}</p>${v.evidence_quote?`<details><summary>대표 원문 근거 보기</summary><blockquote class="evidence">${esc(v.evidence_quote)}</blockquote></details>`:''}<div class="proposal-footer"><span class="subtle">[업무에 반영]을 누르면 내 업무의 미처리 업무(확인완료)로 들어갑니다.</span><button data-reject="${p.id}">제외</button><button class="primary" data-accept="${p.id}">업무에 반영</button></div></div>`}
function topicCard(g,mode){const key=mode+':'+g.id;const check=mode==='review'?`<input type="checkbox" class="topic-check" data-topic-check="${esc(g.id)}" aria-label="${esc(g.title)} 선택" ${reviewSelected.has(g.id)?'checked':''}>`:'';return `<details class="topic-card panel" data-topic="${esc(g.id)}" data-mode="${mode}" ${openTopics.has(key)?'open':''}><summary>${check}<span class="topic-chevron">›</span><span class="topic-main"><b>${esc(g.title)}</b><span class="topic-meta">${esc(g.category)}${g.approval_status?` &middot; ${esc(g.approval_status)}`:''} <span>메일 ${g.message_count}건</span>${g.proposal_count?`<span>확인 후보 ${g.proposal_count}개</span>`:''}</span></span><time>${esc(fmt(g.latest_at))}<small>${esc(g.date_basis||"수신일")}</small></time></summary><div class="topic-children" data-topic-content="${esc(key)}">메일 요약을 불러오는 중입니다.</div></details>`}
function bindTopics(root){root.querySelectorAll('details[data-topic]').forEach(el=>{el.addEventListener('toggle',()=>{const key=el.dataset.mode+':'+el.dataset.topic;if(el.open){openTopics.add(key);loadTopic(el)}else openTopics.delete(key)});if(el.open)loadTopic(el)})}
async function loadTopic(el){const id=el.dataset.topic;const content=el.querySelector('.topic-children');try{let group=topicCache.get(id);if(!group){group=await api('/api/topic?id='+encodeURIComponent(id));topicCache.set(id,group)}const review=el.dataset.mode==='review';const main=group.messages.find(m=>m.direction==='inbound')||group.messages[0];content.innerHTML=group.messages.map(m=>{const heading=`<div class="topic-mail-heading"><b>${esc(m.subject)}</b><time>${esc(fmt(m.received_at||m.sent_at))} · ${esc(m.sender)}</time></div>`;if(m.id!==main.id)return `<article class="topic-mail topic-mail-meta">${heading}${source(m.id)}</article>`;return `<article class="topic-mail topic-mail-latest">${heading}<p class="mail-brief"><b>메일 요약</b><br>${esc(m.summary)}</p>${source(m.id)}${review&&m.proposals.length?`<details class="candidate-list"><summary>이 메일의 업무 후보 ${m.proposals.length}개 펼치기</summary>${m.proposals.map(proposalCard).join('')}</details>`:''}</article>`}).join('')}catch(e){content.textContent=e.message}}
function reviewGroups(){return (state.topics||[]).filter(g=>g.proposal_count>0&&!g.notice)}
function renderReview(){topicCache.clear();const groups=reviewGroups();const ids=new Set(groups.map(g=>g.id));for(const id of [...reviewSelected])if(!ids.has(id))reviewSelected.delete(id);$('n-review').textContent=groups.length;$('nav-review').textContent=groups.length;$('review-count').textContent=groups.length;$('review-list').innerHTML=groups.length?groups.slice(0,reviewLimit).map(g=>topicCard(g,'review')).join(''):empty('확인할 토픽이 없습니다','메일이 수집되면 같은 대화의 요약을 묶어서 보여줍니다.');$('review-shown').textContent=groups.length?`${Math.min(reviewLimit,groups.length)} / ${groups.length}개 토픽 표시`:'';$('review-more').hidden=groups.length<=reviewLimit;bindTopics($('review-list'));updateReviewSelection();const selected=$('worklog-topic').value;$('worklog-topic').innerHTML='<option value="">전체 토픽</option>'+(state.topics||[]).map(g=>`<option value="${esc(g.id)}">${esc(g.title)}</option>`).join('');$('worklog-topic').value=selected}
function renderMails(){const all=state.topics||[];const groups=all.filter(g=>mailCategory==='all'||(mailCategory==='work'&&g.category!=='전자결재')||(mailCategory==='approval'&&g.category==='전자결재')||(mailCategory==='approval-pending'&&g.approval_status==='진행 알림')||(mailCategory==='approval-done'&&g.approval_status==='결재 완료'));$('mail-list').innerHTML=groups.length?groups.slice(0,mailLimit).map(g=>topicCard(g,'mails')).join(''):empty('수집한 메일이 없습니다','최근 30일을 수집하거나 이전 기간을 지정하세요.');$('mail-more').hidden=groups.length<=mailLimit;bindTopics($('mail-list'))}
function renderWorklog(r){$('worklog-result').innerHTML=`<div class="rag-answer panel"><div class="rag-mode">${esc(r.mode)} <span>${esc(r.generation)}</span></div><h2>${esc(r.answer.summary)}</h2>${r.answer.facts.map(f=>`<p>${esc(f.text)} <span class="citation">${esc(f.citations.join(', '))}</span></p>`).join('')}${(r.answer.unknowns||[]).map(x=>`<small>${esc(x)}</small>`).join('<br>')}</div><p class="subtle">${esc(r.date_basis)} · 최대 ${r.limit}건 · 근거 ${r.evidence_count}건</p>${r.days.map(day=>`<div class="history-day"><h2>${esc(day.date)} <span>${day.items.length}건</span></h2><div class="panel">${day.items.map(item=>`<article class="timeline-row"><div class="event-head"><b>${esc(item.topic)}</b><small>${esc(fmt(item.time))}</small></div><p><span class="badge">${esc(item.kind)}</span> <span class="citation">${esc(item.citation)}</span></p><pre>${esc(item.quote)}</pre>${item.message_id?source(item.message_id):''}</article>`).join('')}</div></div>`).join('')}`}
async function searchWorklog(){notice('업무 히스토리의 근거를 검색하고 있습니다.');const r=await api('/api/history',{query:$('worklog-query').value,start:$('worklog-start').value,end:$('worklog-end').value,topic_id:$('worklog-topic').value});renderWorklog(r);worklogLoaded=true;notice('일자별 히스토리를 조회했습니다.')}
document.addEventListener('click',e=>{const b=e.target.closest('button');if(!b)return;if(b.dataset.tab==='worklog'&&!worklogLoaded)action(searchWorklog);if(b.dataset.pdf)action(async()=>{const response=await fetch('/api/report-file?id='+encodeURIComponent(b.dataset.pdf),{headers:{'X-App-Token':TOKEN}});if(!response.ok)throw Error((await response.json()).error);const url=URL.createObjectURL(await response.blob()),a=document.createElement('a');a.href=url;a.download=`주간보고_${b.dataset.pdf}.pdf`;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000)})});
$('mail-more').onclick=()=>{mailLimit+=25;renderMails()};document.addEventListener('click',e=>{const b=e.target.closest('[data-mail-category]');if(!b)return;mailCategory=b.dataset.mailCategory;document.querySelectorAll('[data-mail-category]').forEach(x=>x.classList.toggle('selected',x===b));mailLimit=25;renderMails()});$('worklog-form').onsubmit=e=>{e.preventDefault();action(searchWorklog)};$('worklog-all').onclick=()=>action(async()=>{$('worklog-query').value='';$('worklog-start').value='';$('worklog-end').value='';$('worklog-topic').value='';await searchWorklog()});
document.addEventListener('DOMContentLoaded',()=>{const id=location.hash.startsWith('#mail=')?decodeURIComponent(location.hash.slice(6)):null;if(id)action(async()=>{const m=await api('/api/message?id='+encodeURIComponent(id));$('detail-body').innerHTML=`<h2>${esc(m.subject)}</h2><p class="muted">${esc(m.sender)}</p><pre>${esc(m.body)}</pre>`;$('detail').showModal()})});

/* 확인함 일괄 처리: 토픽 단위로 체크해 대기 후보를 한 번에 반영하거나 제외합니다. */
function visibleReviewIds(){return reviewGroups().slice(0,reviewLimit).map(g=>g.id)}
function updateReviewSelection(){
  const visible=visibleReviewIds(),count=reviewSelected.size;
  $('review-selected').textContent=`선택 ${count}개`;
  $('review-bulk').hidden=!visible.length;
  $('review-bulk-accept').disabled=$('review-bulk-reject').disabled=!count;
  $('review-select-all').checked=!!visible.length&&visible.every(id=>reviewSelected.has(id));
  $('review-select-all').indeterminate=!!count&&!$('review-select-all').checked;
}
document.addEventListener('click',e=>{const box=e.target.closest('input[data-topic-check]');if(box)e.stopPropagation()},true);
document.addEventListener('change',e=>{
  const box=e.target.closest('input[data-topic-check]');
  if(box){if(box.checked)reviewSelected.add(box.dataset.topicCheck);else reviewSelected.delete(box.dataset.topicCheck);updateReviewSelection();return}
  if(e.target.id==='review-select-all'){
    const visible=visibleReviewIds();
    if(e.target.checked)visible.forEach(id=>reviewSelected.add(id));else visible.forEach(id=>reviewSelected.delete(id));
    document.querySelectorAll('input[data-topic-check]').forEach(x=>x.checked=reviewSelected.has(x.dataset.topicCheck));
    updateReviewSelection();
  }
});
function bulkResolve(accept){
  const ids=[...reviewSelected];if(!ids.length)return;
  if(!accept&&!confirm(`선택한 토픽 ${ids.length}개의 업무 후보를 모두 제외할까요?`))return;
  action(async()=>{
    const r=await api('/api/resolve/topics',{topic_ids:ids,accept});
    reviewSelected.clear();
    const failed=r.failed.length?` · 실패 ${r.failed.length}개 (${r.failed.slice(0,3).map(f=>f.title||f.topic_id).join(', ')}${r.failed.length>3?' 외':''}: ${r.failed[0].error})`:'';
    notice((accept?`토픽 ${r.done}개를 내 업무에 반영했습니다`:`토픽 ${r.done}개의 후보를 제외했습니다`)+failed,!!r.failed.length);
    await refresh();
  });
}
$('review-bulk-accept').onclick=()=>bulkResolve(true);
$('review-bulk-reject').onclick=()=>bulkResolve(false);
