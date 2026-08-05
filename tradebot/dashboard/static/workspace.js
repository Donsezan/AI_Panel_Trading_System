/* The workspace's client: keep the panes current, and be loud when it cannot.
 *
 * Two jobs, and nothing else. The socket carries pane *names* and never data or commands
 * (ADR 0024), so this file translates a name into an ordinary authenticated GET that the server
 * renders through the same templates a navigation would. There is no second rendering path here,
 * and nothing on this page can change state — every mutation remains a POST on another screen.
 *
 * Losing the transport must not be silent: that is the exact failure live updates exist to
 * prevent. So a dropped socket raises a visible pill and the panes fall back to slow polling
 * until it returns.
 */
(function () {
  "use strict";

  var RECONNECT_MIN_MS = 1000;
  var RECONNECT_MAX_MS = 30000;
  /* Slow on purpose: this is the degraded mode, and it must not turn a server that is briefly
     unreachable into a page hammering it. */
  var FALLBACK_POLL_MS = 30000;

  var pill = document.getElementById("live-pill");
  var backoff = RECONNECT_MIN_MS;
  var fallback = null;

  function panes(names) {
    return (names || []).map(function (name) {
      return document.querySelector('[data-pane="' + name + '"]');
    }).filter(Boolean);
  }

  function refresh(elements) {
    elements.forEach(function (element) {
      element.dispatchEvent(new CustomEvent("refresh", { bubbles: false }));
    });
  }

  function allPanes() {
    return Array.prototype.slice.call(document.querySelectorAll("[data-pane]"));
  }

  function degraded(isDegraded) {
    if (pill) pill.hidden = !isDegraded;
    if (isDegraded && fallback === null) {
      fallback = window.setInterval(function () { refresh(allPanes()); }, FALLBACK_POLL_MS);
    } else if (!isDegraded && fallback !== null) {
      window.clearInterval(fallback);
      fallback = null;
    }
  }

  function connect() {
    var scheme = window.location.protocol === "https:" ? "wss:" : "ws:";
    var socket = new WebSocket(scheme + "//" + window.location.host + "/ws/updates");

    socket.onopen = function () {
      backoff = RECONNECT_MIN_MS;
      degraded(false);
    };
    socket.onmessage = function (event) {
      var notice = JSON.parse(event.data);
      refresh(panes(notice.panes));
    };
    socket.onclose = function () {
      degraded(true);
      window.setTimeout(connect, backoff);
      backoff = Math.min(backoff * 2, RECONNECT_MAX_MS);
    };
    /* `onclose` always follows, so the retry is scheduled there and only there. */
    socket.onerror = function () { socket.close(); };
  }

  /* ------------------------------------------------------------------ the chart
   *
   * Floats live on the wire and on the canvas, and nowhere else: every number a human reads in a
   * marker label is the server's own decimal string (chart.py, PHASE_10 decision 6). Nothing here
   * reads a value back out of a chart.
   */

  function drawChart(figure) {
    var canvas = figure.querySelector(".chart-canvas");
    var error = figure.querySelector(".chart-error");
    var note = figure.querySelector(".chart-note");

    var chart = window.LightweightCharts.createChart(canvas, {
      autoSize: true,
      layout: { background: { color: "transparent" }, textColor: "#8b959f", attributionLogo: false },
      grid: { vertLines: { color: "#2a313a" }, horzLines: { color: "#2a313a" } },
      rightPriceScale: { borderColor: "#2a313a" },
      timeScale: { borderColor: "#2a313a", timeVisible: true, secondsVisible: false }
    });
    var series = chart.addSeries(window.LightweightCharts.CandlestickSeries, {
      upColor: "#3fb950", downColor: "#f85149",
      borderUpColor: "#3fb950", borderDownColor: "#f85149",
      wickUpColor: "#3fb950", wickDownColor: "#f85149"
    });
    var markers = window.LightweightCharts.createSeriesMarkers(series, []);

    function show(message) {
      /* A failed chart is information; a spinner that never resolves is not. */
      error.textContent = message;
      error.hidden = !message;
    }

    function load() {
      fetch(figure.dataset.url, { headers: { accept: "application/json" } })
        .then(function (response) { return response.json(); })
        .then(function (data) {
          if (data.error) { show(data.error); return; }
          show("");
          series.setData(data.candles);
          markers.setMarkers(data.markers);
          /* A gap is a bar the venue never published, so it is named rather than drawn over. */
          note.textContent = data.gaps.length
            ? data.gaps.length + " gap(s) in the tape"
            : "";
        })
        .catch(function (reason) { show(String(reason)); });
    }

    figure.addEventListener("refresh", load);
    load();
  }

  /* ------------------------------------------------------------------ one submit per click
   *
   * UX hygiene, not a safety control: the money path is idempotent by `client_order_id`, so a
   * double-submitted close cannot become two orders. What this prevents is the second POST landing
   * after the first has already changed the state the operator was acting on.
   *
   * Disabled after the event loop turn, never during it — disabling a button mid-dispatch drops it
   * from the submitted form data. Re-enabled on `pageshow`, so a page restored from the back/
   * forward cache does not come back with every action dead; and only the buttons this guard
   * disabled, so a close the *server* disabled — supervision stopped, nothing polling orders —
   * stays disabled.
   */
  function guardSubmits() {
    document.addEventListener("submit", function (event) {
      var button = event.target.querySelector('button[type="submit"]:not([disabled])');
      if (!button) return;
      window.setTimeout(function () {
        button.dataset.guarded = "1";
        button.disabled = true;
      }, 0);
    });
    window.addEventListener("pageshow", function () {
      Array.prototype.slice
        .call(document.querySelectorAll("button[data-guarded]"))
        .forEach(function (button) {
          button.disabled = false;
          delete button.dataset.guarded;
        });
    });
  }

  /* ------------------------------------------------------------------ the layout
   *
   * Pane sizes are the one piece of state this screen keeps client-side, and the exception to
   * "selection lives in the URL" is deliberate: a size is a per-workstation display preference
   * that changes nothing the server renders. Selection is in the URL precisely so a bookmark
   * reproduces a *view*; putting one operator's pane sizes there would make it reproduce their
   * monitor instead.
   *
   * Sizes are written as `--size-<pane>` on the container, never as an inline style on a pane:
   * htmx replaces a pane's whole section on every refresh and would swap the style away with it.
   * The value is the pane's pixel extent at the moment of the drag, used as a flex-grow ratio —
   * which is exactly the proportion the operator just drew, and keeps scaling with the window.
   *
   * Everything degrades. An unreadable or absent stored layout leaves the stylesheet's defaults
   * in place, a storage write that fails costs the persistence and not the drag, and a browser
   * that never ran this file still gets the designed layout.
   */
  var LAYOUT_KEY = "tradebot.workspace.layout";
  /* No pane may be dragged below its own title bar — a pane that cannot be seen cannot be dragged
     back, and nothing else on this screen restores one. */
  var MIN_PANE_PX = 44;
  var KEY_STEP_PX = 24;
  /* Which key moves which way, per axis. A table rather than a branch, and it also means an arrow
     that makes no sense for this splitter is a lookup miss rather than a surprising resize. */
  var KEY_STEPS = {
    h: { ArrowUp: -KEY_STEP_PX, ArrowDown: KEY_STEP_PX },
    v: { ArrowLeft: -KEY_STEP_PX, ArrowRight: KEY_STEP_PX }
  };

  function storedSizes() {
    /* Defensive on read: this value is editable by anyone with the console open, and a junk size
       must cost the stored layout rather than the screen. */
    var sizes = {};
    try {
      var stored = JSON.parse(window.localStorage.getItem(LAYOUT_KEY)) || {};
      Object.keys(stored).forEach(function (name) {
        if (typeof stored[name] === "number" && isFinite(stored[name]) && stored[name] > 0) {
          sizes[name] = stored[name];
        }
      });
    } catch (reason) {
      return {};
    }
    return sizes;
  }

  function layout(root) {
    var sizes = storedSizes();

    function write(name, value) {
      sizes[name] = value;
      root.style.setProperty("--size-" + name, value);
    }

    function reset(name) {
      delete sizes[name];
      root.style.removeProperty("--size-" + name);
    }

    function save() {
      try {
        window.localStorage.setItem(LAYOUT_KEY, JSON.stringify(sizes));
      } catch (reason) {
        /* Private browsing, a full quota: the layout still works, it just stops being remembered. */
      }
    }

    Object.keys(sizes).forEach(function (name) { write(name, sizes[name]); });

    function arm(splitter) {
      var axis = splitter.dataset.axis;
      var names = splitter.dataset.resize.split(":");
      var vertical = axis === "v";
      var before = splitter.previousElementSibling;
      var after = splitter.nextElementSibling;

      function extent(element) {
        var box = element.getBoundingClientRect();
        return vertical ? box.width : box.height;
      }

      function along(event) {
        return vertical ? event.clientX : event.clientY;
      }

      /* Captures both sizes once, at the start: the pair shares a fixed total, so the drag only
         decides where the boundary between them sits. */
      function dragFrom(origin) {
        var first = extent(before);
        var total = first + extent(after);
        return function (to) {
          var size = Math.min(Math.max(first + (to - origin), MIN_PANE_PX), total - MIN_PANE_PX);
          write(names[0], size);
          write(names[1], total - size);
        };
      }

      splitter.addEventListener("pointerdown", function (event) {
        var move = dragFrom(along(event));
        var onMove = function (moved) { move(along(moved)); };
        var end = function () {
          splitter.removeEventListener("pointermove", onMove);
          splitter.classList.remove("dragging");
          save();
        };
        splitter.setPointerCapture(event.pointerId);
        splitter.classList.add("dragging");
        splitter.addEventListener("pointermove", onMove);
        splitter.addEventListener("pointerup", end, { once: true });
        splitter.addEventListener("pointercancel", end, { once: true });
        /* Otherwise the browser starts a text selection across the panes being resized. */
        event.preventDefault();
      });

      splitter.addEventListener("keydown", function (event) {
        var step = KEY_STEPS[axis][event.key];
        if (step === undefined) return;
        dragFrom(0)(step);
        save();
        event.preventDefault();
      });

      /* One gesture back to the stylesheet's default, for a layout dragged somewhere unhelpful. */
      splitter.addEventListener("dblclick", function () {
        names.forEach(reset);
        save();
      });
    }

    Array.prototype.slice.call(root.querySelectorAll("[data-resize]")).forEach(arm);
  }

  function start() {
    var pane = document.querySelector('[data-pane="chart"]');
    var figures = Array.prototype.slice.call(document.querySelectorAll("[data-chart]"));
    figures.forEach(drawChart);
    /* The pane is nudged by name; each figure reloads its own window. */
    if (pane) {
      pane.addEventListener("refresh", function () { refresh(figures); });
    }
    var root = document.querySelector("[data-layout]");
    if (root) layout(root);
    guardSubmits();
    connect();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();
