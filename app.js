"use strict";

(() => {
  const main = document.getElementById("shows");
  const footer = document.getElementById("footer");
  const tip = document.getElementById("tip");

  const rtf = new Intl.RelativeTimeFormat("en", { numeric: "auto" });
  const fullDate = new Intl.DateTimeFormat(undefined, { dateStyle: "long", timeStyle: "short" });

  function relative(iso) {
    const then = new Date(iso);
    const secs = (then.getTime() - Date.now()) / 1000;
    const steps = [
      ["year", 31536000], ["month", 2592000], ["week", 604800],
      ["day", 86400], ["hour", 3600], ["minute", 60],
    ];
    for (const [unit, size] of steps) {
      if (Math.abs(secs) >= size) return rtf.format(Math.round(secs / size), unit);
    }
    return "just now";
  }

  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  }

  function pad(n) {
    return String(n).padStart(2, "0");
  }

  function code(ep) {
    const s = `S${pad(ep.s)}`;
    return ep.e == null ? s : `${s}E${pad(ep.e)}`;
  }

  function stateText(ep) {
    if (ep.state === "watched") return "watched";
    if (ep.state === "in_progress") return `${ep.pct}%`;
    return "unwatched";
  }

  function seasonShort(season) {
    return season.number === 0 ? "Sp" : `S${season.number}`;
  }

  function seasonLong(season) {
    const generic = /^season\s+\d+$/i.test(season.name || "");
    return generic || !season.name ? seasonShort(season) : `${seasonShort(season)} · ${season.name}`;
  }

  // ------------------------------------------------------------ rendering

  function renderBar(show) {
    const bar = el("div", "bar");
    const inProgress = show.seasons.reduce(
      (n, s) => n + s.episodes.filter((e) => e.state === "in_progress").length, 0);
    let label = `Watched ${show.watched} of ${show.total} episodes`;
    if (inProgress) label += `, ${inProgress} in progress`;
    bar.setAttribute("role", "img");
    bar.setAttribute("aria-label", label);
    bar.tabIndex = 0;

    const currentIndex = show.current ? show.current.index : -1;
    const episodes = [];
    for (const season of show.seasons) {
      const lab = el("div", "season-label", seasonShort(season));
      lab.title = seasonLong(season);
      lab.setAttribute("aria-hidden", "true");
      const cells = el("div", "cells");
      cells.setAttribute("aria-hidden", "true");
      for (const ep of season.episodes) {
        const seg = el("div", `seg ${ep.state}`);
        if (ep.state === "in_progress") seg.style.setProperty("--pct", `${ep.pct}%`);
        if (episodes.length === currentIndex) seg.classList.add("current");
        seg.dataset.i = episodes.length;
        episodes.push(ep);
        cells.append(seg);
      }
      bar.append(lab, cells);
    }
    bar._episodes = episodes;
    return bar;
  }

  function position(show) {
    const cur = show.current;
    if (!cur) return show.watched === show.total ? "Finished" : "";
    return cur.state === "in_progress" ? `${code(cur)} · ${cur.pct}%` : `Next: ${code(cur)}`;
  }

  function renderCard(show) {
    const card = el("article", "card");
    const h = el("h2", null, show.name);
    if (show.year) h.append(el("span", "year", String(show.year)));
    card.append(h);

    const meta = el("p", "meta");
    const pos = el("span", "pos", position(show));
    meta.append(pos);
    if (show.current && show.current.title) meta.append(el("span", "ep-title", show.current.title));
    if (show.last_activity) {
      const t = el("time", "when", `updated ${relative(show.last_activity)}`);
      t.dateTime = show.last_activity;
      t.title = fullDate.format(new Date(show.last_activity));
      meta.append(t);
    }
    card.append(meta, renderBar(show));
    return card;
  }

  // ------------------------------------------------------------ tooltip

  let active = null; // { bar, index, pinned }

  function nearestSeg(bar, x, y) {
    let best = null;
    let bestDist = Infinity;
    for (const seg of bar.querySelectorAll(".seg")) {
      const r = seg.getBoundingClientRect();
      const dx = Math.max(0, r.left - x, x - r.right);
      const dy = Math.max(0, r.top - y, y - r.bottom);
      const d = dx + dy * 4;
      if (d < bestDist) { bestDist = d; best = seg; }
      if (d === 0) break;
    }
    return best;
  }

  function show(bar, index, pinned) {
    hide();
    const seg = bar.querySelector(`.seg[data-i="${index}"]`);
    const ep = bar._episodes[index];
    if (!seg || !ep) return;
    active = { bar, index, pinned };
    seg.classList.add("active");

    tip.replaceChildren(
      el("span", "code", code(ep)),
      document.createTextNode(` · ${ep.title || "Untitled"} · ${stateText(ep)}`),
    );
    tip.hidden = false;

    const r = seg.getBoundingClientRect();
    const t = tip.getBoundingClientRect();
    const margin = 8;
    let left = r.left + r.width / 2 - t.width / 2;
    left = Math.max(margin, Math.min(left, window.innerWidth - t.width - margin));
    let top = r.top - t.height - 8;
    if (top < margin) top = r.bottom + 8;
    tip.style.left = `${left + window.scrollX}px`;
    tip.style.top = `${top + window.scrollY}px`;
  }

  function hide() {
    if (!active) return;
    const seg = active.bar.querySelector(`.seg[data-i="${active.index}"]`);
    if (seg) seg.classList.remove("active");
    active = null;
    tip.hidden = true;
  }

  function fromEvent(bar, e, pinned) {
    const seg = nearestSeg(bar, e.clientX, e.clientY);
    if (seg) show(bar, Number(seg.dataset.i), pinned);
  }

  function wire(bar) {
    bar.addEventListener("pointermove", (e) => {
      if (e.pointerType === "mouse" && !(active && active.pinned && active.bar !== bar)) {
        fromEvent(bar, e, false);
      } else if (e.pointerType !== "mouse" && e.buttons) {
        fromEvent(bar, e, true); // touch/pen scrub
      }
    });
    bar.addEventListener("pointerdown", (e) => fromEvent(bar, e, true));
    bar.addEventListener("pointerleave", (e) => {
      if (e.pointerType === "mouse" && active && !active.pinned) hide();
    });
    bar.addEventListener("keydown", (e) => {
      const last = bar._episodes.length - 1;
      const cur = active && active.bar === bar ? active.index : -1;
      const next = {
        ArrowRight: Math.min(cur + 1, last),
        ArrowLeft: Math.max(cur - 1, 0),
        Home: 0,
        End: last,
      }[e.key];
      if (next != null) {
        e.preventDefault();
        show(bar, next, true);
      } else if (e.key === "Escape") {
        hide();
      }
    });
    bar.addEventListener("blur", hide);
  }

  document.addEventListener("pointerdown", (e) => {
    if (active && !active.bar.contains(e.target)) hide();
  });

  // ------------------------------------------------------------ boot

  function status(text) {
    main.replaceChildren(el("p", "status", text));
  }

  async function load() {
    let data;
    try {
      const res = await fetch(`data/progress.json?t=${Date.now()}`, { cache: "no-store" });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      data = await res.json();
    } catch (err) {
      status(`Couldn't load progress right now (${err.message}). Try again later.`);
      footer.textContent = "synced from Jellyfin";
      return;
    }

    const series = Array.isArray(data.series) ? data.series : [];
    if (series.length === 0) {
      status("Nothing in progress right now.");
    } else {
      main.replaceChildren(...series.map(renderCard));
      for (const bar of main.querySelectorAll(".bar")) wire(bar);
      window.addEventListener("resize", hide);
    }

    footer.replaceChildren(document.createTextNode("synced from Jellyfin"));
    if (data.generated) {
      footer.append(document.createTextNode(" · "));
      const t = el("time", null, `updated ${relative(data.generated)}`);
      t.dateTime = data.generated;
      t.title = fullDate.format(new Date(data.generated));
      footer.append(t);
    }
  }

  load();
})();
