/* 내 업무: 처리 유형(일반업무/전자결재)과 처리 단계로만 관리합니다. 메뉴 안내 토스트도 이 파일에 있습니다. */
let taskRequest = 0, taskFilterTimer, menuToastTimer;
const openTasks = new Set();

const MENU_HELP = {
  review: '같은 토픽의 메일 요약과 업무 후보를 펼쳐 보고 [업무에 반영]하거나 제외합니다.',
  tasks: '일반업무는 미확인 → 확인완료 → 진행중 → 처리완료, 전자결재는 미처리 → 처리완료 순서로 관리합니다.',
  mails: '최근 수신일순으로 묶인 토픽을 펼쳐 메일 요약과 원문을 확인합니다.',
  search: '수집된 메일의 단어와 관련 스레드를 찾아 원문 근거를 확인합니다.',
  worklog: 'RAG로 관련 근거를 찾고 메일·업무 변경을 일자별 타임라인으로 확인합니다.',
  reports: '내 업무에서 확정된 업무로 금주·차주 보고서를 만들고, 항목별로 수정·삭제한 뒤 PDF로 받습니다.',
  history: '상태 변경·완료·삭제·복원의 전후 값과 근거를 확인합니다.',
  runs: '메일 수집·분석·검색·보고의 실행 결과와 오류를 확인합니다.',
};

const STAGE_LABELS = {
  일반업무: {미확인: '미확인 업무', 확인완료: '미처리 업무(확인완료)', 진행중: '진행중', 처리완료: '처리완료'},
  전자결재: {미처리: '미처리 업무', 처리완료: '처리완료'},
};
const ACTION_LABELS = {confirm: '확인', start: '진행', complete: '처리 완료', reopen: '재진행'};
const ACTION_NOTICES = {
  confirm: '확인완료(미처리) 업무로 옮겼습니다.',
  start: '진행중으로 변경했습니다.',
  complete: '처리완료로 변경하고 처리 시각을 기록했습니다.',
  reopen: '다시 진행하도록 되돌렸습니다. 이전 처리 이력은 유지됩니다.',
};

function showMenuToast(name) {
  const toast = $('menu-toast');
  clearTimeout(menuToastTimer);
  toast.textContent = MENU_HELP[name] || '';
  toast.classList.add('show');
  toast.setAttribute('aria-hidden', 'false');
  menuToastTimer = setTimeout(() => {
    toast.classList.remove('show');
    toast.setAttribute('aria-hidden', 'true');
  }, 4200);
}

function stageLabel(kind, stage) {
  return (STAGE_LABELS[kind] || {})[stage] || stage;
}

function taskDateText(task, trash) {
  if (trash) return '삭제 ' + fmt(task.deleted_at);
  if (task.work_stage === '처리완료') return '처리 ' + (task.completed_at ? fmt(task.completed_at) : '시각 미확인');
  return '기한 ' + (task.due_date || '미정');
}

function taskRow(task, trash) {
  const done = task.work_stage === '처리완료';
  const actions = trash
    ? `<button data-task-restore="${esc(task.id)}">업무 복원</button>`
    : (task.actions || []).map(action =>
        `<button class="${action === 'reopen' ? '' : 'primary'}" data-task-action="${esc(action)}" data-task-id="${esc(task.id)}">${esc(ACTION_LABELS[action] || action)}</button>`).join('');
  const reply = task.needs_reply && !trash ? `<button data-reply-task="${esc(task.id)}">회신 초안</button>` : '';
  const edit = trash
    ? '<p class="subtle">삭제 전 상태와 처리 시각을 보존합니다. 복원하면 일반 목록에 표시됩니다.</p>'
    : `<details class="task-edit"><summary>기한 수정 및 삭제</summary><div class="task-controls">
         <label>기한<input id="due-${esc(task.id)}" type="date" value="${esc(task.due_date || '')}"></label>
         <button data-save="${esc(task.id)}">기한 저장</button>
         <button class="danger" data-task-delete="${esc(task.id)}">휴지통으로 이동</button></div></details>`;
  return `<details class="task-item ${done ? 'is-completed' : ''}" data-task-item="${esc(task.id)}" ${openTasks.has(String(task.id)) ? 'open' : ''}>
    <summary><span class="topic-chevron">›</span>
      <span class="task-title"><b>${esc(task.display_title || task.title)}</b><small>${esc(task.assignee || '담당 미정')}</small></span>
      <span class="task-state ${done ? 'done' : ''}">${esc(stageLabel(task.work_kind, task.work_stage))}</span>
      <span class="task-date">${esc(taskDateText(task, trash))}</span>
    </summary>
    <div class="task-expanded">
      <div class="task-primary-actions">${actions}${reply}${source(task.source_id)}</div>
      ${task.summary && !trash ? `<p class="task-summary">${esc(task.summary)}</p>` : ''}
      ${edit}
    </div></details>`;
}

// 같은 메일 대화의 업무는 한 묶음으로 보여줍니다. 과제(IFRS18·SAP 등) 구분은 표시하지 않습니다.
function topicGroups(tasks, trash) {
  const groups = new Map();
  for (const task of tasks) {
    const key = task.topic_id || 'task-' + task.id;
    if (!groups.has(key)) {
      groups.set(key, {title: task.topic_title || task.title || '제목 없는 업무', messageCount: task.topic_message_count || 1,
                       summary: task.topic_summary || task.summary || '', threadMessages: task.thread_messages || [], tasks: []});
    }
    groups.get(key).tasks.push(task);
  }
  return [...groups].map(([key, group]) => {
    if (group.tasks.length === 1 && group.threadMessages.length <= 1) return taskRow(group.tasks[0], trash);
    const thread = group.threadMessages.length > 1
      ? `<details class="task-thread-meta"><summary>연결된 메일 ${group.threadMessages.length}건</summary>${group.threadMessages.map(mail =>
          `<article><b>${esc(mail.subject)}</b><small>${esc(fmt(mail.received_at || mail.sent_at))} &middot; ${esc(mail.sender)}</small>${source(mail.id)}</article>`).join('')}</details>`
      : '';
    return `<details class="task-topic-group" data-task-topic="${esc(key)}" open>
      <summary><b>${esc(group.title)}</b><small>메일 ${group.messageCount}건 &middot; 업무 ${group.tasks.length}건</small></summary>
      <div class="task-topic-items">${thread}${group.tasks.map(task => taskRow(task, trash)).join('')}</div></details>`;
  }).join('');
}

// 처리 유형·단계 묶음은 펼치거나 접을 수 있고, 접은 상태는 이 브라우저에 기억합니다.
const STAGE_NUMBERS = ['①', '②', '③', '④'];
const closedSections = new Set((() => {
  try { return JSON.parse(localStorage.getItem('mailbrain.closedSections') || '[]'); } catch (error) { return []; }
})());

function saveClosedSections() {
  try { localStorage.setItem('mailbrain.closedSections', JSON.stringify([...closedSections])); } catch (error) { /* 저장소를 쓸 수 없어도 화면은 동작합니다. */ }
}

function stageSections(tasks, stages, {kindFilter = '', showEmpty = false} = {}) {
  return Object.entries(stages).map(([kind, kindStages]) => {
    if (kindFilter && kindFilter !== kind) return '';
    const ofKind = tasks.filter(task => task.work_kind === kind);
    if (!ofKind.length && !showEmpty) return '';
    const sections = kindStages.map((stage, index) => {
      const items = ofKind.filter(task => task.work_stage === stage);
      if (!items.length && !showEmpty) return '';
      const key = 'stage:' + kind + ':' + stage;
      return `<details class="task-stage" data-task-stage="${esc(kind + ':' + stage)}" data-section="${esc(key)}" ${closedSections.has(key) ? '' : 'open'}>
        <summary class="task-stage-heading"><span class="topic-chevron">›</span>${esc(STAGE_NUMBERS[index] + ' ' + stageLabel(kind, stage))} <span data-task-count>${items.length}</span></summary>
        ${items.length ? topicGroups(items, false) : '<p class="subtle task-stage-empty">해당 업무가 없습니다.</p>'}</details>`;
    }).join('');
    const key = 'kind:' + kind;
    return `<details class="task-kind" data-task-kind="${esc(kind)}" data-section="${esc(key)}" ${closedSections.has(key) ? '' : 'open'}>
      <summary class="task-group-title"><span class="topic-chevron">›</span>${esc(kind)} <span data-task-count>${ofKind.length}</span></summary>${sections}</details>`;
  }).join('');
}

function stageOptions(stages, kind, selected) {
  const options = [];
  for (const [name, list] of Object.entries(stages)) {
    if (kind && kind !== name) continue;
    for (const stage of list) {
      const value = name + ':' + stage;
      options.push(`<option value="${esc(value)}" ${value === selected ? 'selected' : ''}>${esc(name + ' · ' + stageLabel(name, stage))}</option>`);
    }
  }
  const groups = Object.entries(GROUP_LABELS).map(([value, label]) =>
    `<option value="${esc(value)}" ${value === selected ? 'selected' : ''}>${esc(label)}</option>`).join('');
  return '<option value="">전체 단계</option>' + groups + options.join('');
}

// 상단 요약의 [진행할 업무]·[완료한 업무]를 누르면 쓰는 묶음 필터입니다.
const GROUP_LABELS = {'group:open': '진행할 업무 전체(처리완료 전)', 'group:done': '완료한 업무 전체(처리완료)'};

let selectedStage = '';

// 상단 요약 숫자를 누르면 해당 화면으로 이동해 대상만 걸러 보여줍니다.
function openSummary(target) {
  if (target === 'mail') {
    mailCategory = 'all';
    document.querySelectorAll('[data-mail-category]').forEach(x => x.classList.toggle('selected', x.dataset.mailCategory === 'all'));
    selectTab('mails');
    renderMails();
    notice('수집한 메일 전체를 표시합니다.');
  } else if (target === 'review') {
    selectTab('review');
    notice('처리할 업무 후보가 있는 토픽만 표시합니다.');
  } else {
    selectedStage = 'group:' + target;
    for (const id of ['task-kind', 'task-query', 'task-completed-start', 'task-completed-end']) $(id).value = '';
    $('task-scope').value = 'active';
    selectTab('tasks');
    action(renderTasks);
    notice(`${GROUP_LABELS[selectedStage]} 필터를 적용했습니다. [필터 해제]로 전체 업무를 다시 볼 수 있습니다.`);
  }
}

async function renderTasks() {
  const request = ++taskRequest;
  const kind = $('task-kind').value;
  const grouped = selectedStage.startsWith('group:');
  if (kind && selectedStage && !grouped && !selectedStage.startsWith(kind + ':')) selectedStage = '';
  const [stageKind, stage] = grouped ? ['', ''] : (selectedStage || ':').split(':');
  const params = new URLSearchParams({
    query: $('task-query').value, scope: $('task-scope').value,
    start: $('task-completed-start').value, end: $('task-completed-end').value,
    kind: kind || stageKind || '', stage: stage || '', group: grouped ? selectedStage.slice(6) : '',
  });
  try {
    const result = await api('/api/tasks?' + params);
    if (request !== taskRequest) return;
    const trash = $('task-scope').value === 'deleted';
    $('filter').innerHTML = stageOptions(result.stages, kind, selectedStage);
    const filterLabel = GROUP_LABELS[selectedStage] || (selectedStage ? selectedStage.replace(':', ' · ') : '');
    $('task-result-count').innerHTML = `선택 조건 ${result.filtered_count}건` + (filterLabel
      ? ` <span class="active-filter">${esc(filterLabel)} <button class="link-button" data-clear-stage>필터 해제</button></span>` : '');
    $('task-status-summary').innerHTML = Object.entries(result.stage_counts).map(([name, counts]) =>
      `<div class="task-stage-chips"><b>${esc(name)}</b>${Object.entries(counts).map(([key, count]) => {
        const value = name + ':' + key;
        return `<button data-task-status="${esc(value)}" class="${selectedStage === value ? 'selected' : ''}">${esc(stageLabel(name, key))} <b>${count}</b></button>`;
      }).join('')}</div>`).join('');
    $('task-list').innerHTML = result.tasks.length
      ? (trash ? topicGroups(result.tasks, true) : stageSections(result.tasks, result.stages, {kindFilter: kind, showEmpty: !selectedStage}))
      : empty(trash ? '조건에 맞는 삭제 업무가 없습니다' : '선택 조건에 맞는 업무가 없습니다',
              trash ? '삭제한 업무와 변경 이력은 보존되며 이곳에서 복원할 수 있습니다.' : '처리 유형·단계·검색어를 바꾸거나 필터를 초기화하세요.');
    $('task-list').querySelectorAll('[data-task-item]').forEach(element => element.addEventListener('toggle', () => {
      if (element.open) openTasks.add(element.dataset.taskItem);
      else openTasks.delete(element.dataset.taskItem);
    }));
    $('task-list').querySelectorAll('[data-section]').forEach(element => element.addEventListener('toggle', () => {
      if (element.open) closedSections.delete(element.dataset.section);
      else closedSections.add(element.dataset.section);
      saveClosedSections();
    }));
    const advanced = [$('task-completed-start').value, $('task-completed-end').value, trash].filter(Boolean).length;
    $('advanced-filter-count').textContent = advanced ? `${advanced}개 적용` : '';
  } catch (error) {
    if (request !== taskRequest) return;
    $('task-list').innerHTML = empty('필터를 확인하세요', error.message);
    $('task-result-count').textContent = '';
    notice(error.message, true);
  }
}

for (const id of ['task-kind', 'task-scope', 'task-completed-start', 'task-completed-end']) $(id).onchange = () => action(renderTasks);
$('filter').onchange = () => {
  selectedStage = $('filter').value;
  action(renderTasks);
};
$('task-query').oninput = () => {
  clearTimeout(taskFilterTimer);
  taskFilterTimer = setTimeout(renderTasks, 220);
};
$('task-filter-reset').onclick = () => action(async () => {
  for (const id of ['task-kind', 'task-query', 'task-completed-start', 'task-completed-end']) $(id).value = '';
  selectedStage = '';
  $('task-scope').value = 'active';
  await renderTasks();
});

document.addEventListener('click', event => {
  const button = event.target.closest('button');
  if (!button) return;
  if (button.dataset.tab) showMenuToast(button.dataset.tab);
  if (button.dataset.summary) {
    openSummary(button.dataset.summary);
    return;
  }
  if (button.dataset.clearStage !== undefined) {
    selectedStage = '';
    action(renderTasks);
    return;
  }
  if (button.dataset.taskStatus) {
    selectedStage = selectedStage === button.dataset.taskStatus ? '' : button.dataset.taskStatus;
    $('task-kind').value = '';
    action(renderTasks);
    return;
  }
  if (button.dataset.taskAction) {
    const id = button.dataset.taskId, operation = button.dataset.taskAction;
    action(async () => {
      await api('/api/task/action', {id, action: operation});
      openTasks.add(String(id));
      notice(ACTION_NOTICES[operation] || '업무 상태를 변경했습니다.');
      await refresh();
    });
    return;
  }
  const operation = button.dataset.taskDelete ? 'delete' : button.dataset.taskRestore ? 'restore' : null;
  if (!operation) return;
  const id = button.dataset.taskDelete || button.dataset.taskRestore;
  action(async () => {
    await api('/api/task/' + operation, {id});
    notice(operation === 'delete' ? '업무를 휴지통으로 이동했습니다. 변경 이력은 유지됩니다.' : '업무를 복원했습니다. 일반 목록에서 확인할 수 있습니다.');
    await refresh();
  });
});

$('reply-close').onclick = () => $('reply-dialog').close();
$('reply-copy').onclick = async () => {
  try {
    await navigator.clipboard.writeText($('reply-text').value);
    $('reply-copy').textContent = '복사됨';
    setTimeout(() => $('reply-copy').textContent = '복사', 1600);
  } catch (error) {
    $('reply-text').select();
    document.execCommand('copy');
  }
};
document.addEventListener('click', event => {
  const button = event.target.closest('button[data-reply-task]');
  if (!button) return;
  action(async () => {
    const result = await api('/api/reply-draft', {task_id: button.dataset.replyTask});
    $('reply-subject').textContent = result.subject;
    $('reply-text').value = result.draft;
    $('reply-dialog').showModal();
  });
});
