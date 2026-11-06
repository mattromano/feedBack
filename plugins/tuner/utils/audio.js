(function() {
    const _TUNER_MIN_YIN_SAMPLES = 4096;
    const _TUNER_FRAME_SIZE = 2048;
    const _TUNER_MIN_DETECTABLE_HZ = 20;
    const _FREQ_HISTORY_LEN = 3;
    const _WARMUP_FRAMES = 2;
    // If a frame is posted to the worker and no message/error comes back within
    // this window, clear the in-flight guard so the poll loop can't latch shut.
    const _FRAME_WATCHDOG_MS = 500;

    let _audioCtx = null;
    let _sourceNode = null;
    let _stream = null;
    let _processor = null;
    let _gainNode = null;
    let _accumBuffer = new Float32Array(0);
    let _pendingBuffer = null;
    let _detectInterval = null;
    let _processingFrame = false;
    let _yinWorker = null;
    let _freqHistory = [];
    let _validFrameCount = 0;
    let _lastFreq = 0;
    let _onResult = null;
    let _usingDesktopBridge = false;
    let _bridgeInterval = null;
    // Bumped on every start/stop. An async start captures the value and aborts
    // if it changes mid-flight, so a re-entrant start()/restart() can't orphan
    // a worker + interval created by a superseded call.
    let _startGen = 0;
    // Timestamp of the last frame posted to the worker (watchdog, see above).
    let _frameSentAt = 0;

    function _octaveFold(freq, ref) {
        if (!ref || freq <= 0) return freq;
        while (freq > ref * 1.414) freq /= 2;
        while (freq < ref / 1.414) freq *= 2;
        return freq;
    }

    function _median(arr) {
        if (!arr.length) return 0;
        var s = arr.slice().sort(function(a, b) { return a - b; });
        var mid = Math.floor(s.length / 2);
        return s.length % 2 !== 0 ? s[mid] : (s[mid - 1] + s[mid]) / 2;
    }

    // ── Note lock with hysteresis + decay hold ───────────────────────────────
    // A plucked string decays 20-30 dB in its first second. The old logic used
    // one absolute level (-40 dBFS RMS) to both start and end a reading, and a
    // single quiet or unclear frame (30 ms) blanked the display, so on a
    // low-gain USB guitar cable the reading died after ~0.3-0.8 s while the
    // string was still ringing. Now:
    //   - acquire: a clear frame above _ON_RMS (-44 dBFS);
    //   - stay locked while the frame is above the higher of an absolute floor
    //     (-62 dBFS) and 34 dB below the note's own peak, with a laxer clarity
    //     bar, and within a semitone of the locked pitch (octave-folded);
    //   - a bad frame doesn't blank: after _MISS_FRAMES in a row the last
    //     reading is held for _HOLD_MS, then cleared;
    //   - a new pluck (+6 dB jump) or a clear new pitch re-locks without a blank.
    const _ON_RMS = 0.006;
    const _OFF_RMS_ABS = 0.0008;
    const _OFF_REL = 0.02;
    const _ON_CONF = 0.5;
    const _OFF_CONF = 0.25;
    const _LOCK_CENTS = 100;
    const _MISS_FRAMES = 3;
    const _HOLD_MS = 600;
    const _LOCKED_HISTORY_LEN = 5;
    let _locked = false;
    let _peakRms = 0;
    let _missCount = 0;
    let _holdUntil = 0;

    function _centsBetween(a, b) {
        return (a > 0 && b > 0) ? 1200 * Math.log2(a / b) : Infinity;
    }

    function _resetLock() {
        _locked = false; _peakRms = 0; _missCount = 0; _holdUntil = 0;
        _validFrameCount = 0; _freqHistory = []; _lastFreq = 0;
    }

    function _emit(smoothedFreq, rms, hasSignal) {
        if (_onResult) _onResult({ smoothedFreq, rms, hasSignal });
    }

    function _handleYinResult(result) {
        const now = Date.now();
        const rms = result ? (result.rms || 0) : 0;
        const conf = result ? (result.confidence || 0) : 0;
        const freq = result ? (result.freq || 0) : 0;
        const pitched = freq >= _TUNER_MIN_DETECTABLE_HZ;

        if (!_locked) {
            // ── Acquire ──
            if (!pitched || rms <= _ON_RMS || conf < _ON_CONF) {
                _validFrameCount = 0; _freqHistory = [];
                _emit(null, rms, false);
                return;
            }
            _freqHistory.push(_octaveFold(freq, _lastFreq));
            if (_freqHistory.length > _FREQ_HISTORY_LEN) _freqHistory.shift();
            _validFrameCount++;
            if (_validFrameCount <= _WARMUP_FRAMES) { _emit(null, rms, true); return; }
            _locked = true; _peakRms = rms; _missCount = 0; _holdUntil = 0;
            _lastFreq = _median(_freqHistory);
            _emit(_lastFreq, rms, true);
            return;
        }

        // ── Locked ──
        const folded = pitched ? _octaveFold(freq, _lastFreq) : 0;
        const offRms = Math.max(_OFF_RMS_ABS, _peakRms * _OFF_REL);
        const near = pitched && Math.abs(_centsBetween(folded, _lastFreq)) <= _LOCK_CENTS;
        // A fresh pluck or a clearly different note (another string): re-lock
        // on it straight away, without blanking in between.
        const repluck = pitched && rms > _peakRms * 2 && conf >= _ON_CONF;
        const newNote = pitched && !near && rms > _ON_RMS && conf >= _ON_CONF;
        if (newNote || (repluck && !near)) {
            _freqHistory = [freq]; _lastFreq = freq; _peakRms = rms; _missCount = 0; _holdUntil = 0;
            _emit(freq, rms, true);
            return;
        }
        if (near && rms > offRms && conf >= _OFF_CONF) {
            _peakRms = Math.max(_peakRms * 0.999, rms);
            _missCount = 0; _holdUntil = 0;
            _freqHistory.push(folded);
            while (_freqHistory.length > _LOCKED_HISTORY_LEN) _freqHistory.shift();
            _lastFreq = _median(_freqHistory);
            _emit(_lastFreq, rms, true);
            return;
        }
        // A bad frame: keep showing the last stable pitch, then hold, then clear.
        _missCount++;
        if (_missCount >= _MISS_FRAMES && !_holdUntil) _holdUntil = now + _HOLD_MS;
        if (_holdUntil && now >= _holdUntil) {
            _resetLock();
            _emit(null, rms, false);
            return;
        }
        _emit(_lastFreq, rms, true);
    }

    // True while a frame is being processed by the worker, unless that frame is
    // older than the watchdog window — in which case the worker is assumed wedged
    // and the guard is released so the poll loop can recover.
    function _frameBusy() {
        if (!_processingFrame) return false;
        if (Date.now() - _frameSentAt > _FRAME_WATCHDOG_MS) { _processingFrame = false; return false; }
        return true;
    }

    async function _tryBridgeStart(audioInputMode, myGen) {
        if (audioInputMode === 'browser') return false;
        var desktop = (typeof window !== 'undefined') ? window.feedBackDesktop : null;
        if (!desktop || !desktop.isDesktop || !desktop.audio
            || typeof desktop.audio.isAvailable !== 'function') return false;

        var available = false;
        try { available = await desktop.audio.isAvailable(); } catch (_) {}
        if (myGen !== _startGen) return false;
        if (!available) return false;

        // The tuner runs its own tuning-optimised YIN over the raw sample frame.
        // The engine's getRawPitch endpoint is deliberately NOT used as a
        // fallback — it produces a jittery readout. A build without
        // getRawAudioFrame can't feed our pipeline, so fall back to getUserMedia
        // instead of claiming the bridge.
        if (typeof desktop.audio.getRawAudioFrame !== 'function') return false;

        var started = false;
        try {
            var running = typeof desktop.audio.isAudioRunning === 'function'
                ? await desktop.audio.isAudioRunning() : false;
            if (!running && typeof desktop.audio.startAudio === 'function') {
                await desktop.audio.startAudio();
                started = true;
            }
        } catch (e) {
            // A failed startAudio means frames will never arrive — surface it
            // rather than silently claiming a dead bridge.
            console.warn('[tuner] bridge startAudio failed:', e && e.message ? e.message : e);
        }
        if (myGen !== _startGen) {
            // Superseded by a newer start/stop while awaiting — undo any engine
            // start we triggered and bail without claiming the bridge.
            if (started && typeof desktop.audio.stopAudio === 'function') {
                try { desktop.audio.stopAudio(); } catch (_) {}
            }
            return false;
        }

        var bridgeSampleRate = 48000;
        try {
            if (typeof desktop.audio.getSampleRate === 'function') {
                var sr = await desktop.audio.getSampleRate();
                if (typeof sr === 'number' && Number.isFinite(sr) && sr > 0) bridgeSampleRate = sr;
            }
        } catch (_) {}
        if (myGen !== _startGen) return false;

        _usingDesktopBridge = true;
        console.log('[tuner] using desktop JUCE bridge with raw audio + YIN');

        _yinWorker = new Worker('/api/plugins/tuner/workers/yin.js');
        _yinWorker.onmessage = function(e) { _handleYinResult(e.data); _processingFrame = false; };
        _yinWorker.onerror = function(e) { console.error('Tuner: YIN worker error', e); _processingFrame = false; };

        _bridgeInterval = setInterval(async function() {
            if (_frameBusy() || !_yinWorker) return;
            try {
                var samples = await desktop.audio.getRawAudioFrame(_TUNER_MIN_YIN_SAMPLES);
                if (!_yinWorker) return; // torn down while awaiting the frame
                if (!(samples instanceof Float32Array) || samples.length < _TUNER_MIN_YIN_SAMPLES) return;
                // Copy rather than transfer: the engine may hand back a view onto
                // a buffer it reuses, and transferring would detach it. A 4096-
                // sample copy every 30 ms is negligible.
                var frame = samples.slice();
                _processingFrame = true;
                _frameSentAt = Date.now();
                _yinWorker.postMessage({ samples: frame, sampleRate: bridgeSampleRate }, [frame.buffer]);
            } catch (e) {
                console.warn('[tuner] bridge raw audio poll failed:', e && e.message ? e.message : e);
                if (_onResult) _onResult({ smoothedFreq: null, rms: 0, hasSignal: false });
            }
        }, 30);

        return true;
    }

    async function _doStart(deviceId, channel, audioInputMode) {
        // Tear down any existing session first so a double start() can't orphan a
        // worker/interval; _doStop bumps _startGen, which also aborts any start
        // still suspended on an await.
        _doStop();
        const myGen = _startGen;

        var bridgeStarted = await _tryBridgeStart(audioInputMode || 'auto', myGen);
        if (myGen !== _startGen) return; // superseded while probing the bridge
        if (bridgeStarted) return;
        const constraints = {
            audio: { echoCancellation: false, noiseSuppression: false, autoGainControl: false, channelCount: 2 }
        };
        if (deviceId) constraints.audio.deviceId = { exact: deviceId };

        try {
            _stream = await navigator.mediaDevices.getUserMedia(constraints);
        } catch (e) {
            if (e.name === 'OverconstrainedError' && deviceId) {
                delete constraints.audio.deviceId;
                delete constraints.audio.channelCount;
            } else if (e.name === 'NotFoundError' && deviceId) {
                delete constraints.audio.deviceId;
            } else if (e.name === 'OverconstrainedError') {
                delete constraints.audio.channelCount;
            } else {
                throw e;
            }
            _stream = await navigator.mediaDevices.getUserMedia(constraints);
        }

        if (myGen !== _startGen) {
            // Superseded while awaiting mic permission — release the stream.
            if (_stream) { _stream.getTracks().forEach(t => t.stop()); _stream = null; }
            return;
        }

        _audioCtx = new (window.AudioContext || window.webkitAudioContext)();
        _sourceNode = _audioCtx.createMediaStreamSource(_stream);
        _gainNode = _audioCtx.createGain();
        _gainNode.gain.value = 1.0;

        if (_sourceNode.channelCount >= 2 && channel !== 'mono') {
            const splitter = _audioCtx.createChannelSplitter(2);
            const merger = _audioCtx.createChannelMerger(1);
            _sourceNode.connect(splitter);
            splitter.connect(merger, channel === 'left' ? 0 : 1, 0);
            merger.connect(_gainNode);
        } else {
            _sourceNode.connect(_gainNode);
        }

        _processor = _audioCtx.createScriptProcessor(_TUNER_FRAME_SIZE, 1, 1);
        _processor.onaudioprocess = (e) => {
            const input = e.inputBuffer.getChannelData(0);
            const combined = new Float32Array(_accumBuffer.length + input.length);
            combined.set(_accumBuffer);
            combined.set(input, _accumBuffer.length);
            if (combined.length >= _TUNER_MIN_YIN_SAMPLES) {
                _pendingBuffer = combined.slice(combined.length - _TUNER_MIN_YIN_SAMPLES);
                _accumBuffer = combined.slice(input.length);
            } else {
                _accumBuffer = combined;
            }
        };

        _gainNode.connect(_processor);
        _processor.connect(_audioCtx.destination);

        _yinWorker = new Worker('/api/plugins/tuner/workers/yin.js');
        _yinWorker.onmessage = (e) => { _handleYinResult(e.data); _processingFrame = false; };
        _yinWorker.onerror = (e) => { console.error('Tuner: YIN worker error', e); _processingFrame = false; };

        _detectInterval = setInterval(() => {
            if (_frameBusy() || !_pendingBuffer || !_yinWorker) return;
            const buf = _pendingBuffer;
            _pendingBuffer = null;
            _processingFrame = true;
            _frameSentAt = Date.now();
            _yinWorker.postMessage({ samples: buf, sampleRate: _audioCtx.sampleRate }, [buf.buffer]);
        }, 30);
    }

    function _doStop() {
        // Invalidate any start still suspended on an await so it aborts instead
        // of installing a worker/interval after we've torn down.
        _startGen++;
        if (_bridgeInterval) { clearInterval(_bridgeInterval); _bridgeInterval = null; }
        _usingDesktopBridge = false;
        if (_detectInterval) { clearInterval(_detectInterval); _detectInterval = null; }
        if (_yinWorker) { _yinWorker.terminate(); _yinWorker = null; }
        _processingFrame = false;
        _pendingBuffer = null;
        _accumBuffer = new Float32Array(0);
        _resetLock();
        if (_processor) { _processor.disconnect(); _processor = null; }
        if (_gainNode) { _gainNode.disconnect(); _gainNode = null; }
        if (_sourceNode) { _sourceNode.disconnect(); _sourceNode = null; }
        if (_stream) { _stream.getTracks().forEach(t => t.stop()); _stream = null; }
        if (_audioCtx) { _audioCtx.close(); _audioCtx = null; }
    }

    window._tunerAudio = {
        start: async function(options, onResult) {
            _onResult = onResult;
            await _doStart(options.deviceId, options.channel, options.audioInputMode || 'auto');
        },
        stop: function() {
            _onResult = null;
            _doStop();
        },
        restart: async function(options) {
            _doStop();
            await _doStart(options.deviceId, options.channel, options.audioInputMode || 'auto');
        },
        get usingBridge() { return _usingDesktopBridge; },
        // Test hook: drive the note-lock logic with synthetic worker results.
        _feed: function(result, onResult) {
            if (onResult) _onResult = onResult;
            _handleYinResult(result);
        },
        _reset: function() { _resetLock(); },
    };
})();
