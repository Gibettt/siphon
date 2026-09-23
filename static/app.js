let currentJobId = null;
let pollInterval = null;
let currentMode = 'normal';

function selectMode(mode) {
  currentMode = mode;

  document.querySelectorAll('.mode-tab').forEach(t => t.classList.remove('active'));
  document.querySelector(`.mode-tab[data-mode="${mode}"]`).classList.add('active');

  document.getElementById('normalOptions').style.display = mode === 'normal' ? '' : 'none';
  document.getElementById('deepOptions').style.display = mode === 'deep' ? '' : 'none';
  document.getElementById('completeOptions').style.display = mode === 'complete' ? '' : 'none';

  const btn = document.getElementById('startBtn');
  const labels = { normal: 'Download', deep: 'Deep Download', complete: 'Complete Download' };
  btn.lastChild.textContent = ' ' + labels[mode];
}

async function startCrawl() {
  const url = document.getElementById('urlInput').value.trim();
  const errDiv = document.getElementById('errorMsg');

  if (!url) {
    errDiv.textContent = 'URL harus diisi';
    document.getElementById('urlInput').focus();
    return;
  }
  errDiv.textContent = '';

  const maxPages = parseInt(document.getElementById('maxPages').value) || 30;
  const maxAssets = parseInt(document.getElementById('maxAssets').value) || 300;
  const waitSeconds = parseInt(document.getElementById('waitSeconds').value) || 15;
  const crawlIframes = document.getElementById('crawlIframes').checked;
  const completeWait = parseInt(document.getElementById('completeWait').value) || 12;
  const completeMaxPages = parseInt(document.getElementById('completeMaxPages').value) || 1;
  const followLinks = document.getElementById('followLinks').checked;
  const isComplete = currentMode === 'complete';
  const isDeep = currentMode === 'deep';

  document.getElementById('startBtn').disabled = true;
  const result = document.getElementById('resultCard');
  result.classList.add('show');
  result.style.display = 'block';

  document.getElementById('detectionSection').classList.remove('visible');
  document.getElementById('statsSection').classList.remove('visible');
  document.getElementById('downloadBtn').classList.remove('visible');
  document.getElementById('logBox').innerHTML = '';
  document.getElementById('progressTrack').classList.add('visible');
  document.getElementById('progressFill').className = 'progress-fill indeterminate';
  setStatus('queued');
  showSpinner(true);

  result.scrollIntoView({ behavior: 'smooth', block: 'start' });

  try {
    const res = await fetch('/api/crawl', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        url,
        max_pages: isComplete ? completeMaxPages : maxPages,
        max_assets: maxAssets,
        deep_mode: isDeep,
        wait_seconds: isComplete ? completeWait : waitSeconds,
        crawl_iframes: crawlIframes,
        complete_mode: isComplete,
        follow_links: followLinks,
      })
    });
    const data = await res.json();

    if (data.status) {
      // Vercel: synchronous response — job already finished
      currentJobId = data.id;
      updateUI(data);
      document.getElementById('startBtn').disabled = false;
      showSpinner(false);
      const fill = document.getElementById('progressFill');
      fill.classList.remove('indeterminate');
      fill.style.width = '100%';
      if (data.status === 'error') fill.style.background = 'var(--red)';
    } else {
      // Local: async response — start polling
      currentJobId = data.job_id;
      startPolling();
    }
  } catch (e) {
    errDiv.textContent = 'Gagal memulai: ' + e.message;
    document.getElementById('startBtn').disabled = false;
    showSpinner(false);
    document.getElementById('progressTrack').classList.remove('visible');
  }
}

function startPolling() {
  if (pollInterval) clearInterval(pollInterval);
  pollInterval = setInterval(pollJob, 1500);
}

async function pollJob() {
  if (!currentJobId) return;
  try {
    const res = await fetch('/api/job/' + currentJobId);
    const job = await res.json();
    updateUI(job);
    if (job.status === 'done' || job.status === 'error') {
      clearInterval(pollInterval);
      document.getElementById('startBtn').disabled = false;
      showSpinner(false);

      const fill = document.getElementById('progressFill');
      fill.classList.remove('indeterminate');
      fill.style.width = '100%';
      if (job.status === 'error') fill.style.background = 'var(--red)';
    }
  } catch (e) {}
}

function updateUI(job) {
  setStatus(job.status);

  const logBox = document.getElementById('logBox');
  logBox.innerHTML = (job.log || []).map(l =>
    `<div class="log-line">${escHtml(l)}</div>`
  ).join('');
  logBox.scrollTop = logBox.scrollHeight;

  if (job.detection) {
    const section = document.getElementById('detectionSection');
    section.classList.add('visible');
    const chips = document.getElementById('techChips');
    const all = job.detection.all || [];
    chips.innerHTML = all.map((f, i) =>
      `<span class="tech-chip ${i === 0 ? 'primary' : ''}">${f}</span>`
    ).join('');
  }

  if (job.stats) {
    document.getElementById('statsSection').classList.add('visible');
    document.getElementById('statPages').textContent = job.stats.pages ?? '-';
    document.getElementById('statAssets').textContent = job.stats.assets ?? '-';
    document.getElementById('statSize').textContent = job.stats.total_kb ?? '-';
    document.getElementById('statTime').textContent = job.stats.elapsed ?? '-';

    const extras = [
      ['statPostSlots', 'post_slots', 'POST Slots', 'extra'],
      ['statChunks', 'webpack_chunks', 'JS Chunks', 'extra'],
      ['statCssAssets', 'css_assets', 'CSS Assets', 'extra'],
      ['statReferenced', 'referenced_assets', 'HTML Refs', 'extra'],
    ];
    for (const [id, key, label, cls] of extras) {
      if (job.stats[key] === undefined) continue;
      if (!document.getElementById(id)) {
        const box = document.createElement('div');
        box.className = `stat-box ${cls}`;
        box.innerHTML = `<div class="stat-val" id="${id}">-</div><div class="stat-label">${label}</div>`;
        document.querySelector('.stats-grid').appendChild(box);
      }
      document.getElementById(id).textContent = job.stats[key];
    }

    if (job.stats.threed_assets !== undefined) {
      if (!document.getElementById('stat3d')) {
        const box = document.createElement('div');
        box.className = 'stat-box extra-purple';
        box.innerHTML = '<div class="stat-val" id="stat3d">-</div><div class="stat-label">3D Assets</div>';
        document.querySelector('.stats-grid').appendChild(box);
      }
      document.getElementById('stat3d').textContent = job.stats.threed_assets;
    }
  }

  if (job.status === 'done') {
    const btn = document.getElementById('downloadBtn');
    btn.classList.add('visible');
    btn.querySelector('svg').nextSibling.textContent = ` Download ZIP (${job.zip_size_kb} KB)`;
  }
}

function setStatus(status) {
  const badge = document.getElementById('statusBadge');
  badge.className = 'badge badge-' + status;
  const labels = {
    queued: 'Antrian',
    running: 'Berjalan...',
    done: 'Selesai',
    error: 'Error'
  };
  badge.textContent = labels[status] || status;
}

function showSpinner(show) {
  document.getElementById('spinner').classList.toggle('visible', show);
}

function downloadZip() {
  if (currentJobId) window.location.href = '/api/download/' + currentJobId;
}

function escHtml(str) {
  const d = document.createElement('div');
  d.textContent = str;
  return d.innerHTML;
}

document.addEventListener('DOMContentLoaded', () => {
  selectMode('normal');
  document.getElementById('urlInput').addEventListener('keydown', e => {
    if (e.key === 'Enter') { e.preventDefault(); startCrawl(); }
  });
});
