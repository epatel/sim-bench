// Run controls: play / pause, step, reset, speed (auto or manual), and the run status.
import { fmt } from "./canvas.js";

const $ = (id) => document.getElementById(id);

// ctx: { state, editor, run, results, renderMetrics, showBanner, setStatus }
export function wireRunControls({ state, editor, run, results, renderMetrics, showBanner, setStatus }) {
  function ensureLoaded() {
    if (!state.runLoaded || state.runStale) {
      editor.clearLive();
      run.load(state.model, Number($("seed").value) || 0);
      run.speed(state.speed);
      state.runLoaded = true;
      state.runStale = false;
      setStatus("");
    }
  }

  $("btn-play").onclick = () => {
    if (state.playing) return run.pause();
    if (state.finished) state.runStale = true; // play again from the start
    ensureLoaded();
    run.play();
  };
  $("btn-step").onclick = () => {
    ensureLoaded();
    run.step();
  };
  $("btn-reset").onclick = () => {
    state.runStale = true;
    state.summary = null;
    ensureLoaded();
    renderMetrics();
  };
  function setSpeed(value, auto) {
    state.speed = value;
    state.speedAuto = auto;
    $("speed").value = String(Math.log10(value));
    $("speed-label").textContent = `${fmt(value)}×${auto ? " auto" : ""}`;
    run.speed(value);
  }

  $("speed").oninput = () => setSpeed(10 ** Number($("speed").value), false);
  $("speed-label").onclick = () => {
    if (state.suggestedSpeed) setSpeed(state.suggestedSpeed, true);
  };

  run.addEventListener("frame", (ev) => {
    const f = ev.detail;
    state.playing = f.playing;
    editor.setClock(f.now, f.playing, state.speed);
    editor.addRecords(f.records, { stepping: f.step });
    $("sim-time").textContent = fmt(f.now);
    $("sim-events").textContent = fmt(f.events);
    $("btn-play").innerHTML = `<i class="fa-solid ${f.playing ? "fa-pause" : "fa-play"}"></i>`;
    state.finished = !f.playing && f.pending === 0 && f.events > 0;
    if (!state.runStale) runStatus(f);
    if (f.summary) {
      state.summary = f.summary;
      renderMetrics();
    }
  });
  run.addEventListener("whatif", (ev) => results.show(ev.detail.batch));
  run.addEventListener("loaded", (ev) => {
    state.ports = ev.detail.ports;
    state.suggestedSpeed = ev.detail.suggested_speed;
    if (state.speedAuto && state.suggestedSpeed) setSpeed(state.suggestedSpeed, true);
  });

  function runStatus(f) {
    const delivered = f.records.filter((r) => r.type === "deliver");
    if (f.lagging) setStatus("lagging · lower speed", "warn");
    else if (state.finished) setStatus("finished · play restarts");
    else if (f.playing) setStatus("running");
    else if (f.step && delivered.length) setStatus(`delivered to ${delivered.map((r) => `${r.entity}.${r.port}`).join(", ")}`);
    else setStatus("");
  }
  run.addEventListener("error", (ev) => {
    state.runLoaded = false;
    setStatus("run error", "bad");
    showBanner(ev.detail.message);
  });
  run.addEventListener("closed", () => {
    state.runLoaded = false;
    state.playing = false;
    setStatus("disconnected", "bad");
  });
}
