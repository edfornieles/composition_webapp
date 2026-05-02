/* Shared renderer used by live page + editor preview. */
(function (global) {
  "use strict";

  function createRunner(config) {
    const mountAsset = typeof config.mountAsset === "function" ? config.mountAsset : function () {};
    const sourceMode = (config.sourcePlaybackMode || "chronological").toLowerCase();
    const speed = Math.max(0.25, Number(config.speed) || 1);
    function normalizeTransitionMode(value) {
      const raw = String(value || "none").toLowerCase().trim();
      const compact = raw.replace(/[\s_-]+/g, "");
      if (compact === "crossfade") return "crossfade";
      if (compact === "fade") return "fade";
      return "none";
    }
    const transition = normalizeTransitionMode(config.transition);
    const transitionByLayerRaw = config.transitionByLayer || {};
    const transitionByLayer = {
      bg: normalizeTransitionMode(transitionByLayerRaw.bg || transition),
      fg: normalizeTransitionMode(transitionByLayerRaw.fg || transition),
      any: normalizeTransitionMode(transitionByLayerRaw.any || transition),
    };
    const pools = config.pools || {};
    const els = config.elements || {};
    const root = config.root || null;
    const transitionMount = Boolean(config.transitionMount);
    const timers = new Set();
    let bgCursor = 0;
    let fgCursor = 0;
    let anyCursor = 0;
    let batBackdropStage = null;

    function clearAllTimers() {
      timers.forEach((id) => clearTimeout(id));
      timers.clear();
    }

    function later(fn, ms) {
      const id = setTimeout(function () {
        timers.delete(id);
        fn();
      }, Math.max(0, ms | 0));
      timers.add(id);
      return id;
    }

    function cycleDelay(base, jitter) {
      const raw = base + Math.floor(Math.random() * jitter);
      return Math.max(320, Math.floor(raw / speed));
    }

    function centerMaskDelay(shape) {
      // Cross modes benefit from steadier rhythm; wide jitter makes them
      // feel randomly fast/slow from one cycle to the next.
      if (shape === "cross") {
        return cycleDelay(1850, 320);
      }
      return cycleDelay(2100, 1600);
    }

    function pickFromPool(pool, key, lastIndex) {
      if (!Array.isArray(pool) || !pool.length) return { asset: null, idx: -1 };
      if (pool.length === 1) return { asset: pool[0], idx: 0 };
      let idx = 0;
      if (sourceMode === "random") {
        idx = Math.floor(Math.random() * pool.length);
        if (idx === lastIndex) idx = (idx + 1) % pool.length;
      } else if (key === "bg") {
        idx = bgCursor % pool.length;
        bgCursor += 1;
      } else if (key === "fg") {
        idx = fgCursor % pool.length;
        fgCursor += 1;
      } else {
        idx = anyCursor % pool.length;
        anyCursor += 1;
      }
      return { asset: pool[idx], idx: idx };
    }

    function mountPaneAsset(el, asset, afterMount, done, modeOverride) {
      if (!el || !asset) {
        if (typeof done === "function") done();
        return;
      }
      const mode = transitionMount ? normalizeTransitionMode(modeOverride || transition) : "none";
      const shouldTransition = mode === "fade" || mode === "crossfade";
      const hasCurrent = el.children && el.children.length > 0;
      const finish = function (target) {
        if (typeof afterMount === "function") afterMount(target || el);
        if (typeof done === "function") done();
      };
      if (!shouldTransition || !hasCurrent) {
        if (!hasCurrent) {
          mountAsset(el, asset, function () { finish(el); });
          return;
        }
        const incoming = document.createElement("div");
        incoming.className = "shared-transition-layer";
        incoming.style.position = "absolute";
        incoming.style.inset = "0";
        incoming.style.opacity = "1";
        const computedPosition = global.getComputedStyle ? global.getComputedStyle(el).position : "";
        if (!computedPosition || computedPosition === "static") el.style.position = "relative";
        el.style.overflow = "hidden";
        el.appendChild(incoming);
        mountAsset(incoming, asset, function () {
          if (typeof afterMount === "function") afterMount(incoming);
          const stale = Array.from(el.children).filter(function (node) { return node !== incoming; });
          stale.forEach(function (node) {
            if (node && node.parentElement === el) node.remove();
          });
          if (typeof done === "function") done();
        });
        return;
      }
      const incoming = document.createElement("div");
      incoming.className = "shared-transition-layer";
      incoming.style.position = "absolute";
      incoming.style.inset = "0";
      incoming.style.opacity = "0";
      incoming.style.transition = "opacity 520ms ease-in-out";
      incoming.style.willChange = "opacity";
      const computedPosition = global.getComputedStyle ? global.getComputedStyle(el).position : "";
      if (!computedPosition || computedPosition === "static") el.style.position = "relative";
      el.style.overflow = "hidden";
      el.appendChild(incoming);
      mountAsset(incoming, asset, function () {
        if (typeof afterMount === "function") afterMount(incoming);
        const stale = Array.from(el.children).filter(function (node) { return node !== incoming; });
        requestAnimationFrame(function () {
          incoming.style.opacity = "1";
          stale.forEach(function (node) {
            node.style.transition = "opacity 520ms ease-in-out";
            node.style.opacity = mode === "fade" ? "0" : "0";
          });
          later(function () {
            stale.forEach(function (node) {
              if (node && node.parentElement === el) node.remove();
            });
            if (typeof done === "function") done();
          }, 560);
        });
      });
    }

    function bindPaneFlow(el, pool, key, seedMs) {
      if (!el || !Array.isArray(pool) || !pool.length) {
        if (el) el.innerHTML = "";
        return;
      }
      let lastIdx = -1;
      const tick = function () {
        const picked = pickFromPool(pool, key, lastIdx);
        if (!picked.asset) return;
        lastIdx = picked.idx;
        const layerTransition = key === "fg" ? transitionByLayer.fg : (key === "bg" ? transitionByLayer.bg : transitionByLayer.any);
        mountPaneAsset(el, picked.asset, null, function () {
          later(tick, cycleDelay(1700, 1500));
        }, layerTransition);
      };
      later(tick, seedMs || 0);
    }

    function setCenterMaskMode(shape, rotate, mirror) {
      if (!root || !root.classList) return;
      root.classList.remove(
        "center-mask-mode",
        "center-mask-circle-mode",
        "center-mask-cross-mode",
        "center-mask-bat-mode",
        "center-mask-rotate-mode",
        "center-mask-mirror-mode",
        "center-mask-layered-mode",
        "center-mask-tunnel-mode"
      );
      if (!shape) return;
      root.classList.add("center-mask-mode");
      if (shape === "circle") root.classList.add("center-mask-circle-mode");
      if (shape === "cross") root.classList.add("center-mask-cross-mode");
      if (shape === "bat") root.classList.add("center-mask-bat-mode");
      if (rotate) root.classList.add("center-mask-rotate-mode");
      if (mirror) root.classList.add("center-mask-mirror-mode");
    }

    function clearBatBackdropStage() {
      if (batBackdropStage && batBackdropStage.parentNode) {
        batBackdropStage.parentNode.removeChild(batBackdropStage);
      }
      batBackdropStage = null;
    }

    function ensureBatBackdropStage() {
      if (!root) return null;
      if (!batBackdropStage || !batBackdropStage.parentNode) {
        batBackdropStage = document.createElement("div");
        batBackdropStage.className = "bat-backdrop-stage";
        root.appendChild(batBackdropStage);
      }
      return batBackdropStage;
    }

    function assetSourceKey(asset) {
      const raw = String((asset && asset.url) || "");
      const m = raw.match(/^\/source-media\/([^/]+)\//);
      return m ? decodeURIComponent(m[1]) : "";
    }

    function assetKey(asset) {
      return String((asset && asset.url) || (asset && asset.name) || "");
    }

    function poolForSameSource(pool, seedAsset) {
      if (!Array.isArray(pool) || !pool.length || !seedAsset) return [];
      const seedSource = assetSourceKey(seedAsset);
      if (!seedSource) return [];
      return pool.filter(function (a) { return assetSourceKey(a) === seedSource; });
    }

    function mountBatBackdropLayers(pool, avoidAssetKey) {
      if (!Array.isArray(pool) || !pool.length) {
        clearBatBackdropStage();
        return;
      }
      const stage = ensureBatBackdropStage();
      if (!stage) return;
      stage.innerHTML = "";
      const usedKeys = new Set();
      const avoidKey = String(avoidAssetKey || "");
      if (avoidKey) usedKeys.add(avoidKey);
      const targetCount = Math.min(3, Math.max(2, pool.length > 2 ? 3 : 2));
      // Keep concentric steps tight so each larger bat reads as an even halo
      // behind the center bat (instead of only wing fragments).
      const sizes = ["84vmin", "96vmin", "108vmin"];
      const opacities = [0.5, 0.4, 0.3];
      const maskUrls = [
        "url('/source-media/svg_shapes/bat_2.svg')",
        "url('/source-media/svg_shapes/bat_3.svg')",
        "url('/source-media/svg_shapes/bat_3.svg')",
      ];
      const mediaBrightness = [1.06, 0.98, 0.9];
      const baseAlpha = [0.36, 0.28, 0.22];
      const dirs = ["bat-backdrop-media-ccw", "bat-backdrop-media-cw", "bat-backdrop-media-ccw"];
      for (let i = 0; i < targetCount; i += 1) {
        let picked = pickFromPool(pool, "fg", -1);
        if (!picked.asset) continue;
        let guard = 0;
        while (usedKeys.has(assetKey(picked.asset)) && guard < pool.length + 4) {
          picked = pickFromPool(pool, "fg", picked.idx);
          guard += 1;
        }
        const key = assetKey(picked.asset);
        if (!picked.asset || usedKeys.has(key)) continue;
        usedKeys.add(key);
        const layer = document.createElement("div");
        layer.className = "bat-backdrop-layer";
        layer.style.setProperty("--bat-backdrop-size", sizes[i] || "102vmin");
        layer.style.setProperty("--bat-mask-url", maskUrls[i] || maskUrls[0]);
        layer.style.setProperty("--bat-base-alpha", String(baseAlpha[i] || 0.12));
        layer.style.opacity = String(opacities[i] || 0.24);
        const mediaHost = document.createElement("div");
        mediaHost.className = "bat-backdrop-media " + (dirs[i] || "bat-backdrop-media-cw");
        mediaHost.style.filter = "brightness(" + String(mediaBrightness[i] || 0.7) + ")";
        layer.appendChild(mediaHost);
        stage.appendChild(layer);
        mountAsset(mediaHost, picked.asset, function () {
          mediaHost.querySelectorAll("video").forEach(function (video) {
            video.muted = true;
            video.autoplay = true;
            video.loop = true;
            video.playsInline = true;
            try { video.play().catch(function () {}); } catch (_) {}
          });
        });
      }
    }

    function runCenterMaskTunnel(shape) {
      showCoreLayers();
      setCenterMaskMode(shape, false, false);
      if (root && root.classList) root.classList.add("center-mask-tunnel-mode");
      if (config.setVibrateMode) config.setVibrateMode(false);
      // Prevent carry-over from previous modes (e.g. opacity/filter/transform)
      // which can leave the background effectively invisible.
      resetLayerStyle(els.background);
      resetLayerStyle(els.foreground);
      if (els.foreground) {
        // Keep cross-radiate unobstructed: no static foreground pane above it.
        els.foreground.classList.add("hidden");
        els.foreground.innerHTML = "";
      }
      bindPaneFlow(els.background, pools.bg, "bg", 120);
      const tunnelPool = (Array.isArray(pools.fg) && pools.fg.length) ? pools.fg : pools.bg;
      if (!els.tunnelStage || !Array.isArray(tunnelPool) || !tunnelPool.length) return;
      els.tunnelStage.classList.remove("hidden");
      els.tunnelStage.innerHTML = "";
      let layerDepth = 1;
      let lastIdx = -1;
      const isNoTransition = transition === "none";
      const durationSec = Math.max(1.1, 3.6 / speed);
      const spawnMs = isNoTransition
        ? Math.max(100, Math.floor(280 / speed))
        : Math.max(95, Math.floor(240 / speed));
      const spawnLayer = function () {
        const picked = pickFromPool(tunnelPool, "fg", lastIdx);
        if (!picked.asset) return;
        lastIdx = picked.idx;
        const layer = document.createElement("div");
        layer.className = "tunnel-layer";
        layer.style.animationName = isNoTransition ? "crossTunnelExpandNoFade" : "crossTunnelExpand";
        layer.style.animationDuration = durationSec.toFixed(3) + "s";
        layer.style.animationIterationCount = "1";
        layer.style.animationTimingFunction = "linear";
        layer.style.animationFillMode = "forwards";
        layer.style.zIndex = String(layerDepth++);
        layer.style.willChange = "transform, opacity";
        els.tunnelStage.appendChild(layer);
        mountAsset(layer, picked.asset);
        layer.addEventListener("animationend", function () {
          layer.remove();
        }, { once: true });
      };
      spawnLayer();
      const id = setInterval(spawnLayer, spawnMs);
      timers.add(id);
    }

    function mountLayeredCenterMask(host, pool, layerCount) {
      if (!host || !Array.isArray(pool) || !pool.length) return;
      const maxLayers = Math.min(4, Math.max(2, layerCount | 0));
      const blendModes = ["normal", "screen", "lighten", "color-dodge"];
      const opacities = [0.86, 0.54, 0.36, 0.24];
      host.innerHTML = "";
      const pickedIndexes = new Set();
      for (let i = 0; i < maxLayers; i += 1) {
        let picked = pickFromPool(pool, "fg", -1);
        if (picked.asset && pool.length > 1) {
          let guard = 0;
          while (pickedIndexes.has(picked.idx) && guard < pool.length + 2) {
            picked = pickFromPool(pool, "fg", picked.idx);
            guard += 1;
          }
        }
        if (!picked.asset) continue;
        pickedIndexes.add(picked.idx);
        const layer = document.createElement("div");
        layer.className = "center-mask-layer";
        layer.style.position = "absolute";
        layer.style.inset = "0";
        layer.style.opacity = String(opacities[i] || 0.22);
        layer.style.mixBlendMode = blendModes[i] || "screen";
        layer.style.pointerEvents = "none";
        layer.style.willChange = "transform, opacity";
        host.appendChild(layer);
        mountAsset(layer, picked.asset, function () {
          layer.querySelectorAll("video").forEach(function (video) {
            video.muted = true;
            video.autoplay = true;
            video.loop = true;
            video.playsInline = true;
            try { video.play().catch(function () {}); } catch (_) {}
          });
        });
      }
    }

    function showCoreLayers() {
      clearBatBackdropStage();
      setCenterMaskMode("", false, false);
      if (els.background) els.background.classList.remove("hidden");
      if (els.foreground) els.foreground.classList.remove("hidden");
      if (els.split) els.split.classList.add("hidden");
      if (els.splitHorizontal) els.splitHorizontal.classList.add("hidden");
      if (els.tunnelStage) els.tunnelStage.classList.add("hidden");
      if (els.scrollStage) els.scrollStage.classList.add("hidden");
      if (els.strobeStage) els.strobeStage.classList.add("hidden");
      if (typeof config.hideSections === "function") config.hideSections("core");
    }

    function hideCoreLayers() {
      clearBatBackdropStage();
      setCenterMaskMode("", false, false);
      if (els.background) els.background.classList.add("hidden");
      if (els.foreground) els.foreground.classList.add("hidden");
      if (els.split) els.split.classList.add("hidden");
      if (els.splitHorizontal) els.splitHorizontal.classList.add("hidden");
      if (els.tunnelStage) els.tunnelStage.classList.add("hidden");
      if (els.scrollStage) els.scrollStage.classList.add("hidden");
      if (els.strobeStage) els.strobeStage.classList.add("hidden");
      if (typeof config.hideSections === "function") config.hideSections("none");
    }

    function resetLayerStyle(el) {
      if (!el) return;
      el.style.opacity = "1";
      el.style.transform = "none";
      el.style.filter = "none";
      el.style.transition = "";
      el.style.willChange = "";
    }

    function splitCircleMirrorForeground(target) {
      const host = target || els.foreground;
      if (!host) return;
      const source = host.querySelector("img, video");
      if (!source) return;
      const split = document.createElement("div");
      split.className = "circle-mirror-split";
      const left = document.createElement("div");
      const right = document.createElement("div");
      left.className = "circle-mirror-half circle-mirror-left";
      right.className = "circle-mirror-half circle-mirror-right";
      const leftClone = source.cloneNode(true);
      const rightClone = source.cloneNode(true);
      [leftClone, rightClone].forEach(function (clone) {
        clone.removeAttribute("id");
        clone.muted = true;
        clone.autoplay = true;
        clone.loop = true;
        clone.playsInline = true;
        clone.style.position = "absolute";
        clone.style.top = "0";
        clone.style.width = "200%";
        clone.style.height = "100%";
        clone.style.objectFit = "cover";
        clone.style.display = "block";
      });
      leftClone.style.left = "0";
      rightClone.style.left = "-100%";
      left.appendChild(leftClone);
      right.appendChild(rightClone);
      split.appendChild(left);
      split.appendChild(right);
      host.innerHTML = "";
      host.appendChild(split);
      [leftClone, rightClone].forEach(function (clone) {
        if (clone.tagName === "VIDEO") {
          try { clone.play().catch(function () {}); } catch (_) {}
        }
      });
    }

    function restartCircleVideos(target) {
      const host = target || els.foreground;
      if (!host) return;
      host.querySelectorAll("video").forEach(function (video) {
        try { video.play().catch(function () {}); } catch (_) {}
      });
    }

    function afterCircleMount(kind, target) {
      if (kind === "mirror") {
        splitCircleMirrorForeground(target);
      } else {
        restartCircleVideos(target);
      }
    }

    function runClassic(vibrate) {
      showCoreLayers();
      if (els.splitLeft) els.splitLeft.innerHTML = "";
      if (els.splitRight) els.splitRight.innerHTML = "";
      if (els.splitTop) els.splitTop.innerHTML = "";
      if (els.splitBottom) els.splitBottom.innerHTML = "";
      resetLayerStyle(els.background);
      resetLayerStyle(els.foreground);
      if (config.setVibrateMode) config.setVibrateMode(Boolean(vibrate));
      bindPaneFlow(els.background, pools.bg, "bg", 120);
      bindPaneFlow(els.foreground, pools.fg, "fg", 420);
    }

    function runCenterMask(shape, rotate, mirror) {
      showCoreLayers();
      setCenterMaskMode(shape, rotate, mirror);
      if (els.splitLeft) els.splitLeft.innerHTML = "";
      if (els.splitRight) els.splitRight.innerHTML = "";
      if (els.splitTop) els.splitTop.innerHTML = "";
      if (els.splitBottom) els.splitBottom.innerHTML = "";
      resetLayerStyle(els.background);
      resetLayerStyle(els.foreground);
      if (els.foreground) els.foreground.style.transform = "";
      if (config.setVibrateMode) config.setVibrateMode(false);
      const fgPool = Array.isArray(pools.fg) && pools.fg.length ? pools.fg : pools.bg;
      bindPaneFlow(els.background, pools.bg, "bg", 120);
      if (!els.foreground || !Array.isArray(fgPool) || !fgPool.length) return;
      const layeredBatMode = shape === "bat" && rotate && !mirror;
      if (layeredBatMode && root && root.classList) root.classList.add("center-mask-layered-mode");
      let lastIdx = -1;
      const tick = function () {
        if (layeredBatMode) {
          const pickedMain = pickFromPool(fgPool, "fg", lastIdx);
          if (!pickedMain.asset) return;
          lastIdx = pickedMain.idx;
          const mainKey = assetKey(pickedMain.asset);
          const sameSource = poolForSameSource(fgPool, pickedMain.asset);
          let backdropPool = sameSource.filter(function (a) { return assetKey(a) !== mainKey; });
          if (!backdropPool.length) backdropPool = sameSource.length ? sameSource : fgPool;
          mountPaneAsset(els.foreground, pickedMain.asset, function () {
            restartCircleVideos(els.foreground);
          }, function () {
            later(tick, centerMaskDelay(shape));
          }, transitionByLayer.fg);
          mountBatBackdropLayers(backdropPool, mainKey);
          return;
        }
        clearBatBackdropStage();
        const picked = pickFromPool(fgPool, "fg", lastIdx);
        if (!picked.asset) return;
        lastIdx = picked.idx;
        mountPaneAsset(els.foreground, picked.asset, function (target) {
          if (mirror && shape === "circle") afterCircleMount("mirror", target);
          else restartCircleVideos(target);
        }, function () {
          later(tick, centerMaskDelay(shape));
        }, transitionByLayer.fg);
      };
      later(tick, 420);
    }

    function runSingle() {
      hideCoreLayers();
      if (els.background) {
        els.background.classList.remove("hidden");
      }
      if (els.foreground) {
        els.foreground.classList.add("hidden");
        els.foreground.innerHTML = "";
      }
      if (config.setVibrateMode) config.setVibrateMode(false);
      bindPaneFlow(els.background, pools.bg, "bg", 120);
    }

    function runSplit(horizontal) {
      hideCoreLayers();
      if (config.setVibrateMode) config.setVibrateMode(false);
      const splitRoot = horizontal ? els.splitHorizontal : els.split;
      if (splitRoot) splitRoot.classList.remove("hidden");
      const a = horizontal ? els.splitTop : els.splitLeft;
      const b = horizontal ? els.splitBottom : els.splitRight;
      bindPaneFlow(a, pools.bg, "bg", 120);
      bindPaneFlow(b, pools.fg, "fg", 420);
    }

    function runTunnel() {
      hideCoreLayers();
      if (config.setVibrateMode) config.setVibrateMode(false);
      if (!els.tunnelStage || !Array.isArray(pools.bg) || !pools.bg.length) return;
      els.tunnelStage.classList.remove("hidden");
      els.tunnelStage.innerHTML = "";
      let layerDepth = 1;
      let lastIdx = -1;
      const isNoTransition = transition === "none";
      const durationSec = Math.max(0.9, 2.4 / speed);
      const spawnMs = isNoTransition
        ? Math.max(120, Math.floor(durationSec * 1000 * 0.42))
        : Math.max(120, Math.floor(340 / speed));
      const spawnLayer = function (warmOffsetMs) {
        const picked = pickFromPool(pools.bg, "bg", lastIdx);
        if (!picked.asset) return;
        lastIdx = picked.idx;
        const layer = document.createElement("div");
        layer.className = "tunnel-layer";
        // Keep rings opaque for the entire animation; tail fade can reveal black stage.
        layer.style.animationName = "tunnelExpandNoFade";
        layer.style.animationDuration = durationSec.toFixed(3) + "s";
        layer.style.animationIterationCount = "1";
        layer.style.animationTimingFunction = "linear";
        layer.style.animationFillMode = "forwards";
        if (Number(warmOffsetMs) > 0) {
          layer.style.animationDelay = "-" + Math.floor(warmOffsetMs) + "ms";
        }
        layer.style.zIndex = String(layerDepth++);
        layer.style.willChange = "transform, opacity";
        els.tunnelStage.appendChild(layer);
        mountAsset(layer, picked.asset);
        layer.addEventListener("animationend", function () {
          layer.remove();
        }, { once: true });
      };
      const warmLeadMs = 3000;
      const warmLayers = Math.min(12, Math.max(1, Math.ceil(warmLeadMs / Math.max(1, spawnMs))));
      for (let i = warmLayers; i >= 1; i -= 1) {
        const offsetMs = Math.min(i * spawnMs, Math.floor(durationSec * 1000 * 0.94));
        spawnLayer(offsetMs);
      }
      spawnLayer(0);
      const id = setInterval(spawnLayer, spawnMs);
      timers.add(id);
    }

    function runScrollMode(kind) {
      hideCoreLayers();
      if (config.setVibrateMode) config.setVibrateMode(false);
      if (!els.scrollStage || !Array.isArray(pools.bg) || !pools.bg.length) return;
      els.scrollStage.classList.remove("hidden");
      els.scrollStage.innerHTML = "";
      let lastIdx = -1;
      const social = kind === "social-scroll";
      const durationSec = social
        ? Math.max(0.65, 4.8 / Math.pow(speed, 1.85))
        : Math.max(0.58, 3.15 / Math.pow(speed, 1.28));
      const spawnMs = social
        ? Math.max(70, Math.floor(durationSec * 1000 * 0.14))
        : Math.max(62, Math.floor(durationSec * 1000 * 0.12));
      const spawnCard = function () {
        const picked = pickFromPool(pools.bg, "bg", lastIdx);
        if (!picked.asset) return;
        lastIdx = picked.idx;
        const card = document.createElement("div");
        card.className = "scroll-card";
        card.style.zIndex = String(Date.now());
        if (social) {
          const driftX = (-5 + Math.random() * 10).toFixed(2);
          const driftTilt = (-2.4 + Math.random() * 4.8).toFixed(2);
          const driftScale = (0.985 + Math.random() * 0.05).toFixed(3);
          card.style.transform = "translateX(" + driftX + "%) rotate(" + driftTilt + "deg) scale(" + driftScale + ")";
          card.style.opacity = (0.88 + Math.random() * 0.12).toFixed(3);
          const blendModes = ["normal", "screen", "lighten", "color-dodge"];
          card.style.mixBlendMode = blendModes[Math.floor(Math.random() * blendModes.length)];
        } else {
          // Scrollhole: intentionally unstable "signal corruption" movement.
          const driftX = (-12 + Math.random() * 24).toFixed(2);
          const driftTilt = (-6.5 + Math.random() * 13).toFixed(2);
          const driftScale = (0.95 + Math.random() * 0.12).toFixed(3);
          const hue = (-18 + Math.random() * 36).toFixed(1);
          const contrast = (1.12 + Math.random() * 0.34).toFixed(3);
          const sat = (1.05 + Math.random() * 0.85).toFixed(3);
          card.style.transform = "translateX(" + driftX + "%) rotate(" + driftTilt + "deg) scale(" + driftScale + ")";
          card.style.opacity = (0.72 + Math.random() * 0.28).toFixed(3);
          card.style.filter = "contrast(" + contrast + ") saturate(" + sat + ") hue-rotate(" + hue + "deg)";
          const blendModes = ["screen", "lighten", "difference", "color-dodge", "hard-light"];
          card.style.mixBlendMode = blendModes[Math.floor(Math.random() * blendModes.length)];
          if (Math.random() < 0.55) {
            // Vertical band clipping simulates scanline-like signal breaks.
            const top = Math.floor(Math.random() * 34);
            const bottom = Math.floor(Math.random() * 26);
            card.style.clipPath = "inset(" + top + "% 0 " + bottom + "% 0)";
          }
        }
        card.style.animationDuration = durationSec.toFixed(2) + "s";
        els.scrollStage.appendChild(card);
        mountAsset(card, picked.asset);
        requestAnimationFrame(function () {
          card.style.animationPlayState = "running";
        });
        if (!social) {
          const swapCount = Math.max(2, Math.min(6, Math.round(durationSec * 1.9)));
          const swapTimers = [];
          for (let i = 1; i <= swapCount; i++) {
            const jitterAt = Math.floor((durationSec * 1000) * (i / (swapCount + 1)));
            const timerId = setTimeout(function () {
              if (!card.isConnected) return;
              const jit = pickFromPool(pools.bg, "bg", lastIdx);
              if (jit.asset) {
                lastIdx = jit.idx;
                mountAsset(card, jit.asset);
              }
              card.style.opacity = (0.68 + Math.random() * 0.3).toFixed(3);
            }, jitterAt);
            swapTimers.push(timerId);
          }
          card.addEventListener("animationend", function () {
            swapTimers.forEach(function (t) { clearTimeout(t); });
          }, { once: true });
        }
        card.addEventListener("animationend", function () {
          card.remove();
        }, { once: true });
      };
      spawnCard();
      spawnCard();
      spawnCard();
      if (social) spawnCard();
      else {
        spawnCard();
        spawnCard();
      }
      const id = setInterval(spawnCard, spawnMs);
      timers.add(id);
    }

    function runStrobe() {
      hideCoreLayers();
      if (config.setVibrateMode) config.setVibrateMode(false);
      const bgPool = Array.isArray(pools.bg) ? pools.bg : [];
      const fgPool = Array.isArray(pools.fg) ? pools.fg : [];
      const strobePool = bgPool.concat(fgPool).filter(Boolean);
      if (!els.strobeStage || !strobePool.length) return;
      els.strobeStage.classList.remove("hidden");
      els.strobeStage.innerHTML = "";
      let lastIdx = -1;
      let rrSourceCursor = 0;
      const lastIdxBySource = Object.create(null);
      const groupsBySource = Object.create(null);
      strobePool.forEach(function (asset) {
        const key = assetSourceKey(asset) || ("__asset__:" + assetKey(asset));
        if (!groupsBySource[key]) groupsBySource[key] = [];
        groupsBySource[key].push(asset);
      });
      const sourceKeys = Object.keys(groupsBySource).filter(function (k) {
        return Array.isArray(groupsBySource[k]) && groupsBySource[k].length;
      });
      const strobeMs = Math.max(24, Math.floor(42 / speed));
      const tick = function () {
        let picked;
        if (sourceMode === "random" && sourceKeys.length > 1) {
          const key = sourceKeys[rrSourceCursor % sourceKeys.length];
          rrSourceCursor += 1;
          const groupPool = groupsBySource[key] || [];
          const groupLast = Number.isInteger(lastIdxBySource[key]) ? lastIdxBySource[key] : -1;
          picked = pickFromPool(groupPool, "any", groupLast);
          if (picked && picked.asset) lastIdxBySource[key] = picked.idx;
        } else {
          picked = pickFromPool(strobePool, "any", lastIdx);
        }
        if (!picked.asset) return;
        lastIdx = picked.idx;
        mountAsset(els.strobeStage, picked.asset);
      };
      tick();
      const id = setInterval(tick, strobeMs);
      timers.add(id);
    }

    function start(mode) {
      clearAllTimers();
      const m = String(mode || "classic").toLowerCase();
      if (m === "single") {
        runSingle();
        return;
      }
      if (m === "split-lr") {
        runSplit(false);
        return;
      }
      if (m === "split-tb") {
        runSplit(true);
        return;
      }
      if (m === "vibrate") {
        runClassic(true);
        return;
      }
      if (m === "circle-foreground") {
        runCenterMask("circle", false, false);
        return;
      }
      if (m === "circle-foreground-rotate") {
        runCenterMask("circle", true, false);
        return;
      }
      if (m === "circle-foreground-rotate-mirror") {
        runCenterMask("circle", true, true);
        return;
      }
      if (m === "cross-foreground") {
        runCenterMask("cross", false, false);
        return;
      }
      if (m === "cross-foreground-rotate") {
        runCenterMask("cross", true, false);
        return;
      }
      if (m === "cross-foreground-tunnel") {
        runCenterMaskTunnel("cross");
        return;
      }
      if (m === "bat-foreground") {
        runCenterMask("bat", false, false);
        return;
      }
      if (m === "bat-foreground-rotate") {
        runCenterMask("bat", true, false);
        return;
      }
      if (m === "tunnel") {
        runTunnel();
        return;
      }
      if (m === "social-scroll" || m === "dirty-scroll") {
        runScrollMode("social-scroll");
        return;
      }
      if (m === "scrollhole") {
        runScrollMode("scrollhole");
        return;
      }
      if (m === "strobe") {
        runStrobe();
        return;
      }
      runClassic(false);
    }

    function stop() {
      clearAllTimers();
      clearBatBackdropStage();
    }

    return {
      start: start,
      stop: stop,
    };
  }

  global.CompositionSharedRenderer = {
    createRunner: createRunner,
  };
})(window);
