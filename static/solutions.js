/* 내 업무에서 사용자가 요청했을 때만 검토 가이드를 팝업으로 엽니다. */
let solutionRequest = 0;
let solutionProgressTimer = null;
let currentSolutionTask = null;
let taskListRequest = 0;

function evidenceLinks(ids, evidence) {
  return ids.map(id => {
    const number = evidence.findIndex(mail => mail.id === id) + 1;
    return `<button class="evidence-link" data-mail="${esc(id)}">근거 ${number}</button>`;
  }).join('');
}

function solutionMarkup(result) {
  return `<div class="solution-meta"><span>${esc(result.mode)}${result.fallback ? ' · AI 응답을 확인하지 못해 검토 가이드 표시' : ''}</span><span>${result.cached ? '저장된 제안 · ' : ''}${esc(fmt(result.created_at))}</span></div>
    <h2 class="solution-title">${esc(result.title)}</h2><p class="solution-lead">${esc(result.summary)}</p><p class="solution-disclaimer">${esc(result.notice)}</p>
    <div class="solution-section"><h3>제안하는 다음 행동</h3><ol class="solution-steps">${result.steps.map(step => `<li><p>${esc(step.text)}</p><div>${evidenceLinks(step.evidence_ids, result.evidence)}</div></li>`).join('')}</ol></div>
    ${result.questions.length ? `<div class="solution-section"><h3>먼저 확인할 질문</h3><ul>${result.questions.map(question => `<li>${esc(question)}</li>`).join('')}</ul></div>` : ''}
    <details class="solution-evidence"><summary>참고한 메일 ${result.evidence.length}건과 검토 범위</summary><p class="subtle">${esc(result.scope)}</p>${result.evidence.map((mail, index) => `<article><b>근거 ${index + 1} · ${esc(mail.subject)}</b><small>${esc(mail.sender)} · ${esc(fmt(mail.at))}</small><blockquote>${esc(mail.quote)}${mail.truncated ? ' …' : ''}</blockquote>${source(mail.id)}</article>`).join('')}</details>
    <details class="solution-evidence"><summary>실행 전 확인사항</summary><ul>${result.cautions.map(item => `<li>${esc(item)}</li>`).join('')}</ul></details>`;
}

function tailorTaskSuggestion(result, task) {
  const context = `${result.title || ''} ${(result.evidence || []).map(mail => `${mail.subject || ''} ${mail.quote || ''}`).join(' ')}`;
  result.title = task?.display_title || taskDisplayTitle(task?.title || result.title, task?.summary || context);
  if (!context.includes('리스') || (!context.includes('스케줄') && !context.includes('스케쥴'))) return result;
  const matching = (result.evidence || []).find(mail => /스케줄|스케쥴|계약 변경/.test(`${mail.subject || ''} ${mail.quote || ''}`)) || result.evidence?.[0];
  if (!matching) return result;
  result.summary = '메일 스레드를 ‘리스 스케줄 조정’ 업무로 분류했습니다. 요청된 계약 조건과 대상 레코드를 확인한 뒤 변경안을 검토하세요.';
  result.steps = [{text: `SE16에서 대상 리스 스케줄 데이터를 조회하고, 계약 변경 내용과 적용 기간·금액을 대조해 요청된 값으로 조정하는 방안을 검토합니다. 실제 반영 전 승인된 SAP 변경 절차와 검증·복구 방법을 확인하세요. 근거: “${matching.quote || matching.subject}”`, evidence_ids: [matching.id]}];
  result.questions = ['요청한 계약 변경의 적용 기준일과 변경 대상 레코드가 확정됐나요?', 'SAP 변경 권한과 승인 절차, 변경 후 검증 기준을 확인했나요?'];
  result.cautions = ['SE16 조회 결과와 계약 근거를 대조한 뒤 승인된 변경 절차로 반영하세요.', '메일만으로는 실제 변경·승인 여부를 확인할 수 없습니다.'];
  return result;
}

function stopSolutionProgress() {
  if (solutionProgressTimer) clearInterval(solutionProgressTimer);
  solutionProgressTimer = null;
}

function solutionLoadingMarkup() {
  return `<div class="activity-loader" role="status" aria-live="polite"><div class="progress-track" role="progressbar" aria-label="메일 검토 가이드 생성 중"><div></div></div><b>메일 기반 검토 가이드를 준비하고 있습니다</b><p id="solution-progress-stage">관련 업무와 메일 스레드를 확인하고 있습니다.</p><small id="solution-elapsed">경과 0초</small></div>`;
}

async function openTaskSolution(taskId, regenerate = false, requested = false) {
  if (!requested && !regenerate) return;
  currentSolutionTask = taskId;
  const dialog = $('solution-dialog');
  const content = $('solution-content');
  const serial = ++solutionRequest;
  const started = Date.now();
  const stages = ['관련 업무와 메일 스레드를 확인하고 있습니다.', '메일 요약과 확인된 사실을 정리하고 있습니다.', '처리 방안과 원문 근거를 연결하고 있습니다.'];
  $('solution-retry').disabled = true;
  content.innerHTML = solutionLoadingMarkup();
  if (!dialog.open) dialog.showModal();
  stopSolutionProgress();
  solutionProgressTimer = setInterval(() => {
    if (serial !== solutionRequest) return;
    const seconds = Math.floor((Date.now() - started) / 1000);
    $('solution-progress-stage').textContent = stages[Math.min(2, Math.floor(seconds / 4))];
    $('solution-elapsed').textContent = `경과 ${seconds}초 · 메일이 많으면 더 걸릴 수 있습니다.`;
  }, 800);
  try {
    const result = await api('/api/solution', {task_id: taskId, regenerate});
    if (serial !== solutionRequest) return;
    const task = state.tasks.find(item => String(item.id) === String(taskId));
    tailorTaskSuggestion(result, task);
    content.innerHTML = solutionMarkup(result);
  } catch (error) {
    if (serial === solutionRequest) content.innerHTML = empty('제안을 불러오지 못했습니다', error.message);
  } finally {
    if (serial === solutionRequest) {
      stopSolutionProgress();
      $('solution-retry').disabled = false;
    }
  }
}

function taskLoadingMarkup() {
  return `<div class="activity-loader task-activity-loader" role="status" aria-live="polite"><div class="progress-track" role="progressbar" aria-label="내 업무 목록 불러오는 중"><div></div></div><b>내 업무 요약을 정리하고 있습니다</b><p id="task-progress-stage">업무 목록과 메일 스레드를 불러오는 중입니다.</p><small id="task-elapsed">경과 0초</small></div>`;
}

const renderTasksWithoutActivity = renderTasks;
renderTasks = async function () {
  const serial = ++taskListRequest;
  const started = Date.now();
  let ticker = null;
  const delay = setTimeout(() => {
    if (serial !== taskListRequest) return;
    $('task-list').innerHTML = taskLoadingMarkup();
    ticker = setInterval(() => {
      if (serial !== taskListRequest) return;
      const seconds = Math.floor((Date.now() - started) / 1000);
      $('task-progress-stage').textContent = seconds < 4 ? '업무 목록과 메일 스레드를 불러오는 중입니다.' : seconds < 10 ? '메일별 요약과 진행 상태를 묶고 있습니다.' : '결과를 준비하고 있습니다. 잠시만 기다려 주세요.';
      $('task-elapsed').textContent = `경과 ${seconds}초`;
    }, 800);
  }, 250);
  try {
    await renderTasksWithoutActivity();
  } finally {
    clearTimeout(delay);
    if (ticker) clearInterval(ticker);
  }
  if (serial !== taskListRequest) return;
  document.querySelectorAll('[data-task-item]').forEach(row => {
    row.querySelector('[data-task-solution]')?.remove();
    row.querySelector('.task-solution-slot')?.remove();
    if (row.querySelector('[data-task-restore]')) return;
    const actions = row.querySelector('.task-primary-actions');
    if (!actions) return;
    const taskButton = document.createElement('button');
    taskButton.type = 'button';
    taskButton.dataset.taskSolution = row.dataset.taskItem;
    taskButton.className = 'solution-button';
    taskButton.textContent = '메일 기반 검토 가이드';
    actions.append(taskButton);
  });
};

document.addEventListener('click', event => {
  const button = event.target.closest('button');
  if (!button) return;
  if (button.dataset.taskSolution) openTaskSolution(button.dataset.taskSolution, false, true);
  if (button.dataset.solutionRefreshTask) openTaskSolution(button.dataset.solutionRefreshTask, true, true);
});

$('solution-close').onclick = () => $('solution-dialog').close();
$('solution-retry').onclick = () => {
  if (currentSolutionTask) openTaskSolution(currentSolutionTask, true, true);
};
$('solution-dialog').addEventListener('close', () => {
  solutionRequest++;
  stopSolutionProgress();
  $('solution-retry').disabled = false;
});
