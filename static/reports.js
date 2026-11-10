/* 주간 보고: 확정 업무만 표시하고, 항목별 요약·기간을 수정하거나 삭제합니다. 상태 정보는 표시하지 않습니다. */
const editingReportItems = new Set();

function reportPeriod(item) {
  if (!item.start && !item.end) return '기간 미정';
  return `${item.start || '미정'} ~ ${item.end || '미정'}`;
}

function reportItemView(reportId, item, index) {
  const key = reportId + ':' + item.id;
  if (editingReportItems.has(key)) {
    return `<div class="report-item editing" data-report-item="${esc(item.id)}">
      <label>업무명<input data-field="title" value="${esc(item.title)}" maxlength="200"></label>
      <label>요약 항목 <small>한 줄에 하나씩</small><textarea data-field="points" rows="${Math.max(2, item.points.length + 1)}">${esc(item.points.join('\n'))}</textarea></label>
      <div class="report-period-inputs"><label>시작일<input type="date" data-field="start" value="${esc(item.start || '')}"></label><span>~</span><label>종료일<input type="date" data-field="end" value="${esc(item.end || '')}"></label></div>
      <div class="button-row"><button class="primary" data-report-save="${esc(item.id)}" data-report-id="${esc(reportId)}">저장</button><button data-report-cancel="${esc(item.id)}" data-report-id="${esc(reportId)}">취소</button></div>
    </div>`;
  }
  return `<div class="report-item" data-report-item="${esc(item.id)}">
    <b>${index}. ${esc(item.title)}</b>
    <small class="report-period">${esc(reportPeriod(item))}</small>
    <ul class="report-points">${item.points.map(point => `<li>${esc(point)}</li>`).join('')}</ul>
    <div class="report-item-actions"><button data-report-edit="${esc(item.id)}" data-report-id="${esc(reportId)}">수정</button><button class="danger" data-report-delete="${esc(item.id)}" data-report-id="${esc(reportId)}">삭제</button></div>
  </div>`;
}

function reportCell(reportId, items) {
  return items.length ? items.map((item, index) => reportItemView(reportId, item, index + 1)).join('') : '<span class="subtle">해당 없음</span>';
}

function renderReports() {
  if (!state.reports.length) {
    $('report-list').innerHTML = empty('참조 양식으로 주간 보고를 만드세요', '구분 / 업무 내용(금주) / 차주 / 비고 4열로 생성되며 PDF로 내려받을 수 있습니다.');
    return;
  }
  $('report-list').innerHTML = state.reports.map(report => {
    const doc = report.document ? JSON.parse(report.document) : null;
    const heading = `<div class="report-heading"><b>${esc(doc?.title || '주간 보고')} · ${esc(report.start_date)} ~ ${esc(report.end_date)}</b><div class="button-row">${doc ? `<button data-pdf="${esc(report.id)}">PDF 다운로드</button>` : ''}<button data-download="${esc(report.id)}">Markdown 다운로드</button></div></div>`;
    // 이전 버전 보고서(상태 문구가 들어간 형식)는 다시 생성하도록 안내합니다.
    if (!doc || !doc.rows.every(row => [...row.this_week, ...row.next_week].every(item => Array.isArray(item.points) && item.id))) {
      return `<article class="panel">${heading}<p class="report-basis">이전 형식의 보고서입니다. 같은 기간으로 다시 생성하면 확정 업무 기준의 요약형 보고서와 항목 수정 기능을 사용할 수 있습니다.</p></article>`;
    }
    const rows = doc.rows.filter(row => row.this_week.length || row.next_week.length || row.notes.length);
    const body = rows.length ? `<div class="report-scroll"><table class="weekly-table"><thead><tr>${doc.columns.map(column => `<th>${esc(column)}</th>`).join('')}</tr></thead><tbody>${rows.map(row =>
      `<tr><th>${esc(row.category)}</th><td>${reportCell(report.id, row.this_week)}</td><td>${reportCell(report.id, row.next_week)}</td><td>${row.notes.map(note => `<p>${esc(note)}</p>`).join('')}${[...new Set([...row.this_week, ...row.next_week].flatMap(item => item.source_ids))].map(id => source(id)).join('<br>')}</td></tr>`).join('')}</tbody></table></div>`
      : empty('확정된 업무가 없습니다', '내 업무에서 업무를 확인하거나 진행하면 보고서에 반영됩니다.');
    return `<article class="panel">${heading}<p class="report-basis">${esc(doc.basis)}</p>${body}</article>`;
  }).join('');
}

function reportItemChanges(itemId) {
  const element = document.querySelector(`[data-report-item="${CSS.escape(itemId)}"]`);
  const value = field => element.querySelector(`[data-field="${field}"]`).value;
  return {title: value('title'), points: value('points'), start: value('start') || null, end: value('end') || null};
}

document.addEventListener('click', event => {
  const button = event.target.closest('button');
  if (!button || !button.dataset.reportId) return;
  const reportId = button.dataset.reportId;
  if (button.dataset.reportEdit) {
    editingReportItems.add(reportId + ':' + button.dataset.reportEdit);
    renderReports();
  } else if (button.dataset.reportCancel) {
    editingReportItems.delete(reportId + ':' + button.dataset.reportCancel);
    renderReports();
  } else if (button.dataset.reportSave) {
    const itemId = button.dataset.reportSave;
    const changes = reportItemChanges(itemId);
    action(async () => {
      await api('/api/report/item', {report_id: reportId, item_id: itemId, changes});
      editingReportItems.delete(reportId + ':' + itemId);
      notice('보고서 항목을 저장하고 PDF를 다시 만들었습니다.');
      await refresh();
    });
  } else if (button.dataset.reportDelete) {
    if (!confirm('이 항목을 보고서에서 삭제할까요? 내 업무는 바뀌지 않습니다.')) return;
    action(async () => {
      await api('/api/report/item', {report_id: reportId, item_id: button.dataset.reportDelete, delete: true});
      notice('보고서 항목을 삭제했습니다. 내 업무와 변경 이력은 그대로입니다.');
      await refresh();
    });
  }
});
