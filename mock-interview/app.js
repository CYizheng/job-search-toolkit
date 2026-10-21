(function () {
  'use strict';

  var CFG = (window.INTERVIEW && window.INTERVIEW.config) || {};
  var ANSWER = CFG.answerSec || 90;
  var FA_ANSWER = CFG.followupAnswerSec || 60;

  var els = {};
  ['preview','camStatus','btnOpenCam','modeRow','prepRow','optNoLimit','optChain','optTTS','optHint','btnStart',
   'setup','stage','qPos','modeLabel','phaseLabel','timer','qType','qText','hintBox','hintBody',
   'checkPanel','checkBody','btnReplay','btnStopSeg','btnAbort','segList','result','resultDyn','btnAgain','btnReport',
   'selfView','toast','toastMsg','liveMetrics','liveDur','liveLevel'].forEach(function (id) {
    els[id] = document.getElementById(id);
  });

  var state = {
    stream: null,
    modeId: 'standard',
    prepSec: 10,
    queue: [],
    index: 0,
    phase: null,
    timerId: null,
    segStart: 0,
    segDur: 0,
    warningsInSeg: 0,
    recorder: null,
    chunks: [],
    segRecords: [],
    warnings: 0,
    lastWarningAt: 0,
    running: false,
    gen: 0,
    audioCtx: null,
    analyser: null,
    meterRAF: null,
    sessionId: 'sess-' + new Date().toISOString().replace(/[:.]/g, '-')
  };

  function qmap() {
    var map = {};
    (window.INTERVIEW.questions || []).forEach(function (q) { map[q.id] = q; });
    return map;
  }

  function fmt(sec) { return Math.max(0, Math.ceil(sec || 0)); }

  function fmtElapsed(s) {
    s = Math.max(0, Math.floor(s || 0));
    var m = Math.floor(s / 60);
    var r = s % 60;
    return (m < 10 ? '0' + m : m) + ':' + (r < 10 ? '0' + r : r);
  }

  function showToast(msg, ms) {
    els.toastMsg.textContent = msg;
    els.toast.classList.add('show');
    clearTimeout(showToast._t);
    showToast._t = setTimeout(function () { els.toast.classList.remove('show'); }, ms || 3000);
  }

  /* ---------- 纯音频编码 ---------- */
  function pickMime() {
    var c = ['audio/webm;codecs=opus', 'audio/webm', 'audio/ogg;codecs=opus', 'audio/mp4'];
    for (var i = 0; i < c.length; i++) {
      if (window.MediaRecorder && MediaRecorder.isTypeSupported(c[i])) return c[i];
    }
    return '';
  }

  function speak(text) {
    if (!els.optTTS.checked) return;
    if (!('speechSynthesis' in window)) return;
    try {
      window.speechSynthesis.cancel();
      var u = new SpeechSynthesisUtterance(text);
      u.lang = 'zh-CN';
      u.rate = 1.0;
      u.pitch = 1.0;
      window.speechSynthesis.speak(u);
    } catch (e) { /* ignore */ }
  }

  function stopSpeak() {
    if ('speechSynthesis' in window) { try { window.speechSynthesis.cancel(); } catch (e) {} }
  }

  /* ---------- 麦克风电平表（替代原来的摄像头预览） ---------- */
  function startMeter() {
    if (!state.stream) return;
    try {
      var AC = window.AudioContext || window.webkitAudioContext;
      state.audioCtx = new AC();
      var src = state.audioCtx.createMediaStreamSource(state.stream);
      state.analyser = state.audioCtx.createAnalyser();
      state.analyser.fftSize = 1024;
      state.analyser.smoothingTimeConstant = 0.75;
      src.connect(state.analyser);
      drawMeter();
    } catch (e) {
      console.error('meter failed', e);
    }
  }

  function drawMeter() {
    var cv = els.preview;
    if (!cv || !state.analyser) return;
    var ctx = cv.getContext('2d');
    var buf = new Uint8Array(state.analyser.fftSize);
    var W = cv.width, H = cv.height;

    function frame() {
      if (!state.analyser) return;
      state.analyser.getByteTimeDomainData(buf);
      var sum = 0;
      for (var i = 0; i < buf.length; i++) {
        var v = (buf[i] - 128) / 128;
        sum += v * v;
      }
      var rms = Math.sqrt(sum / buf.length);
      var level = Math.min(1, rms * 3.2);

      ctx.clearRect(0, 0, W, H);
      ctx.fillStyle = '#12121a';
      ctx.fillRect(0, 0, W, H);
      var barW = Math.floor(W * level);
      var grad = level < 0.08 ? '#4a4a55' : (level > 0.92 ? '#d64545' : '#2fa36b');
      ctx.fillStyle = grad;
      ctx.fillRect(10, H - 46, Math.max(2, barW - 20), 16);

      if (els.liveLevel) {
        var txt = level < 0.08 ? '音量 偏低（凑近话筒）'
                : (level > 0.92 ? '音量 过高（可能爆音）' : '音量 正常');
        els.liveLevel.textContent = txt;
      }
      state.meterRAF = requestAnimationFrame(frame);
    }
    frame();
  }

  function stopMeter() {
    if (state.meterRAF) { cancelAnimationFrame(state.meterRAF); state.meterRAF = null; }
    if (state.audioCtx) { try { state.audioCtx.close(); } catch (e) {} state.audioCtx = null; }
    state.analyser = null;
  }

  /* ---------- 设备 ---------- */
  els.btnOpenCam.addEventListener('click', async function () {
    try {
      state.stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, channelCount: 1 }
      });
      els.camStatus.textContent = '麦克风已开启 · 电平条随说话跳动即为正常';
      els.camStatus.classList.add('on');
      els.preview.classList.remove('off');
      els.btnStart.disabled = false;
      startMeter();
      showToast('✅ 麦克风已就绪，点【开始模拟面试】', 2000);
      window.addEventListener('beforeunload', function () {
        if (state.stream) state.stream.getTracks().forEach(function (t) { t.stop(); });
      });
    } catch (e) {
      console.error(e);
      els.camStatus.textContent = '授权失败：' + (e.name || e.message) + '（请确认浏览器允许麦克风）';
      showToast('❌ 无法打开麦克风，请检查浏览器授权', 3200);
    }
  });

  /* ---------- 模式 / 节奏 ---------- */
  (function buildModes() {
    var modes = window.INTERVIEW.modes || {};
    var desc = document.getElementById('modeDesc');

    function showDesc(key) {
      if (!desc) return;
      var m = modes[key] || {};
      var cfgD = (window.INTERVIEW.config || {}).chainDepth || 2;
      var pool, depthTxt;
      if (m.ids) {
        pool = '固定 ' + m.ids.length + ' 题';
        depthTxt = '追问 ' + (m.chainDepth != null ? m.chainDepth : cfgD) + ' 层';
      } else if (m.mix) {
        pool = '混合 ' + m.mix.length + ' 段';
        var ds = m.mix.map(function (s) {
          return (s.chainDepth != null) ? s.chainDepth : (m.chainDepth != null ? m.chainDepth : cfgD);
        });
        var lo = Math.min.apply(null, ds), hi = Math.max.apply(null, ds);
        depthTxt = '追问 ' + (lo === hi ? lo : lo + '–' + hi) + ' 层（按段）';
      } else {
        pool = '从「' + (m.tracks || []).join('／') + '」随机抽 ' + (m.pick || 0) + ' 题';
        depthTxt = '追问 ' + (m.chainDepth != null ? m.chainDepth : cfgD) + ' 层';
      }
      desc.innerHTML = '<b>' + (m.label || '') + '</b>　' + pool + '　' + depthTxt + '<br>' + (m.desc || '');
    }

    Object.keys(modes).forEach(function (key) {
      var label = document.createElement('label');
      var radio = document.createElement('input');
      radio.type = 'radio';
      radio.name = 'mode';
      radio.value = key;
      label.appendChild(radio);
      label.appendChild(document.createTextNode(' ' + modes[key].label));
      if (key === state.modeId) { radio.checked = true; label.classList.add('sel'); }
      radio.addEventListener('change', function () {
        state.modeId = key;
        els.modeRow.querySelectorAll('label').forEach(function (l) { l.classList.remove('sel'); });
        label.classList.add('sel');
        showDesc(key);
      });
      els.modeRow.appendChild(label);
    });
    showDesc(state.modeId);
  })();

  els.prepRow.addEventListener('change', function (e) {
    if (e.target.name === 'prep') {
      state.prepSec = parseInt(e.target.value, 10) || 10;
      els.prepRow.querySelectorAll('label').forEach(function (l) { l.classList.remove('sel'); });
      e.target.parentElement.classList.add('sel');
    }
  });

  /* ---------- 面试流程 ---------- */
  els.btnStart.addEventListener('click', function () {
    buildQueue();
    els.setup.style.display = 'none';
    els.stage.classList.add('on');
    els.selfView.style.display = 'block';
    els.modeLabel.textContent = (window.INTERVIEW.modes[state.modeId] || {}).label
      + (els.optChain.checked ? ' + 链式追问' : '')
      + (els.optNoLimit.checked ? ' · 不限时作答' : '');
    state.running = true;
    state.gen++;
    runQueueItem();
  });

  /* ---------- 取题：支持 ids（固定）/ tracks+pick（随机）/ mix（多段混合） ---------- */
  function allQuestions() { return (window.INTERVIEW && window.INTERVIEW.questions) || []; }

  function shuffle(a) {
    for (var i = a.length - 1; i > 0; i--) {
      var j = Math.floor(Math.random() * (i + 1));
      var t = a[i]; a[i] = a[j]; a[j] = t;
    }
    return a;
  }

  function globalDepth() {
    var c = (window.INTERVIEW && window.INTERVIEW.config) || {};
    return (c.chainDepth != null) ? c.chainDepth : 2;
  }

  // 把一段规则解析成题目数组（tracks+pick 会重新随机抽一次；skip 用于跨段去重）
  function resolveSeg(seg, skip) {
    var map = qmap();
    var used = skip || {};
    if (seg.ids && seg.ids.length) {
      return seg.ids.map(function (id) { return map[id]; })
        .filter(function (q) { return q && !used[q.id]; });
    }
    var tracks = seg.tracks || [];
    var exclude = seg.exclude || [];
    var pool = allQuestions().filter(function (q) {
      return tracks.indexOf(q.track) >= 0 && !used[q.id] && !q.noask
          && exclude.indexOf(q.id) < 0;
    });
    shuffle(pool);
    // uniqueGrp：同一段内不重复出「同一组」的题（软问题用，避免一场里问两个同组问题）
    if (seg.uniqueGrp) {
      var seenGrp = {}, keep = [];
      pool.forEach(function (q) {
        var g = q.grp || q.id;
        if (seenGrp[g]) return;
        seenGrp[g] = 1;
        keep.push(q);
      });
      pool = keep;
    }
    var n = seg.pick || pool.length;
    return pool.slice(0, n);
  }

  // 展开成 [{q, depth}]，depth = 这条链往下追几层；同场不重复出题
  function resolvePicks() {
    var M = (window.INTERVIEW.modes || {})[state.modeId] || {};
    var segs = M.mix ? M.mix : [M];
    var out = [], used = {};
    segs.forEach(function (sg) {
      var d = (sg.chainDepth != null) ? sg.chainDepth
            : (M.chainDepth != null ? M.chainDepth : globalDepth());
      resolveSeg(sg, used).forEach(function (q) {
        if (used[q.id]) return;
        used[q.id] = 1;
        out.push({ q: q, depth: d });
      });
    });
    return out;
  }

  function buildQueue() {
    var picks = resolvePicks();
    state.queue = [];
    state.index = 0;
    state.segRecords = [];
    state.warnings = 0;

    picks.forEach(function (p) {
      var q = p.q;
      if (!q) return;
      state.queue.push({ q: q, isFollowup: false, seqLabel: q.label });
      if (!els.optChain.checked || p.depth <= 0) return;

      // 兼容两种写法：q.chain（对象数组）/ q.followups（字符串数组）
      var chain = q.chain || (q.followups || []).map(function (f) {
        return (typeof f === 'string') ? { q: f } : f;
      });
      if (!chain.length) return;

      var use = Math.min(chain.length, p.depth);
      chain.slice(0, use).forEach(function (c, i) {
        state.queue.push({
          q: {
            label: q.label + ' · 追' + (i + 1),
            type: '链式追问 ' + (i + 1) + '/' + use + (use < chain.length ? '（该链共 ' + chain.length + ' 层）' : ''),
            question: c.q,
            hint: c.hint || '',
            check: c.check || [],
            followups: []
          },
          isFollowup: true,
          seqLabel: q.label + '-追' + (i + 1)
        });
      });
    });
  }

  function runQueueItem() {
    if (!state.running) return;
    if (state.index >= state.queue.length) return finishInterview(false);
    var item = state.queue[state.index];
    var total = state.queue.length;
    els.qPos.textContent = '第 ' + (state.index + 1) + ' / ' + total + ' 题' + (item.isFollowup ? ' · 追问' : '');
    els.qType.textContent = item.q.type || '简历深挖';
    els.qText.textContent = item.q.question;
    els.hintBody.textContent = item.q.hint || '';
    els.hintBox.style.display = (els.optHint.checked && item.q.hint) ? '' : 'none';
    els.hintBox.open = true;
    els.checkPanel.style.display = 'none';
    renderChecks(item.q.check || []);
    els.segList.innerHTML = '';
    state.segRecords.forEach(function (r, i) { addSegRow(els.segList, r, i + 1); });

    var prep = item.isFollowup ? (CFG.followupPrepSec || 5) : (state.prepSec || 10);
    var ans = item.isFollowup ? FA_ANSWER : ANSWER;
    els.btnStopSeg.style.display = 'none';
    els.liveDur.textContent = '时长 00:00';
    startPhase('prep', prep, function () {
      startPhase('answer', ans, function () {
        finishSegment();
      });
    });
    if (els.optTTS.checked) {
      setTimeout(function () { speak('请听题：' + item.q.question); }, 300);
    }
  }

  function renderChecks(items) {
    els.checkBody.innerHTML = '';
    items.forEach(function (t) {
      var d = document.createElement('div');
      d.textContent = '☐ ' + t;
      d.style.marginBottom = '2px';
      els.checkBody.appendChild(d);
    });
  }

  function startPhase(name, sec, onDone) {
    state.phase = name;
    var noLimit = name === 'answer' && els.optNoLimit.checked;
    if (name === 'answer') {
      stopSpeak();
      els.hintBox.open = false;
      els.hintBox.style.display = 'none';
      els.btnStopSeg.style.display = 'inline-block';
      state.segStart = performance.now();
      startRecorder();
    } else {
      els.hintBox.open = true;
      els.hintBox.style.display = (els.optHint.checked && state.queue[state.index] && state.queue[state.index].q.hint) ? '' : 'none';
    }
    var gen = state.gen;
    clearInterval(state.timerId);

    if (noLimit) {
      els.phaseLabel.textContent = '作答中 · 正在录音 · 不限时（讲完点"完成本题"）';
      els.phaseLabel.className = 'phase answer';
      els.timer.textContent = '00:00';
      var start = performance.now();
      state.soft90 = false;
      state.soft150 = false;
      state.timerId = setInterval(function () {
        var s = Math.floor((performance.now() - start) / 1000);
        els.timer.textContent = fmtElapsed(s);
        els.liveDur.textContent = '时长 ' + fmtElapsed(s);
        if (s >= 90 && !state.soft90) { state.soft90 = true; showToast('⏳ 已讲 90 秒，可以开始收尾了（不限时，按需继续）', 3000); }
        if (s >= 150 && !state.soft150) { state.soft150 = true; showToast('⏳ 已讲 150 秒，如果还在展开请尽快落到结果上', 3000); }
      }, 500);
      return;
    }

    if (name === 'answer') {
      els.phaseLabel.textContent = '作答中 · 正在录音';
      els.phaseLabel.className = 'phase answer';
    } else {
      els.phaseLabel.textContent = '准备思考（可看不限时关键词提示，不要写稿）';
      els.phaseLabel.className = 'phase prep';
    }
    var end = performance.now() + sec * 1000;
    state.timerId = setInterval(function () {
      var remain = fmt((end - performance.now()) / 1000);
      els.timer.textContent = remain;
      els.timer.classList.toggle('warn', remain <= 10 && name === 'answer');
      if (name === 'answer') {
        var used = Math.floor((performance.now() - state.segStart) / 1000);
        els.liveDur.textContent = '时长 ' + fmtElapsed(used);
      }
      if (remain <= 0) {
        clearInterval(state.timerId);
        els.timer.classList.remove('warn');
        if (state.gen === gen) onDone();
      }
    }, 200);
    els.timer.textContent = sec;
    els.timer.classList.remove('warn', 'done');
  }

  els.btnStopSeg.addEventListener('click', function () {
    if (state.phase === 'answer') finishSegment();
  });

  els.btnReplay.addEventListener('click', function () {
    var item = state.queue[state.index];
    if (item) speak('请听题：' + item.q.question);
  });

  els.btnAbort.addEventListener('click', function () {
    finishInterview(true);
  });

  /* ---------- 录音（纯音频） ---------- */
  function startRecorder() {
    if (!state.stream) return;
    try {
      var opts = { audioBitsPerSecond: 96000, audioChannelCount: 1 };
      var mime = pickMime();
      if (mime) opts.mimeType = mime;
      state.chunks = [];
      state.recorder = new MediaRecorder(state.stream, opts);
      state.recorder.ondataavailable = function (e) { if (e.data && e.data.size) state.chunks.push(e.data); };
      state.recorder.start(1000);
    } catch (e) {
      console.error(e);
      showToast('⚠️ 录音启动失败（不影响回答，但无法留存片段）', 3000);
    }
  }

  function finishSegment() {
    if (state.phase !== 'answer') return;
    state.phase = null;
    clearInterval(state.timerId);
    els.timer.textContent = '✓';
    els.timer.classList.add('done');
    els.phaseLabel.textContent = '本题结束';
    els.phaseLabel.className = 'phase done';
    els.btnStopSeg.style.display = 'none';
    els.checkPanel.style.display = '';
    var item = state.queue[state.index];
    var duration = performance.now() - state.segStart;
    var warnings = state.warningsInSeg || 0;
    var rec = state.recorder;
    state.recorder = null;
    state.warnings += warnings;
    state.warningsInSeg = 0;

    if (rec && rec.state !== 'inactive') {
      rec.onstop = function () {
        if (!state.running) return;
        var blob = new Blob(state.chunks, { type: rec.mimeType || 'audio/webm' });
        var record = { label: item.seqLabel, duration: duration, warnings: warnings, blob: blob };
        state.segRecords.push(record);
        uploadBlob(blob, item.seqLabel, duration, warnings, item.isFollowup);
        addSegRow(els.segList, record, state.segRecords.length);
        afterSegment();
      };
      rec.stop();
    } else {
      var record2 = { label: item.seqLabel, duration: duration, warnings: warnings, blob: null };
      state.segRecords.push(record2);
      addSegRow(els.segList, record2, state.segRecords.length);
      afterSegment();
    }
  }

  function afterSegment() {
    setTimeout(function () {
      if (!state.running) return;
      state.index++;
      runQueueItem();
    }, 3800);
  }

  function uploadBlob(blob, label, duration, warnings, isFollowup) {
    try {
      var fd = new FormData();
      fd.append('qid', label);
      fd.append('blob', blob, label + '.webm');
      fetch('/api/upload', { method: 'POST', body: fd }).catch(function () {});
    } catch (e) {}
    try {
      fetch('/api/meta', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          session: state.sessionId,
          mode: state.modeId,
          noLimit: !!els.optNoLimit.checked,
          qid: label,
          isFollowup: !!isFollowup,
          durationSec: Math.round(duration / 1000 * 10) / 10,
          warnings: warnings,
          blobMB: blob ? Math.round(blob.size / 1048576 * 100) / 100 : 0,
          media: 'audio-only',
          mime: pickMime(),
          userAgent: navigator.userAgent
        })
      }).catch(function () {});
    } catch (e) {}
  }

  function addSegRow(listEl, r, n) {
    var empty = listEl.querySelector('.muted');
    if (empty) empty.remove();
    var div = document.createElement('div');
    div.className = 'seg-item';
    var left = document.createElement('span');
    left.textContent = '#' + n + ' ' + r.label;
    left.className = 'ok-dot';
    var right = document.createElement('span');
    right.textContent = (r.duration / 1000).toFixed(1) + ' 秒' + (r.warnings > 0 ? (' ｜ 切屏/失焦 ' + r.warnings + ' 次') : '');
    if (r.warnings > 0) right.className = 'warn-dot';
    div.appendChild(left);
    div.appendChild(right);
    listEl.appendChild(div);
  }

  function finishInterview(aborted) {
    stopEverything();
    els.resultDyn.innerHTML = '';
    var title = document.createElement('h2');
    title.style.marginBottom = '10px';
    title.textContent = aborted ? '⏹ 面试已终止' : '🏁 模拟面试完成，共 ' + state.segRecords.length + ' 段';
    els.resultDyn.appendChild(title);
    var totalDur = state.segRecords.reduce(function (a, r) { return a + r.duration; }, 0);
    var sum = document.createElement('p');
    sum.className = 'muted';
    sum.textContent = '总作答时长 ' + (totalDur / 1000).toFixed(0) + ' 秒 ｜ 切屏/失焦 '
      + state.warnings + ' 次 ｜ 音频已存到本机 recordings/。下一步：双击 run_analyze.bat 出体检报告。';
    els.resultDyn.appendChild(sum);
    var list = document.createElement('div');
    list.className = 'seg-list';
    state.segRecords.forEach(function (r, i) { addSegRow(list, r, i + 1); });
    els.resultDyn.appendChild(list);
    els.result.style.display = 'block';
    els.stage.classList.remove('on');
    els.selfView.style.display = 'none';
    els.btnReport.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }

  function stopEverything() {
    state.running = false;
    state.gen++;
    clearInterval(state.timerId);
    stopSpeak();
    if (state.recorder && state.recorder.state !== 'inactive') {
      try { state.recorder.stop(); } catch (e) {}
    }
    state.recorder = null;
    state.phase = null;
    els.btnStopSeg.style.display = 'none';
  }

  els.btnAgain.addEventListener('click', function () {
    els.result.style.display = 'none';
    els.setup.style.display = '';
    els.stage.classList.remove('on');
    buildQueue();
    els.preview.classList.remove('off');
    els.camStatus.textContent = '麦克风已开启 · 电平条随说话跳动即为正常';
    startMeter();
  });

  els.btnReport.addEventListener('click', function () {
    showToast('① 确认 recordings/ 里有 .webm 音频；② 双击 run_analyze.bat；③ 打开 analysis_report.md', 5000);
  });

  /* ---------- 切屏检测（保留） ---------- */
  function onBlurEvidence() {
    if (!state.running) return;
    if (state.phase !== 'prep' && state.phase !== 'answer') return;
    var now = Date.now();
    if (now - state.lastWarningAt < 5000) return;
    state.lastWarningAt = now;
    state.warningsInSeg = (state.warningsInSeg || 0) + 1;
    showToast('⚠️ 【切屏检测】检测到页面失焦 / 切屏，计入违规记录（正式面试不允许）', 3200);
  }

  document.addEventListener('visibilitychange', function () {
    if (document.hidden) onBlurEvidence();
  });
  window.addEventListener('blur', function () {
    if (!document.hidden) onBlurEvidence();
  });
})();
