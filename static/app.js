const $ = id => document.getElementById(id);
const readPref = (key, fallback=null) => { try { return JSON.parse(localStorage.getItem(key)) ?? fallback; } catch { return fallback; } };
let preferenceQueue={},preferenceTimer;
const pref = (key, value) => {
  try { localStorage.setItem(key, JSON.stringify(value)); } catch {}
  if(state.token&&state.preferencesLoaded){preferenceQueue[key]=value;clearTimeout(preferenceTimer);preferenceTimer=setTimeout(flushPreferences,350);}
};
function flushPreferences(){if(!Object.keys(preferenceQueue).length)return;const body=preferenceQueue;preferenceQueue={};fetch('/api/preferences',{method:'POST',headers:{'Content-Type':'application/json','X-Book-Token':state.token},body:JSON.stringify(body),keepalive:true}).catch(()=>{});}
const state = { projects: [], selected: readPref('book'), position: null, chunks: [], chunk: null, dirty: false, editing: false,
  busy: false, online: false, model: null, token: null, page: 'text', refreshBusy: false, preview: null,
  generation: 0, followSample: null, importRevision: 0, importing: false,
  manual:null, manualCandidate:null, manualSearchOffset:0, selectedChunks:new Set(), chunksQuery: '' };
const escapeHtml = value => String(value ?? '').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const project = () => state.projects.find(p=>p.name===state.selected);
const url = suffix => `/api/projects/${encodeURIComponent(state.selected)}/${suffix}`;
const formatTime = value => value ? new Date(value.endsWith('Z') || /[+-]\d\d:\d\d$/.test(value) ? value : value.replace(' ','T')+'Z').toLocaleString('ru-RU') : '—';
function renderStable(id,html){const node=$(id);if(node.renderedHtml===html)return;const opened=new Set([...node.querySelectorAll('details[open]')].map(d=>d.dataset.key||d.querySelector('summary')?.textContent));node.innerHTML=html;node.renderedHtml=html;node.querySelectorAll('details').forEach(d=>{if(opened.has(d.dataset.key||d.querySelector('summary')?.textContent))d.open=true;});}

async function api(path, options={}) {
  const controller = new AbortController();
  const timeout = setTimeout(()=>controller.abort(), options.long ? 120000 : 15000);
  const headers = {...options.headers};
  if (options.method && options.method!=='GET') headers['X-Book-Token']=state.token;
  try {
    const response=await fetch(path,{...options,headers,signal:controller.signal});
    if (!response.ok) { let message=`Ошибка ${response.status}`; try { message=(await response.json()).detail||message; } catch {} throw new Error(typeof message==='string'?message:'Проверьте введённые данные'); }
    return await response.json();
  } catch(e) { if(e.name==='AbortError') throw new Error('Ответ не получен вовремя. Проверьте состояние перед повтором действия.'); throw e; }
  finally { clearTimeout(timeout); }
}
const post = (path, body={}) => api(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body),long:true});
function toast(message) { $('toast').textContent=message;$('toast').hidden=false;clearTimeout(toast.timer);toast.timer=setTimeout(()=>$('toast').hidden=true,6000); }
function choose(title, message, yes, no = 'Отмена', canStay = false) {
  const dialog = $('decision-dialog');
  if (dialog.open) return Promise.resolve('stay');
  $('decision-title').textContent = title;
  $('decision-message').textContent = message;
  $('decision-yes').textContent = yes;
  $('decision-no').textContent = no;
  $('decision-stay').hidden = !canStay;
  return new Promise(resolve => {
    const finish = value => { dialog.close(); resolve(value); };
    $('decision-yes').onclick = () => finish('yes');
    $('decision-no').onclick = () => finish('no');
    $('decision-stay').onclick = () => finish('stay');
    dialog.oncancel = event => { event.preventDefault(); finish('stay'); };
    dialog.showModal();
    (canStay ? $('decision-stay') : $('decision-no')).focus();
  });
}

async function decision(title, message, label = 'Продолжить') {
  return await choose(title, message, label) === 'yes';
}

function draftChanges() {
  const c = state.chunk, p = project();
  return {
    text: !!c && $('translation-text').value !== (c.translated_text || ''),
    note: !!c && $('chunk-note').value !== (c.note || ''),
    structure: !!c && ($('section-name').value !== c.section || $('exclude-chunk').checked !== !!c.excluded),
    title: !!p && $('book-title').dataset.book === p.name && $('book-title').value !== $('book-title').dataset.saved,
  };
}

function updateDirty() {
  state.dirty = Object.values(draftChanges()).some(Boolean);
  renderControls();
}

function setBookTitle(p) {
  $('book-title').value = p.display_name;
  $('book-title').dataset.book = p.name;
  $('book-title').dataset.saved = p.display_name;
}

async function saveDrafts(only = null) {
  const changes = draftChanges();
  const edits = [
    ['text', {text: $('translation-text').value}],
    ['note', {action: 'note', note: $('chunk-note').value}],
    ['structure', {action: 'structure', section: $('section-name').value, excluded: $('exclude-chunk').checked}],
  ];
  state.generation++;
  try {
    for (const [field, body] of edits) {
      if (!changes[field] || (only && only !== field)) continue;
      state.chunk = await post(url('chunks/' + state.position), {...body, expected_version: state.chunk.active_version});
      if (field === 'text') {
        $('translation-text').value = state.chunk.translated_text;
        state.editing = false;
      } else if (field === 'note') {
        $('chunk-note').value = state.chunk.note;
      } else {
        $('section-name').value = state.chunk.section;
        $('exclude-chunk').checked = !!state.chunk.excluded;
      }
    }
    if (changes.title && (!only || only === 'title')) {
      const saved = await post(url('settings'), {display_name: $('book-title').value});
      state.projects = state.projects.map(p => p.name === saved.name ? saved : p);
      setBookTitle(saved);
    }
    return true;
  } finally {
    updateDirty();
    if (!state.dirty && state.chunk) renderChunk(state.chunk, true);
  }
}

async function mayLeave() {
  if (!state.dirty) return true;
  const choice = await choose('Изменения ещё не сохранены', 'Сохранить изменения перед переходом?',
                              'Сохранить', 'Отбросить', true);
  if (choice === 'yes') return await action(() => saveDrafts()) === true;
  if (choice !== 'no') return false;
  state.dirty = false;
  if (project()) setBookTitle(project());
  if (state.chunk) renderChunk(state.chunk, true);
  return true;
}
async function action(fn) {
  if(state.busy)return;
  state.busy=true;renderControls();
  try { return await fn(); } catch(e){toast(e.message);} finally {state.busy=false;renderControls();}
}
function label(p) { if(p.runtime.active)return p.runtime.stop_requested?'Пауза запрошена':'Переводится';if(p.runtime.phase==='failed'||p.counts.failed)return 'Есть ошибка';if(p.final_ready)return p.reviewed===p.total?'Проверено':'Переведено';if(p.manual?.enabled&&!p.manual.complete&&!p.total)return 'Требуется разметка';if(p.manual?.enabled&&!p.manual.complete&&p.done===p.total)return 'Разметка продолжается';return p.done?'Приостановлено':'Готово к работе'; }
function renderLibrary() {
  const query=$('library-search').value.toLocaleLowerCase();
  const items=state.projects.filter(p=>(!p.archived||$('show-archive').checked)&&p.display_name.toLocaleLowerCase().includes(query));
  const signature=JSON.stringify(items.map(p=>[p.name,p.display_name,p.percent,label(p),p.archived]))+state.selected;
  if(signature===renderLibrary.signature)return;renderLibrary.signature=signature;
  const focused=document.activeElement?.dataset.project;
  $('project-list').innerHTML=items.map(p=>`<button class="project-item ${p.name===state.selected?'selected':''}" data-project="${escapeHtml(p.name)}" ${p.name===state.selected?'aria-current="true"':''}><strong>${escapeHtml(p.display_name)}</strong><span class="project-meta"><span>${p.archived?'Архив · ':''}${label(p)}</span><span>${p.percent}%</span></span></button>`).join('');
  if(focused)Array.from($('project-list').children).find(n=>n.dataset.project===focused)?.focus();
}
function renderProject() {
  const p=project();$('welcome').hidden=!!p;$('project-view').hidden=!p;if(!p)return;
  $('project-title').textContent=p.display_name;
  $('project-title').title=p.display_name;
  const status=$('project-status');
  status.textContent = p.archived ? 'Книга в архиве' :
    !p.runtime.active && p.runtime.phase === 'failed' ? 'Ошибка перевода' :
    p.final_ready && !p.runtime.active ? 'Перевод готов' : label(p);
  status.title = p.runtime.message || status.textContent;
  status.classList.toggle('error', !p.runtime.active && (p.runtime.phase==='failed'||!!p.counts.failed));
  status.classList.toggle('ready', p.runtime.active || p.final_ready);
  status.classList.toggle('waiting', !p.runtime.active && !p.final_ready && p.done > 0);
  const progressLabel = `${p.done} из ${p.total}${p.manual?.enabled&&!p.manual.complete?' размеченных':''}`;
  $('progress-value').textContent=progressLabel;
  $('progress-fill').style.width=`${p.percent}%`;
  $('progress-track').setAttribute('aria-valuenow',p.percent);
  $('progress-track').setAttribute('aria-valuetext','Переведено ' + progressLabel);
  $('last-saved').textContent=p.last_saved?'Сохранено ' + formatTime(p.last_saved):'';
  let timer=`Успешные запросы: ${p.duration_minutes} мин`;
  if(p.runtime.active&&p.last_run?.started_at)timer=`Сеанс: ${Math.max(0,Math.round((Date.now()-new Date(p.last_run.started_at))/60000))} мин · ${p.runtime.stop_requested?'ожидаем текущий ответ':'работает'}`;
  $('run-timer').textContent=timer;
  $('export-summary').textContent=`Переведено ${p.done}/${p.total}, принято ${p.reviewed}/${p.total}. Непрерывное готовое начало: ${p.partial_count} фрагментов.${p.manual?.enabled&&!p.manual.complete?' Ручная разметка книги ещё продолжается.':''} Машинный перевод требует вычитки.`;
  if ($('book-title').dataset.book !== p.name || !draftChanges().title) setBookTitle(p);
  $('archive-book').textContent=p.archived?'Вернуть из архива':'В архив';
  $('manual-markup-button').hidden=!p.manual?.enabled;$('manual-markup-button').textContent=p.manual?.complete?'Просмотреть разметку':'Продолжить разметку';
  const profile=p.profile;
  $('profile-summary').textContent=`${profile.lmstudio.model} · ${p.connection}`;
  $('profile-json').textContent=JSON.stringify(profile,null,2);$('preparation-notes').textContent=p.notes.join('\n')||'Автоматическое удаление оболочки не применялось.';
  renderControls();
}
function renderControls() {
  const p=project();if(!p)return;
  const active=p.runtime.active, blocked=state.busy||!state.online||active||p.archived;
  const hasWork=!!(p.counts.pending||p.counts.failed);
  const canUseModel=state.model?.ready||state.model?.state==='unknown_load';
  $('start-button').hidden=active||p.final_ready||!hasWork||p.archived;
  $('test-button').hidden=$('start-button').hidden;
  $('start-button').textContent=p.done||p.last_run?'Продолжить':'Перевести книгу';
  $('start-button').disabled=blocked||!!p.runtime.other_project||!canUseModel||state.dirty;
  $('test-button').disabled=$('start-button').disabled;
  $('stop-button').hidden=!active||p.runtime.phase==='external';$('stop-button').disabled=state.busy||!state.online||p.runtime.stop_requested;
  $('stop-button').textContent=p.runtime.stop_requested?'Сохраняем текущий фрагмент…':'Приостановить';
  let hint = '';
  if (!state.online) hint = 'Связь с приложением потеряна. Данные могут быть устаревшими.';
  else if (p.archived) hint = 'Чтобы продолжить перевод, верните книгу из архива в её свойствах.';
  else if (p.runtime.stop_requested) hint = 'Новые фрагменты запустятся после продолжения. Сейчас ждём ответ модели.';
  else if (p.runtime.other_project) hint = 'Сейчас переводится другая книга.';
  else if (!active && p.manual?.enabled && !p.manual.complete && !hasWork) hint = 'Добавьте фрагменты в ручной разметке, чтобы продолжить.';
  else if (!active && p.runtime.phase==='failed' && p.runtime.message) hint = p.runtime.message;
  else if (!active && !state.model?.ready && hasWork) hint = state.model?.message || 'Проверяем подключение к LM Studio…';
  $('action-hint').textContent=hint;
  $('action-hint').hidden=!hint;
  $('other-project').hidden=!p.runtime.other_project;
  $('project-notice').hidden=!hint&&!p.runtime.other_project;
  const c=state.chunk;
  const editBlocked=blocked||!c;
  const changes = draftChanges();
  $('translation-text').readOnly=editBlocked||!state.editing;
  const showsTranslation = isComparing() || $('reading-area').dataset.mode === 'translation';
  $('edit-text').hidden=state.editing||!c||!showsTranslation;
  $('edit-text').disabled=editBlocked;
  $('save-text').hidden=!state.editing;
  $('save-text').disabled=editBlocked||!changes.text;
  $('cancel-text').hidden=!state.editing;
  $('cancel-text').disabled=state.busy;
  $('fragment-details').disabled=state.busy||!c;
  $('review-text').disabled=editBlocked||state.dirty||!c?.active_version||c.active_version===c.reviewed_version;
  $('retranslate').disabled=editBlocked||state.dirty||!!c?.excluded||!!p.runtime.other_project||!canUseModel;
  $('restore-version').disabled=editBlocked||state.dirty||!c?.versions.length;
  $('save-structure').disabled=editBlocked||!changes.structure;
  $('save-note').disabled=editBlocked||!changes.note;
  for(const id of ['chunk-note', 'section-name', 'exclude-chunk']) $(id).disabled=editBlocked;
  document.querySelectorAll('[data-dismiss]').forEach(n=>n.disabled=blocked||state.dirty);
  const settingsBlocked=state.busy||!state.online||active;
  $('book-title').disabled=settingsBlocked;
  $('rename-book').disabled=settingsBlocked||!changes.title;
  for(const id of ['archive-book','add-term'])$(id).disabled=settingsBlocked;
  $('make-export').disabled=state.busy||!state.online||!p.done||active||state.dirty;
  $('backup-book').disabled=state.busy||!state.online||state.dirty;
  $('manual-markup-button').disabled=state.busy||!state.online||p.archived;
  const selectedCount=state.selectedChunks.size;
  const translationBlocked=blocked||!!p.runtime.other_project||!canUseModel||state.dirty;
  $('selection-count').textContent=`Выбрано: ${selectedCount}`;
  $('selection-count').hidden=!selectedCount;
  $('start-selected').textContent=selectedCount?`Перевести выбранные (${selectedCount})`:'Перевести выбранные';
  $('start-selected').hidden=!selectedCount;
  $('start-selected').disabled=translationBlocked||!selectedCount;
  $('start-current').hidden=!!selectedCount;
  $('start-current').textContent=c?.active_version?'Повторить перевод':'Перевести фрагмент';
  $('start-current').disabled=translationBlocked||!c||!!c.excluded;
  $('select-pending').hidden=!!selectedCount||!state.chunks.some(eligibleForTranslation);
  $('select-pending').disabled=state.busy||!state.online||p.archived;
  $('clear-selection').hidden=!selectedCount;
  $('clear-selection').disabled=state.busy||!selectedCount;
  const saveState = state.dirty?'Есть несохранённая правка':c?.active_version?'Сохранено':c?'Перевод ещё не сохранён':'';
  $('save-state').textContent=saveState;
  $('save-state').title=saveState;
  $('save-output').hidden = !p.final_ready && !p.partial_ready;
  $('save-output').textContent = p.final_ready ? 'Сохранить TXT' : 'Сохранить начало';
  $('save-output').disabled = state.busy || !state.online || state.dirty;
}

const eligibleForTranslation = chunk => !chunk.excluded && ['pending','failed'].includes(chunk.status);

function fragmentStatus(chunk) {
  if (chunk.excluded) return ['Исключён', 'excluded'];
  if (chunk.status === 'processing') return ['Переводится', 'processing'];
  if (chunk.status === 'failed') return ['Ошибка', 'failed'];
  if (chunk.active_version) {
    return [chunk.reviewed_version === chunk.active_version ? 'Проверен' : 'Переведён', 'ready'];
  }
  return ['Ожидает', 'waiting'];
}

function renderOutline() {
  const rawQuery = $('chunk-search').value.trim();
  const query = rawQuery.toLocaleLowerCase();
  const filter = $('chunk-filter').value;
  const rows = state.chunks.filter(chunk => {
    const title = chunk.position + ' ' + (chunk.section || 'Фрагмент');
    if (!title.toLocaleLowerCase().includes(query) &&
        !(rawQuery === state.chunksQuery && chunk.search_match)) return false;
    if (filter === 'all') return true;
    if (filter === 'excluded') return chunk.excluded;
    if (filter === 'warnings') return chunk.warning_count;
    if (filter === 'ready') return chunk.active_version;
    if (filter === 'unreviewed') return chunk.active_version && chunk.active_version !== chunk.reviewed_version;
    if (filter === 'reviewed') return chunk.active_version && chunk.active_version === chunk.reviewed_version;
    return filter === chunk.status;
  });
  const signature = JSON.stringify([state.selected, rows, state.position, [...state.selectedChunks].sort((a,b)=>a-b)]);
  if (renderOutline.signature === signature) return;
  renderOutline.signature = signature;
  const list = $('chunk-list');
  const focused = document.activeElement;
  const focusPosition = list.contains(focused) && (focused.dataset.position || focused.dataset.queuePosition);
  const focusedSelector = focused?.dataset.queuePosition ? 'data-queue-position' : 'data-position';
  const selectionChanged = renderOutline.selection !== state.selected + ':' + state.position;
  renderOutline.selection = state.selected + ':' + state.position;
  list.innerHTML = rows.map(chunk => {
    const selected = chunk.position === state.position;
    const [status, tone] = fragmentStatus(chunk);
    const section = escapeHtml(chunk.section || 'Фрагмент без раздела');
    return `<div class="chunk-row ${selected?'selected':''}">
      <input type="checkbox" data-queue-position="${chunk.position}" aria-label="Выбрать фрагмент ${chunk.position} для перевода" ${state.selectedChunks.has(chunk.position)?'checked':''} ${eligibleForTranslation(chunk)?'':'disabled'}>
      <button data-position="${chunk.position}" class="chunk-item" ${selected?'aria-current="true"':''}>
        <span class="chunk-index">${chunk.position}</span><span><strong>${section}</strong><small class="${tone}">${status}</small></span>
      </button>
    </div>`;
  }).join('') || '<p class="muted">Нет подходящих фрагментов</p>';
  if (focusPosition) list.querySelector('['+focusedSelector+'="'+focusPosition+'"]')?.focus({preventScroll:true});
  if (selectionChanged) revealFragmentInList();
}

function revealFragmentInList() {
  const list = $('chunk-list');
  if (!list.clientHeight) return;
  const row = list.querySelector('.chunk-row.selected');
  if (!row) return;
  const bounds = list.getBoundingClientRect();
  const selected = row.getBoundingClientRect();
  if (selected.top < bounds.top) list.scrollTop += selected.top - bounds.top;
  else if (selected.bottom > bounds.bottom) list.scrollTop += selected.bottom - bounds.bottom;
}

function renderChunk(c,force=false) {
  const old=state.chunk;
  if(state.dirty&&!force)return;
  state.chunk=c;
  const label = `${c.position} · ${c.section||'Без раздела'}`;
  $('chunk-label').textContent=label;
  $('chunk-label').title=label;
  if($('source-text').textContent!==c.source_text)$('source-text').textContent=c.source_text;
  if(force||!old||old.position!==c.position||old.active_version!==c.active_version)$('translation-text').value=c.translated_text||'';
  $('review-badge').textContent=c.active_version&&c.reviewed_version===c.active_version?'Принят':'Не проверен';
  if(force||!old||old.position!==c.position){$('section-name').value=c.section;$('exclude-chunk').checked=!!c.excluded;$('chunk-note').value=c.note||'';}
  const index = state.chunks.findIndex(item => item.position === state.position);
  $('fragment-position').textContent = index<0?'':(index+1)+' / '+state.chunks.length;
  $('previous').disabled = index <= 0;
  $('next').disabled = index < 0 || index >= state.chunks.length - 1;
  renderStable('warnings',c.warnings.filter(w=>!w.dismissed).map(w=>`<div class="warning"><span>${escapeHtml(w.message)}</span><button class="text-button" data-dismiss="${escapeHtml(w.code)}">Проверено, оставить</button></div>`).join('')+(c.error?`<div class="warning error">${escapeHtml(c.error)}</div>`:''));
  const versionSignature=JSON.stringify([state.selected, c.position, c.versions.map(v=>v.id)]);
  if(force||versionSignature!==renderChunk.versionSignature){
    renderChunk.versionSignature=versionSignature;
    $('version-select').innerHTML=c.versions.map(v=>`<option value="${v.id}">№${v.id} · ${escapeHtml(({manual:'Правка',machine:'Машинная',imported:'Исходная',restore:'Восстановленная'})[v.kind]||v.kind)} · ${formatTime(v.created_at)}</option>`).join('');showVersion();
  }
  renderStable('attempt-history',c.attempt_history.map(a=>`<details data-key="${a.id}"><summary>${formatTime(a.started_at)} · ${escapeHtml(a.status)} · ${a.duration?.toFixed(1)||'—'} с</summary><p>${escapeHtml(a.error||'Ответ сохранён')}</p><pre>${escapeHtml(a.response||'Ответ не получен')}</pre></details>`).join('')||'<p class="muted">Новых попыток пока нет.</p>');
  renderControls();
  restoreReaderBookmark();
}
function showVersion(){const v=state.chunk?.versions.find(v=>v.id===Number($('version-select').value));$('version-preview').textContent=v?.text||'Нет сохранённых версий';}
async function loadChunk(force=false){
  if(!state.selected||!state.position)return;
  const name=state.selected,position=state.position,generation=state.generation;
  const c=await api(url(`chunks/${position}`));
  if(name===state.selected&&position===state.position&&generation===state.generation)renderChunk(c,force);
}
async function selectChunk(position) {
  if (state.busy || !position) return;
  if (position === state.position) { setFragmentRailOpen(false); return; }
  if (!await mayLeave()) return;
  await action(async () => {
    const c = await api(url('chunks/' + position));
    state.position = position;
    state.editing = false;
    state.generation++;
    pref('position:' + state.selected, position);
    renderOutline();
    renderChunk(c, true);
    setFragmentRailOpen(false);
  });
}
async function selectProject(name) {
  if(state.busy||!await mayLeave())return;
  state.dirty=false;state.editing=false;state.selected=name;state.position=null;state.chunk=null;
  state.generation++;state.chunks=[];state.chunksQuery='';state.model=null;state.selectedChunks=new Set();
  setLibraryOpen(false);
  pref('book',name);$('run-range').value='';
  for (const id of ['export-download', 'backup-download', 'quick-download']) $(id).hidden = true;
  setPage('text');
  setReaderMode('translation');
  renderLibrary();
  renderProject();
  checkModel();
  await refreshSelected();
}
async function refreshSelected(){
  if(!project())return;const name=state.selected,generation=state.generation;
  const query=$('chunk-search').value.trim();
  const result=await api(url('chunks')+'?q='+encodeURIComponent(query));
  if(query!==$('chunk-search').value.trim())return;
  if(name!==state.selected||generation!==state.generation)return;
  state.chunks=result.chunks;
  state.chunksQuery=query;
  const selectable=new Set(state.chunks.filter(eligibleForTranslation).map(c=>c.position));state.selectedChunks=new Set([...state.selectedChunks].filter(p=>selectable.has(p)));
  if (!state.chunks.length) {
    state.position = null;
    state.chunk = null;
    state.editing = false;
    $('chunk-label').textContent = 'Нет фрагментов';
    $('chunk-label').title = '';
    $('fragment-position').textContent = '';
    $('source-text').textContent = 'Создайте первый фрагмент в ручной разметке.';
    $('translation-text').value = '';
    $('review-badge').textContent = '';
    for (const id of ['warnings', 'version-select', 'version-preview', 'attempt-history']) $(id).replaceChildren();
    $('previous').disabled = $('next').disabled = true;
    renderOutline();
    renderControls();
    return;
  }
  if(!state.position)state.position=readPref(`position:${name}`,project().current?.position||1);
  if(!state.chunks.some(c=>c.position===state.position))state.position=state.chunks[0]?.position;
  renderOutline();await loadChunk();
  if(state.page!=='text'&&state.page!=='fragment')await loadHistory();
}
async function refresh(){
  if(state.refreshBusy)return;state.refreshBusy=true;
  try {
    if(!state.token)state.token=(await api('/api/session')).token;
    if(!state.preferencesLoaded){
      const saved=await api('/api/preferences');
      for(const [key,value] of Object.entries(saved)){try{localStorage.setItem(key,JSON.stringify(value));}catch{}}
      if(saved.book)state.selected=saved.book;
      if(['16','18','21'].includes(saved.fontSize)){$('font-size').value=saved.fontSize;setFont();}
      state.preferencesLoaded=true;
    }
    const result=await api('/api/projects');state.projects=result.projects;state.online=true;
    $('connection').hidden=true;$('library-errors').textContent=(result.errors||[]).map(e=>`${e.name}: ${e.message}`).join('\n');
    if (!state.projects.some(p => p.name === state.selected)) {
      state.selected = state.projects.find(p => !p.archived)?.name || null;
      state.position = null;
      state.chunk = null;
      state.editing = state.dirty = false;
      state.generation++;
    }
    const p=project();
    if (state.modelFor !== state.selected) checkModel();
    if (state.followSample?.name === state.selected && p?.last_run?.ended_at &&
        p.last_run.id !== state.followSample.previous && !p.runtime.active && !state.dirty) {
      state.followSample=null;
      const positions=JSON.parse(p.last_run.selection);
      if(positions.length){state.position=positions[0];state.chunk=null;state.editing=false;pref(`position:${state.selected}`,state.position);}
      toast(p.last_run.state==='complete'?'Пробный перевод готов. Открыт первый фрагмент пробы.':'Проба остановлена. Проверьте состояние и историю.');
    }
    renderLibrary();renderProject();await refreshSelected();
    refresh.last=Date.now();
  } catch(e){state.online=false;$('connection').hidden=false;$('connection').textContent=`Нет связи с приложением. Последнее обновление: ${refresh.last?new Date(refresh.last).toLocaleTimeString('ru-RU'):'не получено'}. ${e.message}`;renderControls();}
  finally{state.refreshBusy=false;}
}
async function checkModel(){
  const name = state.selected;
  if (state.modelRequest?.name === name) return;
  const request = {name};
  state.modelRequest = request;
  const indicator = $('model-state');
  indicator.disabled = true;
  $('model-label').textContent = 'Проверяем LM Studio…';
  try {
    const result = await api('/api/model' + (name ? '?name=' + encodeURIComponent(name) : ''));
    if (request !== state.modelRequest || name !== state.selected) return;
    state.model = result;
    state.modelFor = name;
    $('model-label').textContent = result.ready ? result.message : 'Проверить LM Studio';
    indicator.title = result.message + ' ' + result.address;
    indicator.className = 'model-state ' + (result.ready ? 'ready' : 'unavailable');
  } catch {
    if (request !== state.modelRequest || name !== state.selected) return;
    state.model = {ready: false, message: 'Не удалось проверить модель. Повторите проверку.'};
    state.modelFor = name;
    $('model-label').textContent = 'Проверить LM Studio';
    indicator.title = state.model.message;
    indicator.className = 'model-state unavailable';
  } finally {
    if (request === state.modelRequest) {
      state.modelRequest = null;
      indicator.disabled = false;
      renderControls();
    }
  }
}
function parseRange(){
  const raw=$('run-range').value.trim().replaceAll('–','-').replaceAll('—','-');if(!raw)return null;
  const values=new Set();
  for(const part of raw.split(',')){const m=part.trim().match(/^(\d+)(?:\s*-\s*(\d+))?$/);if(!m)throw new Error('Укажите номера: например, 12–15, 20');const a=Number(m[1]),b=Number(m[2]||m[1]);if(a<1||b<a||b>state.chunks.length)throw new Error('Диапазон выходит за пределы книги');for(let i=a;i<=b;i++)values.add(i);}
  return [...values];
}
async function start(sample=false,retranslate=false,explicitPositions=null){
  if(state.dirty){toast('Сначала сохраните правку.');return;}
  await action(async()=>{
    let positions=retranslate?[state.position]:explicitPositions;
    const previous = project()?.last_run?.id ?? null;
    if(sample){
      const proposed=await post(url('sample'),{positions});
      if(!proposed.chunks.length)throw new Error('В выбранном объёме нет фрагментов для пробы.');
      if(!await decision('Перевести пробу?',proposed.chunks.map(c=>`${c.position}. ${c.section}\n${c.preview}…`).join('\n\n'),'Перевести пробу'))return;
      positions = proposed.chunks.map(c => c.position);
    }
    if(retranslate&&!await decision('Новая машинная версия','Текущая версия и ручные правки останутся в истории. После успешного перевода новой активной версией станет ответ модели.','Создать версию'))return;
    await post(url('start'),{sample,retranslate,positions});
    if(explicitPositions)state.selectedChunks=new Set();
    if(sample)state.followSample={name: state.selected, previous};
    toast(sample?'Пробный перевод запущен':'Перевод запущен');
    await refresh();
  });
}
async function edit(body){if(state.dirty&&body.action){toast('Сначала сохраните правку перевода.');return;}await action(async()=>{state.generation++;const c=await post(url(`chunks/${state.position}`),{...body,expected_version:state.chunk.active_version});state.dirty=false;renderChunk(c,true);await refresh();});}
async function loadHistory(){
  const name=state.selected;const h=await api(url('history'));if(name!==state.selected)return;
  renderStable('run-history',h.runs.map(r=>`<details data-key="${r.id}"><summary>${formatTime(r.started_at)} · ${escapeHtml(r.state)} · ${JSON.parse(r.selection).length} фрагм.</summary><p>${escapeHtml(r.message||'')}</p><p>Состав: ${escapeHtml(r.selection)}</p><pre>${escapeHtml(JSON.stringify(JSON.parse(r.profile),null,2))}</pre></details>`).join('')||'<p>Сеансов пока нет.</p>');
  renderStable('event-history',h.events.map(e=>`<div class="event"><time>${formatTime(e.at)}</time><span>${e.position?'Фрагмент '+e.position+' · ':''}${escapeHtml(e.message)}</span></div>`).join(''));
  renderStable('export-history','<h2>Предыдущие экспорты</h2>'+h.exports.map(e=>{
    const label = formatTime(e.created_at) + ' · ' + JSON.parse(e.manifest).length + ' фрагм.';
    return e.available
      ? '<p><a href="' + escapeHtml(url('exports/' + encodeURIComponent(e.id))) + '" download>' + label + '</a></p>'
      : '<p class="muted">' + label + ' · Файл недоступен. Можно собрать новый TXT.</p>';
  }).join(''));
  renderStable('term-list',h.terms.map(t=>`<div class="event"><span>${escapeHtml(t.source)} → ${escapeHtml(t.target)}</span><button class="text-button" data-delete-term="${t.id}">Удалить термин</button></div>`).join(''));
}
function setPage(page) {
  state.page = page;
  $('book-tools').open = false;
  setFragmentRailOpen(false);
  $('return-to-reader').hidden = page === 'text';
  for (const section of document.querySelectorAll('.page')) section.hidden = section.id !== 'page-' + page;
  if (page === 'text') restoreReaderBookmark(true);
  else $('page-' + page).scrollTop = 0;
}
async function showPage(page) {
  if (state.busy || !await mayLeave()) return;
  state.editing = false;
  setPage(page);
  renderControls();
  if (page !== 'text' && page !== 'fragment') await action(loadHistory);
}
function segmentationMode(){return document.querySelector('[name="segmentation-mode"]:checked')?.value||'auto';}
function formData(){const file=$('book-file').files[0];if(!file)throw new Error('Сначала выберите TXT-файл');const data=new FormData();data.append('file',file);data.append('display_name',$('display-name').value);data.append('encoding',$('encoding').value);data.append('strip_wrapper',$('strip-wrapper').checked);data.append('mode',segmentationMode());return data;}
function invalidatePreview() {
  state.importRevision++;
  state.preview = null;
  $('import-preview').hidden = true;
  $('create-button').disabled = state.importing || !$('book-file').files.length;
}
async function previewImport(){
  if (state.importing) return;
  const revision = state.importRevision;
  $('preview-book').disabled=true;$('form-error').hidden=true;
  try {
    const p = await api('/api/preview', {method:'POST', body:formData(), long:true});
    if (revision !== state.importRevision || !$('add-dialog').open) return;
    state.preview = p;
    $('import-summary').textContent=p.mode==='manual'?`После подготовки: ${p.prepared_chars.toLocaleString()} знаков. Книга будет добавлена без автоматических фрагментов.`:`${p.total} фрагментов · исходник ${p.original_chars.toLocaleString()} знаков · после подготовки ${p.prepared_chars.toLocaleString()}. Проверьте текст и состав перед добавлением.`;
    $('original-preview').textContent=p.original_preview;$('prepared-preview').textContent=p.prepared_preview;$('removed-preview').textContent=(p.removed_start+'\n'+p.removed_end).trim()||'Ничего не удаляется.';
    $('import-structure').innerHTML=p.chunks.map(c=>`<div class="import-row"><input type="checkbox" data-include="${c.position}" checked aria-label="Включить фрагмент ${c.position}"><span>${c.position}</span><input data-section="${c.position}" class="text-input" aria-label="Раздел фрагмента ${c.position}" value="${escapeHtml(c.section)}"><small>${c.chars} знаков</small></div>`).join('');
    $('automatic-preview').hidden=p.mode==='manual';$('manual-import-preview').hidden=p.mode!=='manual';
    $('import-preview').hidden=false;
  } catch(e) {
    if (revision === state.importRevision && $('add-dialog').open) {
      $('form-error').textContent=e.message;
      $('form-error').hidden=false;
    }
  } finally {
    $('preview-book').disabled=false;
  }
}
async function createProject(event) {
  event.preventDefault();
  if (state.importing) return;
  state.importing = true;
  $('create-button').disabled = true;
  $('create-button').textContent = 'Подготавливаем…';
  $('form-error').hidden = true;
  try {
    const manual = segmentationMode() === 'manual';
    const data = formData();
    const structure = state.preview?.chunks.map(c => ({
      position: c.position,
      section: document.querySelector('[data-section="' + c.position + '"]').value,
      excluded: !document.querySelector('[data-include="' + c.position + '"]').checked,
    })) || [];
    data.append('structure', JSON.stringify(structure));
    const created = await api('/api/import', {method: 'POST', body: data, long: true});
    $('add-dialog').close();
    $('add-form').reset();
    invalidatePreview();
    state.projects = [...state.projects.filter(book => book.name !== created.name), created];
    await selectProject(created.name);
    toast('Книга добавлена');
    if (manual) await openManual();
  } catch (e) {
    if ($('add-dialog').open) {
      $('form-error').textContent = e.message;
      $('form-error').hidden = false;
    } else {
      toast(e.message);
    }
  } finally {
    state.importing = false;
    $('create-button').disabled = !$('book-file').files.length;
    $('create-button').textContent = 'Добавить';
  }
}
function closeDialog(){if(!state.importing)$('add-dialog').close();}

function manualSpan(fragment,start,end,kind){
  if(end<=start)return;
  const span=document.createElement('span');span.className=`manual-segment ${kind}`;span.dataset.start=start;span.dataset.end=end;span.textContent=state.manual.text.slice(start,end);fragment.append(span);
}
function renderManual(){
  const m=state.manual;if(!m)return;const reader=$('manual-reader'),scroll=reader.scrollTop,fragment=document.createDocumentFragment();let start=0;
  for(let i=0;i<m.boundaries.length;i++){
    const end=m.boundaries[i];manualSpan(fragment,start,end,'committed');
    const divider=document.createElement('div');divider.className='manual-divider';const label=document.createElement('span');label.textContent=`Фрагмент ${i+1} · ${(end-start).toLocaleString()} знаков`;divider.append(label);
    if(i===m.boundaries.length-1){const undo=document.createElement('button');undo.type='button';undo.className='manual-divider-undo';undo.dataset.manualRemove='1';const locked=m.cursor<=m.locked_cursor;undo.disabled=locked;undo.textContent=locked?'Разделение защищено':'Отменить разделение';undo.title=locked?'Этот фрагмент уже переводился или входит в текущий сеанс перевода.':'Удалить эту границу и вернуть текст к ручной разметке';divider.append(undo);}
    fragment.append(divider);start=end;
  }
  const candidate=state.manualCandidate;
  if(!m.complete&&candidate>start&&candidate<m.text.length){
    manualSpan(fragment,start,candidate,'open');
    const controls=document.createElement('span');controls.className='manual-candidate';
    const add=document.createElement('button');add.type='button';add.className='manual-add';add.dataset.manualAdd=String(candidate);add.textContent='+';add.title='Завершить фрагмент здесь';controls.append(add);
    const cancel=document.createElement('button');cancel.type='button';cancel.className='manual-candidate-cancel';cancel.dataset.manualCancel='1';cancel.textContent='×';cancel.title='Убрать предварительную отметку';controls.append(cancel);fragment.append(controls);
    manualSpan(fragment,candidate,m.text.length,'open');
  }else manualSpan(fragment,start,m.text.length,m.complete?'committed':'open');
  reader.replaceChildren(fragment);reader.scrollTop=scroll;
  const remaining=m.text.length-m.cursor,fragmentCount=m.boundaries.length;
  $('manual-summary').textContent=m.complete?`Разметка завершена · ${fragmentCount} фрагм.`:`Готово ${fragmentCount} фрагм. · осталось разметить ${remaining.toLocaleString()} знаков. Новые фрагменты можно переводить независимо.`;
  $('manual-undo').disabled=!m.boundaries.length||m.cursor<=m.locked_cursor;
  $('manual-finish').disabled=m.complete||remaining<=0||remaining>m.hard_max_chars;
  if(m.complete){$('manual-size').textContent='Вся книга размечена';$('manual-message').textContent='Все границы сохранены.';return;}
  if(candidate>m.cursor){const size=candidate-m.cursor,tokens=Math.max(1,Math.round(size/m.chars_per_token));$('manual-size').textContent=`Будущий фрагмент: ${size.toLocaleString()} знаков · ≈ ${tokens.toLocaleString()} токенов`;$('manual-size').className='manual-size '+(size>m.hard_max_chars?'bad':size>=m.target_chars*.7?'good':'');$('manual-message').textContent=size>m.hard_max_chars?`Превышен максимум ${m.hard_max_chars.toLocaleString()} знаков. Выберите более раннее место.`:'Нажмите + или Ctrl+Enter, чтобы сохранить эту границу.';}
  else{$('manual-size').textContent=`Цель около ${m.target_chars.toLocaleString()} · максимум ${m.hard_max_chars.toLocaleString()} знаков`;$('manual-size').className='manual-size';$('manual-message').textContent=remaining>m.hard_max_chars?'Щёлкните между словами в конце будущего фрагмента и нажмите +.':'Остаток помещается в один фрагмент: можно завершить разметку.';}
}
function normalizeManualOffset(offset){const text=state.manual.text,isWord=c=>/[\p{L}\p{N}]/u.test(c||'');if(isWord(text[offset-1])&&isWord(text[offset])){let left=offset-1,right=offset;while(left>0&&!/\s/.test(text[left]))left--;while(right<text.length&&!/\s/.test(text[right]))right++;const choices=[left,right].filter(v=>v>0&&v<text.length);if(choices.length)offset=choices.sort((a,b)=>Math.abs(a-offset)-Math.abs(b-offset))[0];}while(offset<text.length&&/\s/.test(text[offset]))offset++;return offset;}
async function openManual(){
  if(!state.selected)return;await action(async()=>{state.manual=await api(url('manual'),{long:true});state.manualCandidate=null;state.manualSearchOffset=state.manual.cursor;$('manual-title').textContent=project()?.display_name||'Разметка книги';renderManual();$('manual-dialog').showModal();requestAnimationFrame(()=>{const open=$('manual-reader').querySelector('.manual-segment.open');open?.scrollIntoView({block:'start'});});});
}
async function saveManualBoundary(offset){
  if(!state.manual||offset<=state.manual.cursor||offset>=state.manual.text.length)return;
  const size=offset-state.manual.cursor;if(size>state.manual.hard_max_chars){toast('Этот фрагмент превышает максимальный размер. Поставьте границу раньше.');return;}
  await action(async()=>{state.manual=await post(url('manual'),{action:'add',offset,expected_cursor:state.manual.cursor});state.manualCandidate=null;renderManual();$('manual-reader').querySelector('.manual-divider:last-of-type')?.scrollIntoView({block:'center'});await refresh();});
}
async function removeLastManualBoundary(){if(!state.manual||!state.manual.boundaries.length)return;await action(async()=>{state.manual=await post(url('manual'),{action:'remove_last',expected_cursor:state.manual.cursor});state.manualCandidate=null;renderManual();await refresh();toast('Последнее разделение отменено. Выберите новое место.');});}
function manualPoint(event){
  const span=event.target.closest?.('.manual-segment.open');if(!span||!state.manual||state.manual.complete)return;
  let point=document.caretPositionFromPoint?.(event.clientX,event.clientY);let node=point?.offsetNode,offset=point?.offset;
  if(!point&&document.caretRangeFromPoint){const range=document.caretRangeFromPoint(event.clientX,event.clientY);node=range?.startContainer;offset=range?.startOffset;}
  if(!node)return;while(node&&node.parentElement!==span)node=node.parentNode;if(!node||node.nodeType!==Node.TEXT_NODE)return;
  const absolute=normalizeManualOffset(Number(span.dataset.start)+offset);if(absolute<=state.manual.cursor||absolute>=state.manual.text.length)return;
  state.manualCandidate=absolute;renderManual();
}
function findManual(){
  const query=$('manual-search').value;if(!state.manual||!query)return;let found=state.manual.text.toLocaleLowerCase().indexOf(query.toLocaleLowerCase(),state.manualSearchOffset);
  if(found<0)found=state.manual.text.toLocaleLowerCase().indexOf(query.toLocaleLowerCase());if(found<0){toast('Текст не найден');return;}state.manualSearchOffset=found+query.length;
  const span=[...$('manual-reader').querySelectorAll('.manual-segment')].find(n=>found>=Number(n.dataset.start)&&found<Number(n.dataset.end));if(!span)return;span.scrollIntoView({block:'center'});const node=span.firstChild;if(node){const range=document.createRange();range.setStart(node,found-Number(span.dataset.start));range.setEnd(node,Math.min(node.length,found-Number(span.dataset.start)+query.length));const selection=getSelection();selection.removeAllRanges();selection.addRange(range);}
}
$('add-project').onclick=async()=>{if(state.busy||!await mayLeave())return;$('form-error').hidden=true;$('add-dialog').showModal();};
$('welcome-add').onclick=()=>$('add-project').click();
$('close-dialog').onclick=closeDialog;$('cancel-dialog').onclick=closeDialog;$('add-form').onsubmit=createProject;$('preview-book').onclick=previewImport;
$('add-dialog').oncancel=event=>{if(state.importing)event.preventDefault();};
for(const id of ['book-file','encoding','strip-wrapper'])$(id).addEventListener('change',invalidatePreview);
document.querySelectorAll('[name="segmentation-mode"]').forEach(n=>n.addEventListener('change',invalidatePreview));
$('book-file').addEventListener('change',()=>{$('display-name').value=$('book-file').files[0]?.name.replace(/\.txt$/i,'')||'';});
$('manual-markup-button').onclick=openManual;$('manual-close').onclick=()=>$('manual-dialog').close();$('manual-done').onclick=()=>$('manual-dialog').close();
$('manual-reader').onclick=e=>{const add=e.target.closest('[data-manual-add]'),cancel=e.target.closest('[data-manual-cancel]'),remove=e.target.closest('[data-manual-remove]');if(add)saveManualBoundary(Number(add.dataset.manualAdd));else if(cancel){state.manualCandidate=null;renderManual();}else if(remove&&!remove.disabled)removeLastManualBoundary();else manualPoint(e);};
$('manual-undo').onclick=removeLastManualBoundary;
$('manual-finish').onclick=()=>action(async()=>{state.manual=await post(url('manual'),{action:'finish',expected_cursor:state.manual.cursor});state.manualCandidate=null;renderManual();await refresh();});
$('manual-find-next').onclick=findManual;$('manual-search').onkeydown=e=>{if(e.key==='Enter'){e.preventDefault();findManual();}};
$('manual-dialog').onkeydown=e=>{if(e.key==='Escape'&&state.manualCandidate){e.preventDefault();state.manualCandidate=null;renderManual();return;}if(e.ctrlKey&&e.key==='Enter'&&state.manualCandidate){e.preventDefault();saveManualBoundary(state.manualCandidate);}if(e.ctrlKey&&e.key.toLocaleLowerCase()==='z'&&!e.shiftKey&&!$('manual-undo').disabled){e.preventDefault();$('manual-undo').click();}};
$('project-list').onclick=e=>{const n=e.target.closest('[data-project]');if(n)selectProject(n.dataset.project).catch(e=>toast(e.message));};
$('chunk-list').onclick=e=>{const checkbox=e.target.closest('[data-queue-position]'),n=e.target.closest('[data-position]');if(checkbox){const position=Number(checkbox.dataset.queuePosition);checkbox.checked?state.selectedChunks.add(position):state.selectedChunks.delete(position);renderOutline();renderControls();}else if(n)selectChunk(Number(n.dataset.position));};
$('library-search').oninput=renderLibrary;$('show-archive').onchange=renderLibrary;$('chunk-search').oninput=()=>{renderOutline();clearTimeout(state.searchTimer);state.searchTimer=setTimeout(()=>refreshSelected().catch(e=>toast(e.message)),250);};$('chunk-filter').onchange=renderOutline;
function navigateFragment(direction) {
  const index = state.chunks.findIndex(c => c.position === state.position);
  if (index >= 0) selectChunk(state.chunks[index + direction]?.position);
}
$('previous').onclick=()=>navigateFragment(-1);
$('next').onclick=()=>navigateFragment(1);
for (const id of ['translation-text', 'chunk-note', 'section-name', 'exclude-chunk', 'book-title']) {
  $(id).addEventListener('input', updateDirty);
}
$('edit-text').onclick=()=>{
  state.editing=true;
  renderControls();
  $('translation-text').focus();
};
$('cancel-text').onclick=async()=>{
  if (await mayLeave()) { state.editing=false; renderControls(); $('edit-text').focus(); }
};
function saveField(field) {
  return action(async()=>{await saveDrafts(field);await refresh();});
}
$('save-text').onclick=()=>saveField('text');
$('review-text').onclick=()=>edit({action:'review'});
$('version-select').onchange=showVersion;$('restore-version').onclick=()=>edit({action:'restore',version:Number($('version-select').value)});
$('save-structure').onclick=()=>saveField('structure');
$('save-note').onclick=()=>saveField('note');
$('warnings').onclick=e=>{const n=e.target.closest('[data-dismiss]');if(n)edit({action:'dismiss',code:n.dataset.dismiss});};
$('start-button').onclick=()=>start();
$('test-button').onclick=()=>{ $('book-tools').open=false; start(true); };
$('retranslate').onclick=()=>start(false,true);
$('start-current').onclick=()=>{
  const chunk=state.chunk;
  if (chunk) start(false,!!chunk.active_version,[chunk.position]);
};
$('select-pending').onclick=()=>{state.selectedChunks=new Set(state.chunks.filter(eligibleForTranslation).map(c=>c.position));renderOutline();renderControls();};$('clear-selection').onclick=()=>{state.selectedChunks=new Set();renderOutline();renderControls();};$('start-selected').onclick=()=>start(false,false,[...state.selectedChunks].sort((a,b)=>a-b));
$('stop-button').onclick=()=>action(async()=>{await post(url('stop'));await refresh();});
$('other-project').onclick=()=>selectProject(project().runtime.other_project);$('model-state').onclick=checkModel;
document.querySelectorAll('[data-page]').forEach(n=>n.onclick=()=>showPage(n.dataset.page));
$('return-to-reader').onclick=()=>showPage('text');
$('rename-book').onclick=()=>saveField('title');
$('archive-book').onclick=async()=>{
  if(state.busy||!await mayLeave())return;
  await action(async()=>{
    const saved=await post(url('settings'),{archived:!project().archived});
    state.projects=state.projects.map(p=>p.name===saved.name?saved:p);
    renderLibrary();renderProject();
    toast(saved.archived?'Книга в архиве. Её можно вернуть в свойствах.':'Книга возвращена в библиотеку');
  });
};
$('add-term').onclick=()=>action(async()=>{await post(url('settings'),{term:{source:$('term-source').value,target:$('term-target').value}});$('term-source').value='';$('term-target').value='';await loadHistory();await loadChunk();});
$('term-list').onclick=e=>{const n=e.target.closest('[data-delete-term]');if(n)action(async()=>{await post(url('settings'),{delete_term:Number(n.dataset.deleteTerm)});await loadHistory();});};
function offerExport(link, result) {
  link.href=result.url;
  link.download=result.filename;
  link.hidden=false;
}
$('save-output').onclick=()=>action(async()=>{
  const kind=project().final_ready?'draft':'prefix';
  const result=await post(url('export'),{kind});
  const link=$('quick-download');
  offerExport(link,result);
  link.textContent=kind==='draft'?'Скачать перевод':'Скачать начало';
  link.click();
  toast('TXT готов к сохранению');
});
$('make-export').onclick=()=>action(async()=>{
  const result=await post(url('export'),{kind:$('export-kind').value,positions:parseRange()});
  offerExport($('export-download'),result);
  await loadHistory();
  toast('TXT готов к сохранению');
});
$('backup-book').onclick=()=>action(async()=>{const result=await post(url('backup'));const a=$('backup-download');a.href=result.url;a.download=result.filename;a.hidden=false;toast('Резервная копия создана и проверена.');});
$('restore-file').onchange=async()=>{
  const input=$('restore-file'),file=input.files[0];
  input.value='';
  if(!file||state.busy||!await mayLeave())return;
  const restored=await action(async()=>{
    const data=new FormData();
    data.append('file',file);
    return api('/api/restore',{method:'POST',body:data,long:true});
  });
  if(restored){
    state.projects.push(restored);
    await selectProject(restored.name);
    toast('Копия восстановлена как отдельная книга');
  }
};
$('open-folder').onclick=()=>action(()=>post(url('open-output')));
$('font-size').value=readPref('fontSize','18');
function setFont(){document.documentElement.style.setProperty('--reader-size',$('font-size').value+'px');pref('fontSize',$('font-size').value);}
$('font-size').onchange=setFont;setFont();

const textPanes = [$('source-text'), $('translation-text')];
const programmaticScroll = new Map();
let readerScrollKey = null;
let readerOffsets = [0, 0];

function isComparing() {
  return $('page-text').dataset.layout === 'compare';
}

function bookmarkKey() {
  return state.selected && state.chunk && state.chunk.position === state.position ?
    'scroll:' + state.selected + ':' + state.position : null;
}

function scrollRange(pane) {
  return pane.scrollHeight - pane.clientHeight;
}

function setPaneScroll(index, top) {
  const pane = textPanes[index];
  if (!pane.clientHeight) return;
  const before = pane.scrollTop;
  pane.scrollTop = top;
  if (pane.scrollTop !== before) programmaticScroll.set(pane, pane.scrollTop);
  readerOffsets[index] = pane.scrollTop;
}

function restoreReaderBookmark(again = false) {
  const key = bookmarkKey();
  if (key !== readerScrollKey) {
    readerScrollKey = key;
    const saved = key ? readPref(key, [0,0]) : null;
    readerOffsets = [0,1].map(index => Number(saved?.[index]) || 0);
    programmaticScroll.clear();
  } else if (!again) return;
  if (key) readerOffsets.forEach((offset,index) => setPaneScroll(index,offset));
}

function applyReaderMode(mode) {
  $('reading-area').dataset.mode=mode;
  for (const value of ['source','translation']) $('view-'+value).setAttribute('aria-pressed',String(value===mode));
}

function setReaderMode(mode) {
  if ($('reading-area').dataset.mode === mode) return;
  applyReaderMode(mode);
  restoreReaderBookmark(true);
  renderControls();
}

function toggleCompare() {
  const page = $('page-text');
  const entering = !isComparing();
  const activePane = $('reading-area').dataset.mode === 'source' ? 0 : 1;
  const progress = textPanes.map(pane => pane.clientHeight && scrollRange(pane) > 0 ?
    pane.scrollTop / scrollRange(pane) : null);
  page.dataset.layout = entering ? 'compare' : 'fragments';
  $('compare-mode').setAttribute('aria-pressed', String(entering));
  $('compare-mode').title = entering ? 'Вернуться к списку фрагментов' : 'Показать оригинал и перевод рядом';
  setFragmentRailOpen(false);
  if (!entering && state.editing) applyReaderMode('translation');
  for (const [index,pane] of textPanes.entries()) {
    const ratio = entering && $('sync-scroll').checked && progress[activePane] !== null ?
      progress[activePane] : progress[index];
    setPaneScroll(index, ratio === null ? readerOffsets[index] : ratio * scrollRange(pane));
  }
  if (!entering) revealFragmentInList();
  renderControls();
}

for (const mode of ['source','translation']) $('view-'+mode).onclick=()=>setReaderMode(mode);
$('compare-mode').onclick=toggleCompare;

for (const [index,pane] of textPanes.entries()) {
  pane.addEventListener('scroll', () => {
    const expected = programmaticScroll.get(pane);
    programmaticScroll.delete(pane);
    if (!readerScrollKey || readerScrollKey !== bookmarkKey() || !pane.clientHeight || pane.scrollTop === expected) return;
    readerOffsets[index] = pane.scrollTop;
    const target = textPanes[1-index];
    if (isComparing() && $('sync-scroll').checked && scrollRange(pane) > 0) {
      setPaneScroll(1-index, pane.scrollTop * scrollRange(target) / scrollRange(pane));
    }
    pref(readerScrollKey, readerOffsets.slice());
  }, {passive:true});
}

function setLibraryOpen(open) {
  $('library').classList.toggle('is-open',open);
  $('toggle-library').setAttribute('aria-expanded',String(open));
}

function setFragmentRailOpen(open) {
  $('fragments-panel').classList.toggle('is-open',open);
  $('toggle-fragments').setAttribute('aria-expanded',String(open));
  if (open) revealFragmentInList();
}

$('toggle-library').onclick=()=>{
  const open = !$('library').classList.contains('is-open');
  setFragmentRailOpen(false);
  setLibraryOpen(open);
  if (open) $('library-search').focus();
};
$('toggle-fragments').onclick=()=>setFragmentRailOpen(!$('fragments-panel').classList.contains('is-open'));
document.addEventListener('pointerdown', event => {
  if (!$('book-tools').contains(event.target)) $('book-tools').open=false;
  if (!$('library').contains(event.target) && !$('toggle-library').contains(event.target)) setLibraryOpen(false);
  if (!$('fragments-panel').contains(event.target) && !$('toggle-fragments').contains(event.target)) setFragmentRailOpen(false);
});
document.addEventListener('keydown', event => {
  if (event.key !== 'Escape' || document.querySelector('dialog[open]')) return;
  if ($('book-tools').open) { $('book-tools').open=false; $('book-tools').querySelector('summary').focus(); }
  if ($('library').classList.contains('is-open')) { setLibraryOpen(false); $('toggle-library').focus(); }
  if ($('fragments-panel').classList.contains('is-open')) { setFragmentRailOpen(false); $('toggle-fragments').focus(); }
});

window.addEventListener('beforeunload',e=>{if(state.dirty){e.preventDefault();e.returnValue='';}});
window.addEventListener('pagehide',flushPreferences);
async function poll(){await refresh();setTimeout(poll,2500);}
async function modelPoll(){await checkModel();setTimeout(modelPoll,20000);}
poll();modelPoll();
